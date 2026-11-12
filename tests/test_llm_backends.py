"""Verify the OpenAI-compatible backend end to end against a scripted fake server (no real LLM needed).

Run:  <FreeCAD python> tests_llm_backends.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import agent, llm, synth  # noqa: E402

# Ground-truth steps for synth_003 (see data/synth/synth_003.step.history.txt)
STEPS = [
    ("pad", {"plane": "XY", "geometry": [{"type": "rect", "p1": [0, 0], "p2": [40, 50]}], "length": 20}),
    ("pocket", {"plane": "XY", "offset": 20, "depth": 3, "geometry": [{"type": "rect", "p1": [12, 10], "p2": [27, 25]}]}),
    ("hole", {"plane": "XY", "offset": 20, "x": 28, "y": 28, "diameter": 4, "through_all": True}),
    ("compare", {}),
    ("finish", {}),
]


def make_server(style):
    """style: native | text | no_tools_400 | prose_first | bad_args_first"""
    state = {"i": 0, "calls": 0, "saw_tools": 0, "prose_done": False, "bad_done": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"] += 1
            has_tools = "tools" in req
            state["saw_tools"] += has_tools
            if style == "no_tools_400" and has_tools:
                return self._send(400, {"error": {"message": "registry.ollama.ai/x does not support tools"}})
            if style == "prose_first" and not state["prose_done"]:
                state["prose_done"] = True
                return self._send(200, {"choices": [{"message": {"role": "assistant",
                                                                  "content": "Let me think about this part first."}}]})
            name, args = STEPS[state["i"]]
            if style == "bad_args_first" and not state["bad_done"]:
                state["bad_done"] = True            # native call with invalid JSON arguments; model must recover
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c0", "type": "function", "function": {"name": "pad", "arguments": "{not json"}}]}
                return self._send(200, {"choices": [{"message": msg}]})
            state["i"] += 1
            if has_tools and style in ("native", "prose_first", "bad_args_first"):
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"c{state['i']}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}]}
            else:       # text protocol: JSON in a code fence, like many local models answer
                msg = {"role": "assistant",
                       "content": "```json\n" + json.dumps({"tool": name, "args": args}) + "\n```"}
            self._send(200, {"choices": [{"message": msg}]})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def main():
    tmp = tempfile.mkdtemp(prefix="steprecon_test_")
    step = synth.generate(os.path.join(tmp, "data"), n=1, start=3)[0]
    failures = 0
    cases = [("native tools", "native", "native", "full"),
             ("text protocol", "text", "text", "basic"),
             ("auto -> falls back after HTTP 400", "no_tools_400", "auto", "basic"),
             ("prose reply gets nudged", "prose_first", "native", "full"),
             ("invalid tool arguments are reported", "bad_args_first", "native", "full")]
    for label, style, mode, profile in cases:
        srv, state = make_server(style)
        be = llm.make_backend("openai", base_url=f"http://127.0.0.1:{srv.server_port}/v1", model="fake",
                              tool_mode=mode)
        r = agent.run(step, os.path.join(tmp, "out_" + style), backend=be, profile=profile, verbose=False)
        srv.shutdown()
        ok = r["converged"] and len(r["features"]) == 3
        if style == "no_tools_400":
            ok = ok and be.mode == "text" and state["saw_tools"] == 1
        if style == "bad_args_first":
            tr = json.dumps(be.transcript())
            ok = ok and "not valid JSON" in tr
        print(f"{'PASS' if ok else 'FAIL'}  {label:40s} converged={r['converged']} xor={r['xor_pct']} "
              f"turns={r['turns']} mode={be.mode} profile={r['profile']}")
        failures += not ok

    # schema flattening must remove anyOf (llama.cpp grammar servers choke on it)
    flat = json.dumps([llm.flatten_schema(t["input_schema"]) for t in agent.TOOLS])
    ok = "anyOf" not in flat
    print(f"{'PASS' if ok else 'FAIL'}  flatten_schema removes anyOf from all {len(agent.TOOLS)} tools")
    failures += not ok

    # text-protocol parser edge cases
    calls = llm.parse_text_calls('I will pad.\n```json\n{"tool":"pad","args":{"length":5,"plane":"XY","geometry":[]}}\n```'
                                 ' then {"name":"finish","arguments":"{}"}')
    ok = [c.name for c in calls] == ["pad", "finish"] and calls[0].args["length"] == 5
    print(f"{'PASS' if ok else 'FAIL'}  parse_text_calls: fences, 'name/arguments' aliases, nested braces")
    failures += not ok

    print("ALL OK" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
