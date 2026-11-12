"""The chat panel's back end: user text -> model (a scripted fake gateway here) -> tools -> engine, with the
user's confirmation before DOE variants are built."""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import chat, llm, studio  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def use(i, name, **args):
    return {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": args}], "stop_reason": "tool_use"}


def say(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


DOE = [{"name": "Hole_D8_x2.Diameter1", "values": [6, 10]}, {"name": "Fillet_R3_x4.Radius", "values": [2, 4]}]
SCRIPT = [
    use(1, "get_part"), use(2, "set_values", values={"Hole_D8_x2.Diameter1": 10}), say("지름을 **10 mm**로 바꿨습니다."),
    use(3, "plan_doe", factors=DOE), use(4, "run_doe"), say("4개 변형을 만들었습니다."),
    use(5, "plan_doe", factors=DOE), use(6, "run_doe"), say("취소하셨네요. 무엇을 바꿀까요?"),
    {"http": 400},
    say("다시 연결됐습니다."),
    use(7, "set_values", values={"Fillet_R3_x4.Radius": 40}), say("그 값으로는 만들 수 없습니다."),
]
seen = []


class Gateway(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        step = SCRIPT[min(len(seen), len(SCRIPT) - 1)]
        seen.append({"auth": self.headers.get("Authorization"), "body": body})
        if "http" in step:
            self.send_response(step["http"])
            self.end_headers()
            self.wfile.write(b"bad request")
            return
        out = json.dumps({"type": "message", "role": "assistant", "usage": {"output_tokens": 5}, **step}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


gw = HTTPServer(("127.0.0.1", 0), Gateway)
threading.Thread(target=gw.serve_forever, daemon=True).start()
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "STEPRECON_MODEL", "STEPRECON_BACKEND", "STEPRECON_BASE_URL"):
    os.environ.pop(k, None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
report("no settings -> chat is off", not chat.from_settings(None).brief()["enabled"])
os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{gw.server_port}"
os.environ["ANTHROPIC_AUTH_TOKEN"] = "secret-token"
os.environ["ANTHROPIC_MODEL"] = "test-model"
llm.AnthropicHttpBackend.retry_wait = 0

tmp = tempfile.mkdtemp(prefix="steprecon_chat_")
part = Part.makeBox(40, 30, 10)
part = part.makeFillet(3, [e for e in part.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 9])
part = part.cut(Part.makeCylinder(4, 10, V(12, 15, 0))).cut(Part.makeCylinder(4, 10, V(28, 15, 0)))
step = os.path.join(tmp, "plate.stp")
part.exportStep(step)

server, st = studio.serve(os.path.join(tmp, "ws"), chat=lambda s: chat.from_settings(s))
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, obj=None, data=None):
    if obj is not None:
        data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait(until, seconds=90):
    t0 = time.time()
    while time.time() - t0 < seconds:
        c = call("/api/chat")[1]
        if until(c):
            return c
        time.sleep(0.05)
    return call("/api/chat")[1]


idle = lambda c: not c["busy"]  # noqa: E731
with open(step, "rb") as f:
    call("/api/open?name=plate.stp", data=f.read())
v0 = st.ref_volume
n_hole = next(f["n"] for f in st.features if f["label"].startswith("Hole"))

c = call("/api/chat")[1]
report("panel knows the model, never the token", c["enabled"] and c["label"].startswith("test-model · 127.0.0.1") and "secret" not in json.dumps(c)
       and call("/api/pulse")[1]["chat"] == {"enabled": True, "busy": False, "seq": 0, "pending": False})
report("empty message refused", call("/api/chat/send", {"text": "  "})[0] == 400)

# ---- 1: change a value
code, r = call("/api/chat/send", {"text": "이 구멍을 10으로 키워줘", "selected": n_hole})
c = wait(idle)
roles = [m["role"] for m in c["items"]]
report("request -> tools -> answer", code == 200 and roles == ["user", "tool", "ai"] and "치수 변경" in c["items"][1]["text"]
       and c["items"][2]["text"].startswith("지름을") and st.worker.call(st.changed) == {"Hole_D8_x2.Diameter1": 10.0}, str(roles))
first = seen[0]["body"]
report("what the model was sent", seen[0]["auth"] == "Bearer secret-token" and first["model"] == "test-model" and len(first["tools"]) == 11
       and "STEP Studio" in first["system"] and first["messages"][0]["content"].startswith("[Part plate.stp is open, 8 features. The user has feature #"
                                                                                           + str(n_hole) + " selected (Hole_D8_x2; parameters: Hole_D8_x2.Diameter1")
       and first["messages"][0]["content"].endswith("\n이 구멍을 10으로 키워줘"))
report("tool results went back to the model", "Hole_D8_x2.Diameter1 = 8 mm" in seen[1]["body"]["messages"][2]["content"][0]["content"]
       and "8 -> 10 mm" in seen[2]["body"]["messages"][4]["content"][0]["content"])

# ---- 2: DOE waits for the user
call("/api/chat/send", {"text": "구멍과 필렛으로 실험 만들어줘"})
c = wait(lambda c: c["pending"] or not c["busy"])
p = c["pending"] or {}
report("run_doe waits with the plan on show", c["busy"] and p.get("total") == 4 and p["names"] == [d["name"] for d in DOE]
       and p["rows"][0] == [6, 2] and st.run_dir is None and call("/api/pulse")[1]["chat"]["pending"])
report("a second message is refused meanwhile", call("/api/chat/send", {"text": "그리고"})[0] == 400)
report("clearing is refused meanwhile", call("/api/chat/clear", {})[0] == 400)
call("/api/chat/approve", {"ok": True})
c = wait(idle)
files = sorted(os.listdir(st.run_dir)) if st.run_dir else []
report("after yes: variants built, part back as it was", c["pending"] is None and c["items"][-1]["text"] == "4개 변형을 만들었습니다."
       and [f for f in files if f.endswith(".step")] == [f"run_00{i}.step" for i in (1, 2, 3, 4)]
       and any("DOE 완료: 4 / 4" in m["text"] for m in c["items"] if m["role"] == "tool")
       and st.worker.call(st.changed) == {"Hole_D8_x2.Diameter1": 10.0}, str(files))
report("the model got the DOE summary", "4 of 4 variants built" in seen[5]["body"]["messages"][-1]["content"][0]["content"])
report("nothing waits -> approve is an error", call("/api/chat/approve", {"ok": True})[0] == 400)

# ---- 3: the user says no
done_dir = st.run_dir
call("/api/chat/send", {"text": "한 번 더"})
wait(lambda c: c["pending"] or not c["busy"])
call("/api/chat/approve", {"ok": False})
c = wait(idle)
report("after no: nothing built, the model is told", st.run_dir == done_dir and not st.plan["started"]
       and "chose NOT to run" in seen[8]["body"]["messages"][-1]["content"][0]["content"] and c["items"][-1]["role"] == "ai")

# ---- 4: the model server fails, the talk goes on
n_msgs = len(seen[8]["body"]["messages"])
call("/api/chat/send", {"text": "안녕"})
c = wait(idle)
report("server error is shown", c["items"][-1]["role"] == "error" and "HTTP 400" in c["items"][-1]["text"])
call("/api/chat/send", {"text": "다시"})
c = wait(idle)
last = seen[-1]["body"]["messages"]
report("next message works and keeps the history", c["items"][-1]["text"] == "다시 연결됐습니다." and len(last) == n_msgs + 2
       and last[-1]["content"].endswith("다시") and all(a["role"] != b["role"] for a, b in zip(last, last[1:])))

# ---- 5: a value that cannot be built
call("/api/chat/send", {"text": "필렛 40"})
c = wait(idle)
report("refused change is shown and nothing changed", any("실패" in m["text"] for m in c["items"][-3:] if m["role"] == "tool")
       and st.worker.call(st.changed) == {"Hole_D8_x2.Diameter1": 10.0} and "NOT changed" in seen[-1]["body"]["messages"][-1]["content"][0]["content"])
report("old tool results are shortened for the model", "(older result shortened" in json.dumps(seen[-1]["body"]["messages"])
       and "(older result shortened" not in json.dumps(seen[2]["body"]["messages"]))

code, r = call("/api/chat/clear", {})
report("clear", code == 200 and call("/api/chat")[1]["items"] == [] and st.chat.backend is None)
server.shutdown()
sys.exit(1 if fails else 0)
