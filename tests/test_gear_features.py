"""A gear-like part: outer teeth and an inner spline, rounds along the axis at every corner of the outline and chamfers
(two distances) around both ends. Hundreds of small faces: removing them and making them parameters has to be exact
and take seconds (the kernel's defeaturing took minutes and returned a broken solid; see sharpen.py)."""
import math
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze, machined, params as P, sharpen  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector
W = 12.0


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def profile(z, r_root, r_tip):
    pts = []
    d = 2 * math.pi / z
    for i in range(z):
        a = i * d
        for t, r in ((0.0, r_root), (0.22, r_root), (0.36, r_tip), (0.64, r_tip), (0.78, r_root)):
            pts.append(V(r * math.cos(a + t * d), r * math.sin(a + t * d), 0))
    pts.append(pts[0])
    return Part.makePolygon(pts)


sharp = Part.Face(profile(10, 36.0, 40.0)).extrude(V(0, 0, W)).cut(Part.Face(profile(6, 14.0, 12.0)).extrude(V(0, 0, W)))
vert = [e for e in sharp.Edges if type(e.Curve).__name__ == "Line" and abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > W - 1e-6]
rounded = sharp.makeFillet(0.6, vert)
ends = [e for e in rounded.Edges if e.BoundBox.ZLength < 1e-6 and (abs(e.BoundBox.ZMin) < 1e-6 or abs(e.BoundBox.ZMin - W) < 1e-6)]
gear = rounded.makeChamfer(0.3, 0.2, ends)
tmp = tempfile.mkdtemp(prefix="steprecon_gear_")
path = os.path.join(tmp, "gear.stp")
gear.exportStep(path)
ref = analyze.load_shape(path)
print(f"gear: {len(ref.Faces)} faces (sharp {len(sharp.Faces)}, rounded {len(rounded.Faces)})")

t0 = time.time()
sc = machined.scan(ref)
dt = time.time() - t0
kinds = {(g["kind"], g["size"], g.get("size2")) for g in sc["groups"]}
report("scan finds the rounds and the two-distance chamfers, in seconds", ("fillet", 0.6, None) in kinds
       and any(k[0] == "chamfer" for k in kinds) and dt < 15, f"{dt:.1f}s {sorted(kinds, key=str)}")
fil = [i["faces"][0] for i in sc["items"] if i["kind"] == "fillet"]
cham = [i["faces"][0] for i in sc["items"] if i["kind"] == "chamfer"]

# ------------------------------------------------------------ removal
t0 = time.time()
res = machined.remove_faces_ex(ref, fil + cham)
dt = time.time() - t0
new = res["shape"]
report("remove every round and chamfer: outline route, exactly the sharp part", res["method"] == "outline" and res["failed"] == 0
       and new.isValid() and abs(new.Volume - sharp.Volume) < 1e-6 and len(new.Faces) == len(sharp.Faces), f"{dt:.1f}s {res['method']} added {res['added']}")
res = machined.remove_faces_ex(ref, cham)
report("only the chamfers: the rounds stay, the ends are sharp", res["method"] == "outline" and abs(res["shape"].Volume - rounded.Volume) < 1e-6
       and len(res["shape"].Faces) == len(rounded.Faces), f"{len(res['shape'].Faces)} faces vs {len(rounded.Faces)}")
res = machined.remove_faces_ex(ref, fil)
report("only the rounds: the chamfers on them have to go too (reported)", res["method"] == "outline" and res["added"] > 0
       and abs(res["shape"].Volume - sharp.Volume) < 1e-6, f"added {res['added']}")

# the kernel's answer is checked: a blown-up solid is refused
big = Part.makeBox(10, 10, 10)
report("a broken result of the kernel is recognised", not machined._sane(big, Part.makeBox(30, 30, 30))
       and machined._sane(big, Part.makeBox(10, 10, 9)) and not machined._sane(big, Part.makeBox(10, 10, 10).translated(V(50, 0, 0))
                                                                              if hasattr(Part.Shape, "translated") else Part.makeBox(1, 1, 1, V(50, 0, 0))))

# a part that is not such an extrusion is left alone
boss = Part.makeBox(40, 30, 10).fuse(Part.makeCylinder(8, 12, V(20, 15, 10))).removeSplitter()
rounded_boss = boss.makeFillet(1, [e for e in boss.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Curve.Radius - 8) < 1e-6])
new, info = sharpen.rebuild_prism(rounded_boss, [i for i, f in enumerate(rounded_boss.Faces) if type(f.Surface).__name__ == "Toroid"])
report("a boss in the middle is not mistaken for the rim of an end", new is None, str(info)[:60])

# ------------------------------------------------------------ dimensions
t0 = time.time()
sess, rep = machined.build_selected(ref, [], fil, [])
dt = time.time() - t0
c = sess.compare()
report("dimensions for the rounds (their chamfers come along): the same as the file, or near enough", (c.get("converged") or c.get("near")) and rep["fillets_made"] > 0
       and rep["chamfers_made"] > 0 and len(rep["auto_added"]) > 0, f"{dt:.1f}s {rep['fillets_made']} fillet edges, "
       f"{rep['chamfers_made']} chamfer edges, xor {c.get('xor_pct')} ({c.get('xor_method')})")
model = os.path.join(tmp, "gear.FCStd")
sess.save(model)
App.closeDocument(sess.doc.Name)
doc = App.openDocument(model)
plist = P.collect(doc)
names = [p["name"] for p in plist]
rad = next(n for n in names if n.startswith("Fillet_R") and n.endswith(".Radius"))
v0 = P.result_shape(doc).Volume
t0 = time.time()
P.apply(doc, plist, {rad: 0.9})
doc.recompute()
s = P.result_shape(doc)
report("radius 0.6 -> 0.9 rebuilds a valid part", not P.problems(doc) and s.isValid() and abs(s.Volume - v0) > 1e-6, f"{time.time() - t0:.1f}s {names}")
sys.exit(1 if fails else 0)
