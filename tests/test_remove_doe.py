"""Removing features from the part (defeature), staying in the pick view when nothing could be made, and the DOE
built by several worker processes."""
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
from step_recon import machined, studio  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


box = Part.makeBox(60, 40, 10)
part = box.makeFillet(3, [e for e in box.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 9])
for x in (15, 45):
    part = part.cut(Part.makeCylinder(4, 10, V(x, 20, 0)))
part = part.cut(Part.makeCylinder(2.5, 10, V(30, 32, 0)))
v_part = part.Volume

# ------------------------------------------------------------ engine
sc = machined.scan(part)
fil = [it["faces"][0] for it in sc["items"] if it["kind"] == "fillet"]
new, done, failed = machined.remove_faces(part, fil)
report("remove_faces: fillets gone, corners sharp again", done == 4 and failed == 0
       and abs(new.Volume - (v_part + (4 - 3.141592653589793) * 9 * 10)) < 1e-3 and len(new.Faces) == len(part.Faces) - 4,
       f"{new.Volume - v_part:+.2f} mm3")
side = next(i for i, f in enumerate(part.Faces) if type(f.Surface).__name__ == "Plane" and abs(f.normalAt(0, 0).x) > 0.9)
new, done, failed = machined.remove_faces(part, fil[:1] + [side])
report("what cannot be removed is left, the rest is done", done == 1 and failed == 1 and new.isValid(), f"{done} done, {failed} failed")

# ------------------------------------------------------------ the app
tmp = tempfile.mkdtemp(prefix="steprecon_rm_")
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


with open(step, "rb") as f:
    code, s = call("/api/open?name=plate.stp", data=f.read())
grp = {(g["kind"], g["size"]): g["items"] for g in s["groups"]}
others = [it["id"] for it in s["items"] if it["kind"] == "other"]
report("every face can be picked (the rest as 'other')", sum(len(it["faces"]) for it in s["items"]) == len(part.Faces)
       and not s["can_undo"], f"{len(others)} other")
code, r = call("/api/remove", {"items": grp[("fillet", 3.0)]})
info = r.get("info", {})
report("remove the fillets: part changed, candidates found again", code == 200 and r["mode"] == "pick" and r["can_undo"]
       and info["last_removed"] == {"removed": 4, "failed": 0, "volume_pct": info["last_removed"]["volume_pct"]}
       and info["last_removed"]["volume_pct"] > 0 and not any(g["kind"] == "fillet" for g in r["groups"])
       and info["faces"] == len(part.Faces) - 4, str(info.get("last_removed")))
sim = os.path.join(st.dir, info["simplified"])
report("the simplified part is saved as STEP", os.path.isfile(sim) and abs(Part.read(sim).Volume - r["measure"]["volume"]) < 1e-2)
g2 = {(g["kind"], g["size"]): g["items"] for g in r["groups"]}
code, r2 = call("/api/remove", {"items": g2[("hole", 5.0)]})
report("remove a hole: filled", code == 200 and ("hole", 5.0) not in {(g["kind"], g["size"]) for g in r2["groups"]}
       and abs((r2["measure"]["volume"] - r["measure"]["volume"]) - 3.141592653589793 * 2.5 ** 2 * 10) < 1e-2
       and r2["info"]["removed_total"] == 5)
flat = next(it["id"] for it in r2["items"] if it["kind"] == "other" or (it["kind"] == "face" and it["side"] == "+X"))
code, e = call("/api/remove", {"items": [it["id"] for it in r2["items"] if it["kind"] == "face"][:1]})
report("a face that cannot be closed over is refused, nothing changes", code == 400 and "없앨 수 없" in e["error"]
       and call("/api/state")[1]["info"]["removed_total"] == 5, e.get("error", "")[:50])
code, u = call("/api/unremove", {})
report("undo brings the hole back", code == 200 and ("hole", 5.0) in {(g["kind"], g["size"]) for g in u["groups"]}
       and abs(u["measure"]["volume"] - r["measure"]["volume"]) < 1e-6 and u["can_undo"])
g3 = {(g["kind"], g["size"]): g["items"] for g in u["groups"]}
code, m = call("/api/make", {"items": g3[("hole", 8.0)]})
report("dimensions on the simplified part", code == 200 and m["mode"] == "edit" and m["info"]["identical"]
       and abs(m["reference_volume"] - r["measure"]["volume"]) < 1e-3)

# nothing could be made -> stay in the pick view with the reason
real = st._run_job
st._run_job = lambda job, scale=None: {"report": {"features": 0, "holes_made": 0, "fillets_made": 0, "chamfers_made": 0,
                                                    "skipped": ["fillets/chamfers: could not be reproduced exactly, left in the blank"]},
                                        "converged": True, "xor_pct": 0.0}
code, e = call("/api/make", {"items": g3[("hole", 8.0)]})
st._run_job = real
after = call("/api/state")[1]
report("nothing made -> told why, pick view stays", code == 400 and "치수로 만들지 못했습니다" in e["error"] and "없애기" in e["error"]
       and after["mode"] == "pick" and not after["built"], e.get("error", "")[:40])

# ------------------------------------------------------------ DOE by several processes
code, m = call("/api/make", {"items": g3[("hole", 8.0)]})
dia = next(p["name"] for f in m["features"] for p in f["params"] if p["short"] == "Diameter1")
vals = [5, 6, 7, 9, 10, 500]                 # the last one cannot be built
code, started = call("/api/doe/run", {"names": [dia], "variants": [{"index": i + 1, "values": {dia: v}} for i, v in enumerate(vals)]})
report("a second DOE is refused while one runs", call("/api/doe/run", {"names": [dia], "variants": []})[0] == 400)
t0, seen, rows = time.time(), 0, []
while True:
    s = call(f"/api/doe/status?from={seen}")[1]
    rows += s["rows"]
    seen += len(s["rows"])
    if not s["running"]:
        break
    time.sleep(0.1)
code, fin = call("/api/doe/finish", {})
with open(os.path.join(st.run_dir, "results.csv"), encoding="utf-8-sig") as f:
    lines = f.read().splitlines()
vol = {r["run"]: r.get("volume") for r in rows}
report("parallel DOE: every variant once, good ones built, bad one reported", code == 200 and started["workers"] >= 2
       and sorted(r["run"] for r in rows) == [1, 2, 3, 4, 5, 6] and [r["ok"] for r in sorted(rows, key=lambda r: r["run"])] == [True] * 5 + [False]
       and fin["ok"] == 5 and fin["total"] == 6 and vol[1] > vol[2] > vol[3] > vol[4] > vol[5],
       f"{started['workers']} processes, {time.time() - t0:.1f}s")
report("results in run order, files there", [ln.split(",")[0] for ln in lines[1:]] == ["1", "2", "3", "4", "5", "6"]
       and sorted(n for n in os.listdir(st.run_dir) if n.endswith(".step")) == [f"run_00{i}.step" for i in range(1, 6)]
       and abs(Part.read(os.path.join(st.run_dir, "run_005.step")).Volume - vol[5]) < 1e-2)
report("the part on screen is untouched by the DOE", abs(fin["state"]["measure"]["volume"] - m["measure"]["volume"]) < 1e-6)
server.shutdown()
sys.exit(1 if fails else 0)
