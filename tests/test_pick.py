"""Pick what becomes a dimension: scan finds candidates quickly, make builds only the picked ones (engine + API)."""
import json
import math
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


# a plate: 4 vertical edges filleted R3, two D8 holes with a 0.5 rim chamfer, one D5 hole, the top outer edges chamfered 1
part = Part.makeBox(60, 40, 10)
part = part.makeFillet(3, [e for e in part.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 9])
for x in (15, 45):
    part = part.cut(Part.makeCylinder(4, 10, V(x, 20, 0)))
part = part.cut(Part.makeCylinder(2.5, 10, V(30, 32, 0)))
part = part.makeChamfer(0.5, [e for e in part.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Curve.Radius - 4) < 1e-6
                              and abs(e.Vertexes[0].Point.z - 10) < 1e-6])

# ------------------------------------------------------------ scan
t0 = time.time()
sc = machined.scan(part)
dt = time.time() - t0
groups = {(g["kind"], g["size"]): g for g in sc["groups"]}
report("scan: candidates grouped by kind and size", set(groups) == {("hole", 8.0), ("hole", 5.0), ("fillet", 3.0), ("chamfer", 0.5)}
       and len(groups[("hole", 8.0)]["items"]) == 2 and len(groups[("fillet", 3.0)]["items"]) == 4
       and len(groups[("chamfer", 0.5)]["items"]) == 2, f"{sorted(groups)} {dt:.2f}s")
items = {it["id"]: it for it in sc["items"]}
planes = [it for it in sc["items"] if it["kind"] == "face"]
report("scan: movable flat faces with their side", {p["side"] for p in planes} >= {"+Z", "-Z"} and all(p["group"] if "group" in p else True for p in planes))
report("scan: every candidate face is owned", all(sc["owner"][i] == k for k, it in enumerate(sc["items"], 1) for i in it["faces"])
       and sum(1 for o in sc["owner"] if o) == sum(len(it["faces"]) for it in sc["items"]))


def build(holes=(), blends=(), planes_=()):
    sess, rep = machined.build_selected(part, holes, blends, planes_)
    same = sess.compare().get("converged")
    labels = [o.Label for o in sess.body.Group if o.isDerivedFrom("PartDesign::Feature")]
    App.closeDocument(sess.doc.Name)
    return rep, same, labels


d8 = [items[i]["key"] for i in groups[("hole", 8.0)]["items"]]
rep, same, labels = build(holes=d8)
report("only the D8 holes: their rim chamfer comes along, nothing else", same and rep["holes_made"] == 2 and rep["chamfers_made"] == 2
       and rep["fillets_made"] == 0 and len(rep["auto_added"]) == 2 and any(lb.startswith("Hole_D8") for lb in labels)
       and not any(lb.startswith(("Hole_D5", "Fillet")) for lb in labels), str(labels))
one = items[groups[("fillet", 3.0)]["items"][0]]["faces"][0]
rep, same, labels = build(blends=[one])
report("one fillet face only", same and rep["fillets_made"] == 1 and rep["holes_made"] == 0, str(labels))
top = next(p for p in planes if p["side"] == "-Z")
rep, same, labels = build(planes_=[top["faces"][0]])
report("one flat face only", same and rep["faces_made"] == 1 and rep["holes_made"] == 0 and labels[-2:] == ["Face_1", "Face_1_in"], str(labels))

# ------------------------------------------------------------ the app
tmp = tempfile.mkdtemp(prefix="steprecon_pick_")
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
ids = {(g["kind"], g["size"]): g["items"] for g in s["groups"]}
report("open shows the part as read", code == 200 and s["mode"] == "pick" and "key" not in s["items"][0]
       and max(s["mesh"]["feature"]) == len(s["items"]) and s["info"]["faces"] == len(part.Faces))
report("unknown candidate refused", call("/api/make", {"items": ["zz"]})[0] == 400)
want = ids[("hole", 8.0)] + ids[("fillet", 3.0)][:1]
code, e = call("/api/make", {"items": want})
names = sorted(p["name"] for f in e.get("features", []) for p in f["params"])
report("make: dimensions only for the picked ones", code == 200 and e["mode"] == "edit" and e["info"]["identical"]
       and e["info"]["auto_added"] == 2 and e["selection"] == want and any(n.startswith("Hole_D8") for n in names)
       and any(n.startswith("Fillet_R3") for n in names) and not any(n.startswith("Hole_D5") for n in names), str(names)[:120])
code, pk = call("/api/pick")
report("back to picking keeps the choice", code == 200 and pk["built"] and pk["selection"] == want)
code, e2 = call("/api/set", {"values": {next(n for n in names if n.startswith("Hole_D8") and n.endswith("Diameter1")): 9}})
report("the made dimensions work", code == 200 and not e2["problems"]
       and abs((e["measure"]["volume"] - e2["measure"]["volume"]) / (2 * math.pi * (4.5 ** 2 - 16) * 10) - 1) < 0.05,
       f"{e['measure']['volume'] - e2['measure']['volume']:.1f} mm3 less")
code, e3 = call("/api/state")
report("state stays in edit mode after looking at the pick view", e3["mode"] == "edit")
code, e4 = call("/api/make", {"all": True})
report("everything at once still works", code == 200 and e4["auto"] and e4["info"]["identical"] and e4["selection"] == []
       and e4["info"]["counts"]["holes"] == 3)
server.shutdown()
sys.exit(1 if fails else 0)
