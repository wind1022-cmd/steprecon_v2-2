"""CPU setting (cores, priority) and progress of an analysis: engine, HTTP API and saved settings."""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import cpu, machined, studio  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


T = cpu.TOTAL
start = cpu.state()

# ------------------------------------------------------------ cpu module
report("parse", cpu.parse("1") == 1 and cpu.parse("all") == T and cpu.parse("100%") == T and cpu.parse(str(T + 5)) == T
       and cpu.parse("50%") == max(1, round(T / 2)) and 1 <= cpu.default_cores() <= T, f"{T} cores")
for bad in ("0", "-2", "abc", "0%"):
    try:
        cpu.parse(bad)
        report(f"parse refuses {bad!r}", False)
    except ValueError:
        pass
if cpu.CAN_LIMIT and T > 1:
    st = cpu.apply(cores=1)
    report("limit to one core", st["cores"] == 1)
    st = cpu.apply(cores=T + 10)
    report("more than there are -> all", st["cores"] == T)
else:
    report("this system cannot limit cores (only priority)", True)
st = cpu.apply(low=True)
report("low priority", st["low"])
if cpu.CAN_RAISE:
    report("normal priority again", not cpu.apply(low=False)["low"])

# ------------------------------------------------------------ progress of machined.build
part = Part.makeBox(60, 40, 12)
part = part.makeFillet(4, [e for e in part.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 11])
for x in (12, 30, 48):
    part = part.cut(Part.makeCylinder(3, 12, V(x, 20, 0)))
part = part.makeChamfer(0.8, [e for e in part.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Vertexes[0].Point.z - 12) < 1e-6])
seen = []
sess, rep = machined.build(part, lambda f, stage, info: seen.append((f, stage, dict(info))))
fr = [f for f, _, _ in seen]
stages = []
for _, s, _ in seen:
    if s not in stages:
        stages.append(s)
report("progress goes forward, 0..1", fr and all(0 <= a <= b <= 1 for a, b in zip(fr, fr[1:])) and fr[-1] >= 0.8,
       f"{len(seen)} reports, last {fr[-1]:.2f}" if fr else "none")
report("every stage is reported, in order", stages[:1] == ["blends"] and "holes" in stages and stages[-1] == "faces"
       and stages.index("holes") < stages.index("faces"), str(stages))
h = next(i for _, s, i in seen if s == "holes")
report("counts in the reports", h.get("n", 0) >= 1 and h.get("i") == 1)
report("a line for people", machined.describe("holes", {"i": 2, "n": 5, "attempt": 1, "attempts": 3}) == "구멍을 피처로 만드는 중 2/5 (시도 1/3)"
       and machined.describe("faces", {"i": 1, "n": 4}, korean=False) == "checking flat faces 1/4")
report("result is the same with progress", rep["holes_made"] == 3 and sess.compare().get("converged"))
App.closeDocument(sess.doc.Name)
calls = []
machined.build(Part.makeBox(5, 5, 5))
report("no callback, no reports left over", machined._progress is None)
broken = machined.build(Part.makeBox(5, 5, 5), lambda *a: 1 / 0)
report("a failing display does not stop the work", broken[0] is not None)

# ------------------------------------------------------------ the app: API, saved setting, progress while opening
tmp = tempfile.mkdtemp(prefix="steprecon_cpu_")
step = os.path.join(tmp, "plate.stp")
part.exportStep(step)
server, st = studio.serve(os.path.join(tmp, "ws"))
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, obj=None, data=None):
    if obj is not None:
        data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


code, c = call("/api/cpu")
report("GET /api/cpu", code == 200 and c["total"] == T and set(c) >= {"cores", "low", "can_limit", "can_raise"})
want = 2 if T > 2 and cpu.CAN_LIMIT else T
code, c = call("/api/cpu", {"cores": want, "low": True})
with open(os.path.join(tmp, "ws", "settings.json"), encoding="utf-8") as f:
    saved = json.load(f)
report("POST /api/cpu applies and remembers", code == 200 and c["cores"] == want and c["low"]
       and saved == {"cpu_cores": want, "cpu_low": True} and st.load_settings() == saved)
report("bad value refused", call("/api/cpu", {"cores": "many"})[0] == 400)
report("idle: no progress", call("/api/progress")[1] == {"active": False})

got, done = [], threading.Event()


with open(step, "rb") as f:
    opened = call("/api/open?name=plate.stp", data=f.read())     # quick: only the candidates


def opener():          # making the dimensions is the long part
    got.append(call("/api/make", {"all": True}))
    done.set()


threading.Thread(target=opener, daemon=True).start()
polls = []
while not done.is_set():
    polls.append(call("/api/progress")[1])
    call("/api/cpu")                       # answered while the analysis runs
    time.sleep(0.02)
act = [p for p in polls if p.get("active")]
pcts = [p["pct"] for p in act]
report("progress can be read while dimensions are made", len(act) >= 3 and all(a <= b for a, b in zip(pcts, pcts[1:]))
       and act[0]["name"] == "plate.stp" and all(p["elapsed"] >= 0 for p in act), f"{len(act)} polls, {pcts[:1]} .. {pcts[-1:]}")
texts = {p["text"] for p in act}
known = [k for _, k in machined.STAGES.values()] + ["STEP 파일", "형상 읽음", "모델 저장", "원본과 비교", "치수 정리", "3D 화면"]
report("stage texts", len(texts) >= 2 and all(any(t.startswith(k) for k in known) for t in texts), " | ".join(sorted(texts))[:150])
report("made fine, progress cleared", opened[0] == 200 and got and got[0][0] == 200 and got[0][1]["info"]["identical"] and call("/api/progress")[1] == {"active": False})

server.shutdown()
cpu.apply(cores=start["cores"], low=start["low"] if cpu.CAN_RAISE else None)
sys.exit(1 if fails else 0)
