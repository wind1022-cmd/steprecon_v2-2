"""An AI client driving STEP Studio over MCP: the protocol (HTTP and the stdio relay) and every tool."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import studio, tools  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


tmp = tempfile.mkdtemp(prefix="steprecon_mcp_")
part = Part.makeBox(40, 30, 10)
part = part.makeFillet(3, [e for e in part.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 9])
part = part.cut(Part.makeCylinder(4, 10, V(12, 15, 0))).cut(Part.makeCylinder(4, 10, V(28, 15, 0)))
step = os.path.join(tmp, "plate.stp")
part.exportStep(step)
v0 = part.Volume

server, st = studio.serve(os.path.join(tmp, "ws"))
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()
ids = [0]


def http(path, data=None, headers=None):
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def rpc(method, params=None, notify=False):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        ids[0] += 1
        msg["id"] = ids[0]
    code, body = http("/mcp", json.dumps(msg).encode("utf-8"), {"Content-Type": "application/json"})
    return code, (json.loads(body.decode("utf-8")) if body else None)


def tool(name, /, **args):
    code, r = rpc("tools/call", {"name": name, "arguments": args})
    res = r["result"]
    return res["content"][0]["text"], res["isError"]


# ------------------------------------------------------------ protocol
code, r = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
res = r.get("result", {})
report("initialize", code == 200 and r["id"] == 1 and res["protocolVersion"] == "2025-03-26" and "tools" in res["capabilities"]
       and res["serverInfo"]["name"] and "run_doe" in res["instructions"])
report("unknown protocol version -> ours", rpc("initialize", {"protocolVersion": "1999-01-01"})[1]["result"]["protocolVersion"] == "2025-06-18")
report("notification gets no body", rpc("notifications/initialized", notify=True) == (202, None))
code, r = rpc("tools/list")
listed = r["result"]["tools"]
report("tools/list", [t["name"] for t in listed] == tools.NAMES and all(t["inputSchema"]["type"] == "object" and t["description"] for t in listed),
       str(len(listed)))
report("ping", rpc("ping")[1]["result"] == {})
report("unknown method is an error", rpc("nope/nope")[1]["error"]["code"] == -32601)
report("unknown tool is an error", rpc("tools/call", {"name": "format_disk", "arguments": {}})[1]["error"]["code"] == -32602)
report("GET /mcp refused", http("/mcp")[0] == 405)
report("a foreign web page cannot call it", http("/mcp", b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                                                 {"Origin": "https://evil.example", "Content-Type": "application/json"})[0] == 403)

# ------------------------------------------------------------ tools
text, bad = tool("get_part")
report("no part yet -> told to open one", bad and "open_part" in text)
text, bad = tool("open_part", path=os.path.join(tmp, "missing.stp"))
report("missing file reported", bad and "not found" in text)
text, bad = tool("open_part", path=step)
report("open_part lists the candidates, builds nothing", not bad and "hole D8, 2 holes" in text and "fillet R3, 4 faces" in text
       and "Movable flat faces (6" in text and "make_dimensions" in text and st.doc is None, text.splitlines()[0])
text2, bad2 = tool("set_values", values={"x": 1})
report("no dimensions yet -> told to make them", bad2 and "make_dimensions" in text2)
report("unknown group refused", tool("make_dimensions", groups=["g99"])[1] and tool("make_dimensions")[1])
gid = next(g["id"] for g in st.cands["groups"] if g["kind"] == "fillet")
text, bad = tool("make_dimensions", groups=[gid])
report("make_dimensions: only the chosen group", not bad and "Fillet_R3_x4.Radius = 3 mm" in text and "Hole_D8" not in text
       and "Features: 1" in text, text.splitlines()[0])
report("candidates on request show what was made", "[dimension made for 4]" in tool("get_part", candidates=True)[0])
text, bad = tool("make_dimensions", everything=True)
report("open_part describes the part", not bad and "identical to the file: yes" in text and "Hole_D8_x2.Diameter1 = 8 mm" in text
       and "Fillet_R3_x4.Radius = 3 mm" in text and "Features: 8" in text and "nothing, all parameters" in text, text.splitlines()[0])
text, bad = tool("get_part", category="fillet", density_g_cm3=2.7)
report("get_part filter + mass", not bad and "1 shown" in text and "Hole_D8" not in text and f"mass {tools.g(v0 / 1000 * 2.7)} g" in text)
rev0 = st.rev

text, bad = tool("set_values", values={"Hole_D8_x2.Diameter1": 10})
report("set_values rebuilds", not bad and "8 -> 10 mm" in text and abs(st.worker.call(tools._measure, st)["volume"] - (v0 - 2 * 3.141592653589793 * 9 * 10)) < 0.05
       and st.rev > rev0, text.splitlines()[0])
text, bad = tool("set_values", values={"fillet_r3_x4.radius": 2})
report("earlier change is kept, name matched without case", not bad and st.worker.call(st.changed) == {"Hole_D8_x2.Diameter1": 10.0, "Fillet_R3_x4.Radius": 2.0})
text, bad = tool("set_values", values={"Fillet_R3_x4.Radius": 40})
report("impossible value refused and taken back", bad and "NOT changed" in text
       and st.worker.call(st.changed) == {"Hole_D8_x2.Diameter1": 10.0, "Fillet_R3_x4.Radius": 2.0}
       and not st.worker.call(studio.P.problems, st.doc), text.splitlines()[0][:70])
text, bad = tool("set_values", values={"Hole_D8.Diameter": 9})
report("wrong name -> suggestions", bad and "Did you mean" in text and "Hole_D8_x2.Diameter1" in text)
text, bad = tool("set_values", values={"Fillet_R3_x4.Radius": "big"})
report("non-number refused", bad and "must be a number" in text)
text, bad = tool("set_values", values={"Fillet_R3_x4.Radius": 4}, replace=True)
report("replace drops the other changes", not bad and st.worker.call(st.changed) == {"Fillet_R3_x4.Radius": 4.0})
text, bad = tool("get_part", feature=next(f["n"] for f in st.features if f["label"].startswith("Fillet")))
report("changed value shown with the original", "Radius = 4 mm  (original 3)" in text and "Changed now: Fillet_R3_x4.Radius = 4" in text)
text, bad = tool("reset_values")
report("reset_values", not bad and st.worker.call(st.changed) == {} and "1 parameter(s) put back" in text)

text, bad = tool("find_limits", name="Fillet_R3_x4.Radius")
import re  # noqa: E402
m = re.search(r"can be built from ([\d.]+) to ([\d.]+) mm", text)
report("find_limits brackets the largest fillet", not bad and m and float(m.group(1)) < 0.5 and 10 < float(m.group(2)) <= 15.01
       and "fails above" in text and st.worker.call(st.changed) == {}, m.group(0) if m else text[:80])
text, bad = tool("find_limits", name="Hole_D8_x2.Diameter1", low=2, high=12)
report("limits inside the asked range", not bad and "from 2 to 12 mm" in text and "still builds" in text)
text, bad = tool("find_limits", name="Hole_D8_x2.Diameter1", low=9)
report("limits need low < value < high", bad and "low < present value" in text)

# ------------------------------------------------------------ DOE
text, bad = tool("run_doe")
report("run without a plan refused", bad and "plan_doe" in text)
tool("set_values", values={"Face_1.Thickness": st.base_values["Face_1.Thickness"] + 1})
text, bad = tool("plan_doe", factors=[{"name": "Hole_D8_x2.Diameter1", "values": [6, 10]}, {"name": "Fillet_R3_x4.Radius", "min": 2, "max": 40, "levels": 2}])
report("plan_doe builds nothing", not bad and "4 runs" in text and "Nothing has been built" in text and "Kept in every run: Face_1.Thickness" in text
       and st.run_dir is None and "4 | 10 | 40" in text)
text, bad = tool("run_doe")
files = sorted(os.listdir(st.run_dir))
report("run_doe: variants, failures, effects", not bad and "2 of 4 variants built" in text and "run 2 failed" in text
       and "Effect of each factor" in text and files == ["plate_doe.zip", "results.csv", "run_001.step", "run_003.step"], str(files))
report("the earlier change is still there after the DOE", list(st.worker.call(st.changed)) == ["Face_1.Thickness"])
with open(os.path.join(st.run_dir, "results.csv"), encoding="utf-8-sig") as f:
    head = f.readline().strip().split(",")
report("results.csv has the factors", head[:3] == ["run", "Hole_D8_x2.Diameter1", "Fillet_R3_x4.Radius"])
text, bad = tool("run_doe")
report("a finished plan is not run twice", not bad and "already been run" in text and len(st.rows) == 4)
report("doe_results", "2 of 4 variants built" in tool("doe_results")[0])
tool("reset_values")
text, bad = tool("plan_doe", factors=[{"name": "Hole_D8_x2.Diameter1", "min": 6, "max": 10}, {"name": "Fillet_R3_x4.Radius", "values": [2, 4]}], design="one_at_a_time")
report("one_at_a_time: centre + each level alone", not bad and "5 runs" in text and "1 | 8 | 3" in text, text.splitlines()[0])
text, bad = tool("plan_doe", factors=[{"name": "Hole_D8_x2.Diameter1", "min": 6, "max": 10}, {"name": "Fillet_R3_x4.Radius", "min": 1, "max": 5}], design="latin", runs=6)
rows = st.plan["rows"]
report("latin: every run a different level of each factor", not bad and len(rows) == 6 and len({r[0] for r in rows}) == 6
       and all(6 <= r[0] <= 10 and 1 <= r[1] <= 5 for r in rows))
text, bad = tool("plan_doe", factors=[{"name": "Hole_D8_x2.Diameter1", "min": 6, "max": 10, "levels": 50}, {"name": "Fillet_R3_x4.Radius", "min": 1, "max": 5, "levels": 50}])
report("too many runs refused", bad and "More than 2000" in text)
tool("plan_doe", factors=[{"name": "Hole_D8_x2.Diameter1", "values": [6, 7, 9, 10]}])
st.worker.call(setattr, tools.time, "time", (lambda real=tools.time.time, n=[0]: (n.__setitem__(0, n[0] + 4), real() + n[0])[1]))
text, bad = tool("run_doe", max_seconds=5)
part_done = st.plan["next"]
text2, bad2 = tool("run_doe", max_seconds=1e6)
report("run_doe continues where it stopped", "remain" in text and 0 <= part_done < 4 and "4 of 4 variants built" in text2, f"{part_done} then all")

# ------------------------------------------------------------ what the user sees
n_hole = next(f["n"] for f in st.features if f["label"].startswith("Hole"))
text, bad = tool("show_feature", feature=n_hole)
text2, bad2 = tool("label_features", labels=[{"feature": n_hole, "name": "체결 구멍 <b>"}])
pulse = json.loads(http("/api/pulse")[1].decode("utf-8"))
report("pulse: highlight, names, activity", not bad and not bad2 and pulse["highlight"] == n_hole and pulse["aliases"] == {str(n_hole): "체결 구멍 b"}
       and len(pulse["notes"]) > 8 and any("DOE 완료" in n["text"] for n in pulse["notes"]) and pulse["rev"] == st.rev)
state = json.loads(http("/api/state")[1].decode("utf-8"))
report("state carries the given name", next(f for f in state["features"] if f["n"] == n_hole)["alias"] == "체결 구멍 b" and state["rev"] == st.rev)
report("no such feature", tool("show_feature", feature=99)[1])
tool("set_values", values={"Hole_D8_x2.Diameter1": 9})
text, bad = tool("export_step", file_name="..\\..\\out side")
out = os.path.join(st.dir, "out_side.step")
report("export_step stays in the work folder", not bad and os.path.isfile(out) and out in text and "Diameter1 = 9" in text
       and abs(Part.read(out).Volume - (v0 - 2 * 3.141592653589793 * (4.5 ** 2 - 16) * 10)) < 0.05)
report("unknown argument explained", tool("get_part", colour="red") == ("get_part has no argument colour. Arguments: category, feature, density_g_cm3, candidates", True))

# ------------------------------------------------------------ stdio relay (mcp_server.py)
msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_part", "arguments": {"category": "hole"}}}]
p = subprocess.run([sys.executable, os.path.join(ROOT, "mcp_server.py"), "--url", base],
                   input=("\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n").encode("utf-8"), capture_output=True, timeout=120)
lines = [json.loads(ln) for ln in p.stdout.decode("utf-8").splitlines() if ln.strip()]
report("stdio relay: one line per request, nothing else on stdout", p.returncode == 0 and [m.get("id") for m in lines] == [1, 2, 3]
       and len(lines[1]["result"]["tools"]) == len(tools.NAMES) and "체결 구멍 b" in lines[2]["result"]["content"][0]["text"],
       p.stderr.decode("utf-8", "replace").strip()[:120])
p = subprocess.run([sys.executable, os.path.join(ROOT, "mcp_server.py"), "--url", "http://127.0.0.1:9"],
                   input=(json.dumps(msgs[0]) + "\n").encode("utf-8"), capture_output=True, timeout=120)
lines = [json.loads(ln) for ln in p.stdout.decode("utf-8").splitlines() if ln.strip()]
report("relay answers with an error when the engine is gone", len(lines) == 1 and lines[0]["id"] == 1 and "cannot be reached" in lines[0]["error"]["message"])

server.shutdown()
sys.exit(1 if fails else 0)
