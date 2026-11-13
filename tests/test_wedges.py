"""Chamfers along the teeth of a gear that has a step (a flange) - so it is not a plain extrusion: the strips are
removed by filling the wedge each one cut off, and can be made a parameter. Seen on a user's part, where neither
worked: the kernel's defeaturing gave up on them."""
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
W, Z = 14.0, 12


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


teeth = Part.Face(profile(Z, 36.0, 40.0)).extrude(V(0, 0, W))
flange = Part.makeCylinder(43.0, 3.0)                              # the step: the teeth run into a flange
sharp = teeth.fuse(flange).removeSplitter().cut(Part.makeCylinder(12, W))
# the edges along the teeth between tip land and flank, above the flange
tip_edges = [e for e in sharp.Edges if type(e.Curve).__name__ == "Line" and abs(e.Vertexes[0].Point.z - e.Vertexes[1].Point.z) > W - 3 - 1e-6
             and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - 40.0) < 1e-6]
gear = sharp.makeChamfer(0.4, tip_edges)
tmp = tempfile.mkdtemp(prefix="steprecon_wedge_")
path = os.path.join(tmp, "gear.stp")
gear.exportStep(path)
ref = analyze.load_shape(path)
print(f"{len(ref.Faces)} faces, {len(tip_edges)} chamfered edges, volume {ref.Volume:.3f} (sharp {sharp.Volume:.3f})")

sc = machined.scan(ref)
cham = [i["faces"][0] for i in sc["items"] if i["kind"] == "chamfer"]
report("the strips along the teeth are found as chamfers", len(cham) == len(tip_edges) == 2 * Z
       and {g["size"] for g in sc["groups"] if g["kind"] == "chamfer"} == {0.4}, f"{len(cham)} faces")
new, info = sharpen.rebuild_prism(ref, cham)
report("the outline route does not apply (step in the part)", new is None)

t0 = time.time()
res = machined.remove_faces_ex(ref, cham)
dt = time.time() - t0
out = res["shape"]
report("removed by filling the wedges: exactly the sharp part", res["method"] == "wedges" and res["failed"] == 0 and out.isValid()
       and abs(out.Volume - sharp.Volume) < 1e-6 and len(out.Faces) == len(sharp.Faces), f"{dt:.1f}s {res['method']} "
       f"{out.Volume - sharp.Volume:+.6f} mm3, {len(out.Faces)} faces")
res = machined.remove_faces_ex(ref, cham[:3])
report("a few of them only", res["method"] == "wedges" and res["done"] == 3 and len(res["shape"].Faces) == len(ref.Faces) - 3)

# a chamfer in a hollow corner (material has to be taken away again)
blk = Part.makeBox(30, 20, 10).cut(Part.makeBox(30, 10, 5, V(0, 10, 5)))
inner = [e for e in blk.Edges if type(e.Curve).__name__ == "Line" and all(abs(v.Point.y - 10) < 1e-9 and abs(v.Point.z - 5) < 1e-9 for v in e.Vertexes)]
hollow = blk.makeChamfer(1.0, inner)
strip = [i for i, f in enumerate(hollow.Faces) if type(f.Surface).__name__ == "Plane" and 0.1 < abs(f.Surface.Axis.y) < 0.9]
new, gone = sharpen.fill_wedges(hollow, strip)
report("hollow corner: the wedge is cut away", new is not None and abs(new.Volume - blk.Volume) < 1e-6 and len(gone) == 1)

# ------------------------------------------------------------ as a parameter
t0 = time.time()
sess, rep = machined.build_selected(ref, [], cham, [])
c = sess.compare()
report("the chamfers become a parameter, the result equals the file", (c.get("converged") or c.get("near")) and rep["chamfers_made"] == len(cham),
       f"{time.time() - t0:.1f}s chamfer edges {rep['chamfers_made']} skipped={rep['skipped'][:1]}")
model = os.path.join(tmp, "g.FCStd")
sess.save(model)
App.closeDocument(sess.doc.Name)
doc = App.openDocument(model)
plist = P.collect(doc)
size = next(p["name"] for p in plist if p["name"].startswith("Chamfer") and p["name"].endswith(".Size"))
v0 = P.result_shape(doc).Volume
P.apply(doc, plist, {size: 0.8})
doc.recompute()
s = P.result_shape(doc)
report("chamfer 0.4 -> 0.8 rebuilds a valid, smaller part", not P.problems(doc) and s.isValid() and s.Volume < v0 - 1.0, f"{s.Volume - v0:+.2f} mm3")
sys.exit(1 if fails else 0)
