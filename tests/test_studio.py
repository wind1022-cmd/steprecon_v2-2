"""STEP Studio back end (app.py): the local HTTP API the page uses - open, state + mesh, edit, DOE, downloads."""
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import studio  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


tmp = tempfile.mkdtemp(prefix="steprecon_studio_")
part = Part.makeBox(40, 30, 10)
part = part.makeFillet(3, [e for e in part.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 9])
part = part.cut(Part.makeCylinder(4, 10, V(12, 15, 0))).cut(Part.makeCylinder(4, 10, V(28, 15, 0)))
step = os.path.join(tmp, "plate.stp")
part.exportStep(step)

server, st = studio.serve(os.path.join(tmp, "ws"))
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()     # FreeCAD is only touched by that thread


def call(path, data=None, raw=False, host=None):
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return (r.status, body if raw else json.loads(body.decode("utf-8")))
    except urllib.error.HTTPError as e:
        body = e.read()
        return (e.code, body if raw else json.loads(body.decode("utf-8")))


def post(path, obj):
    return call(path, json.dumps(obj).encode("utf-8"))


code, page = call("/", raw=True)
text = page.decode("utf-8")
report("page is served, self-contained", code == 200 and "<canvas" in text and "http://" not in text.replace("http://www.w3.org", "")
       and "https://" not in text, f"{len(page)} bytes")
report("nothing loaded at start", call("/api/state")[1] == {"loaded": False})

with open(step, "rb") as f:
    code, s = call("/api/open?name=plate.stp", f.read())
feats = s.get("features", [])
cats = sorted(f["category"] for f in feats)
report("upload -> features", code == 200 and s["loaded"] and s["info"]["identical"] and cats == ["face"] * 6 + ["fillet", "hole"], str(cats))
m = s["mesh"]
nv = len(m["positions"]) // 3
report("mesh is consistent", nv > 100 and len(m["normals"]) == len(m["positions"]) and len(m["feature"]) == nv
       and len(m["indices"]) % 3 == 0 and max(m["indices"]) < nv and len(m["lines"]) % 6 == 0
       and [round(x) for x in m["bbox"]] == [0, 0, 0, 40, 30, 10])
owned = {n: m["feature"].count(n) for n in set(m["feature"])}
report("every face belongs to the feature that made it or would move it",
       {1, 2} <= set(owned) and all(v > 0 for v in owned.values()) and sum(owned.values()) == nv, str(owned))
thick = sorted(p["base"] for f in feats if f["category"] == "face" for p in f["params"])
sides = sorted(f["side"] for f in feats if f["category"] == "face")
kinds = sorted(f["kind"] for f in feats if f["category"] == "face")
report("all six faces of the plate are movable", thick == [10.0, 10.0, 30.0, 30.0, 40.0, 40.0]
       and sides == ["+X", "+Y", "+Z", "-X", "-Y", "-Z"] and kinds == ["land"] * 4 + ["straight"] * 2, f"{thick} {sides} {kinds}")
hole = next(f for f in feats if f["category"] == "hole")
fil = next(f for f in feats if f["category"] == "fillet")
dia = next(p for p in hole["params"] if p["short"].startswith("Diameter"))
rad = next(p for p in fil["params"] if p["short"] == "Radius")
report("parameters with base values", dia["base"] == 8 and rad["base"] == 3 and hole["count"] == 2 and fil["count"] == 4)

v0 = s["measure"]["volume"]
code, s2 = post("/api/set", {"values": {dia["name"]: 10}})
import math  # noqa: E402
report("edit rebuilds the solid", code == 200 and not s2["problems"] and "mesh" in s2
       and abs((v0 - s2["measure"]["volume"]) - 2 * math.pi * (25 - 16) * 10) < 0.05)
code, s3 = post("/api/set", {"values": {rad["name"]: 40}})
report("an impossible value is reported, no mesh sent", code == 200 and s3["problems"] and "mesh" not in s3, str(s3["problems"])[:60])
code, s4 = post("/api/set", {"values": {}})
report("empty edit goes back to the original", abs(s4["measure"]["volume"] - v0) < 1e-3 and not s4["problems"])
report("unknown parameter is an error", post("/api/set", {"values": {"nope": 1}})[0] == 500)

post("/api/doe/start", {"names": [dia["name"], rad["name"]]})
rows = []
k = 0
for d in (6, 10):
    for r in (2, 40):
        k += 1
        rows.append(post("/api/doe/variant", {"index": k, "values": {dia["name"]: d, rad["name"]: r}})[1])
code, fin = post("/api/doe/finish", {})
report("DOE: good variants built, bad ones reported", [r["ok"] for r in rows] == [True, False, True, False]
       and fin["ok"] == 2 and fin["total"] == 4 and all(r["error"] for r in rows if not r["ok"]))
report("DOE leaves the part at its own values", abs(fin["state"]["measure"]["volume"] - v0) < 1e-3)
code, z = call("/api/file?name=" + fin["zip"], raw=True)
names = sorted(zipfile.ZipFile(io.BytesIO(z)).namelist())
report("zip holds the STEP files and the table", code == 200 and names == ["results.csv", "run_001.step", "run_003.step"], str(names))
code, csvb = call("/api/file?name=results.csv", raw=True)
report("results table downloadable", code == 200 and csvb.decode("utf-8-sig").splitlines()[0].startswith("run," + dia["name"]))
report("files outside the work folder cannot be fetched", call("/api/file?name=..%2F..%2Fsecret.txt")[0] == 404)
report("requests for another host name are refused", call("/api/state", host="evil.example")[0] == 403)
report("wrong file type is refused", call("/api/open?name=x.exe", b"MZ")[0] == 400)

server.shutdown()
sys.exit(1 if fails else 0)
