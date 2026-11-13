"""Hole rims: a chamfer with two different distances is recognised and follows a diameter change; a rim shape that
is not recognised is reported instead of silently staying behind (seen on a user's part: ring with a chamfered bore)."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import machined, params as P  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def ring(radial, axial):
    """Thin ring with four lugs, D30 through, D34 x 4 from the top, the D34 rim bevelled radial x axial."""
    r = Part.makeCylinder(25, 5)
    for k in range(4):
        lug = Part.makeBox(16, 40, 5, V(-8, 0, 0))
        lug.rotate(V(0, 0, 0), V(0, 0, 1), 90 * k + 45)
        r = r.fuse(lug)
    r = r.common(Part.makeCylinder(34.5, 5)).removeSplitter()
    r = r.cut(Part.makeCylinder(15, 5)).cut(Part.makeCylinder(17, 4, V(0, 0, 1))).removeSplitter()
    return r.cut(Part.makeCone(17, 17 + radial, axial, V(0, 0, 5 - axial))).removeSplitter()


def pick_d34(part):
    sc = machined.scan(part)
    d34 = next(it for it in sc["items"] if it["kind"] == "hole" and abs(it["size"] - 34) < 1e-6)
    group = next(g for g in sc["groups"] if d34["id"] in g["items"])
    sess, rep = machined.build_selected(part, [d34["key"]], [], [])
    same = sess.compare().get("converged")
    sess.save(os.path.join(tempfile.mkdtemp(), "r.FCStd"))
    plist = P.collect(sess.doc)
    return sc, group, sess, rep, same, plist


def cone_radii_after(sess, plist, dia):
    name = next(p["name"] for p in plist if p["name"].startswith("Hole_D34") and p["name"].endswith("Diameter1"))
    P.apply(sess.doc, plist, {name: dia})
    sess.doc.recompute()
    s = P.result_shape(sess.doc)
    return P.problems(sess.doc), [sorted(round(e.Curve.Radius, 3) for e in f.Edges if type(e.Curve).__name__ == "Circle")
                                  for f in s.Faces if type(f.Surface).__name__ == "Cone"]


for radial, axial, label in ((0.5, 0.5, "equal 0.5"), (1.0, 0.5, "1 x 0.5"), (0.577, 1.0, "30 degrees")):
    sc, group, sess, rep, same, plist = pick_d34(ring(radial, axial))
    ch = [g for g in sc["groups"] if g["kind"] == "chamfer"]
    names = [p["name"] for p in plist]
    bad, cones = cone_radii_after(sess, plist, 38)
    want = [[19.0, round(19 + radial, 3)]]
    two = abs(radial - axial) > 0.01
    report(f"rim chamfer {label}: recognised, made with the hole, follows D34 -> 38",
           same and len(ch) == 1 and (("size2" in ch[0]) == two) and rep["chamfers_made"] == 1 and len(rep["auto_added"]) == 1
           and not rep["warnings"] and (any(n.endswith("Size2") for n in names) == two) and not bad and cones == want,
           f"{[(g['size'], g.get('size2')) for g in ch]} {names} {cones}")
    App.closeDocument(sess.doc.Name)

sc, group, sess, rep, same, plist = pick_d34(ring(3.0, 0.2))        # 86 degrees: not taken for a chamfer
report("unrecognised rim: shown before picking and reported after making",
       same and group.get("rim_unknown") == 1 and len(rep["warnings"]) == 1 and rep["warnings"][0]["hole"] == 34.0,
       str(rep["warnings"]))
App.closeDocument(sess.doc.Name)
sys.exit(1 if fails else 0)
