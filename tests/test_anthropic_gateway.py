"""Anthropic-protocol gateway backend (plain HTTP, no SDK): the kind of endpoint Claude Code is pointed at with
ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, possibly serving a non-Claude model. Uses a local fake server."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import agent, cli, llm  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


RECT = [{"type": "rect", "p1": [0, 0], "p2": [20, 10]}]
SCRIPT = [
    {"busy": True},                                                             # 503 once -> retried
    {"content": [], "stop_reason": "max_tokens"},                               # cut off -> asked again, more room
    {"content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "t1", "name": "pad",
                  "input": {"plane": "XY", "geometry": RECT, "length": 5}}], "stop_reason": "tool_use"},
    {"content": [{"type": "text", "text": '<think>x</think>{"tool": "pocket", "args": {"plane": "XY", "offset": 5, '
                  '"through_all": true, "geometry": [{"type": "circle", "c": [10, 5], "r": 2}]}}'}],
     "stop_reason": "end_turn"},                                                # call written as text
]
seen = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        step = SCRIPT[min(len(seen), len(SCRIPT) - 1)]
        seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                     "version": self.headers.get("anthropic-version"), "body": body})
        if step.get("busy"):
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"busy")
            return
        out = json.dumps({"type": "message", "role": "assistant", "usage": {"output_tokens": 7}, **step}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "STEPRECON_MODEL"):
    os.environ.pop(k, None)
os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{srv.server_port}"
os.environ["ANTHROPIC_AUTH_TOKEN"] = "secret-token"
llm.AnthropicHttpBackend.retry_wait = 0

tmp = tempfile.mkdtemp(prefix="steprecon_gw_")
step = os.path.join(tmp, "plate.step")
Part.makeBox(20, 10, 5).cut(Part.makeCylinder(2, 5, App.Vector(10, 5, 0))).exportStep(step)

be = llm.make_backend("anthropic", model="vendor/some-model")
report("gateway env selects the HTTP backend", isinstance(be, llm.AnthropicHttpBackend) and be.custom)
r = agent.run(step, os.path.join(tmp, "out"), backend=be, profile="basic", verbose=False)
report("converges through the gateway", r["converged"] and r["turns"] == 2, f"turns={r['turns']} requests={len(seen)}")
report("cut-off answer is retried with a doubled limit, not counted as a turn",
       seen[2]["body"]["max_tokens"] == 2 * seen[1]["body"]["max_tokens"] == 32768)
first, last = seen[1], seen[-1]
report("request shape", first["path"] == "/v1/messages" and first["auth"] == "Bearer secret-token"
       and first["version"] == "2023-06-01" and first["body"]["model"] == "vendor/some-model"
       and isinstance(first["body"]["system"], str) and first["body"]["tools"][0].get("input_schema"))
msgs = last["body"]["messages"]
report("history stays valid (no empty assistant turn, tool_result follows tool_use)",
       all(m["content"] for m in msgs) and msgs[-1]["role"] == "user"
       and msgs[-1]["content"][0]["type"] == "tool_result" and msgs[-1]["content"][0]["tool_use_id"] == "t1")
report("503 was retried", len(seen) == 4)

NS = type("NS", (), {})
a = NS()
a.backend, a.model, a.profile, a.max_faces_prompt = "anthropic", "vendor/some-model", None, None
report("non-Claude model defaults to the basic profile", cli.run_kwargs(a)["profile"] == "basic")
a.model = "claude-opus-5-5"
report("Claude keeps the full profile", cli.run_kwargs(a)["profile"] == "full")

srv.shutdown()
sys.exit(1 if fails else 0)
