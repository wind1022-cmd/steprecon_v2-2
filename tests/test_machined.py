"""machined.py: holes of a complex part become dimensioned features on top of a frozen blank, without any model."""
import csv
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze, machined  # noqa: E402
import machined as cli  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def housing():
    """A stand-in for a cast housing: lofted (non-prismatic) body with a flange, then machined:
    main bore with a counterbore, six flange holes on a bolt circle, a blind hole from the side, one inclined hole."""
    import math
    a = Part.makePolygon([V(-30, -30, 0), V(30, -30, 0), V(30, 30, 0), V(-30, 30, 0), V(-30, -30, 0)])
    b = Part.Wire(Part.makeCircle(22, V(0, 0, 40)))
    body = Part.makeLoft([Part.Wire(a.Edges), b], True, False)
    body = body.fuse(Part.makeCylinder(45, 8)).removeSplitter()
    cut = [Part.makeCylinder(10, 48, V(0, 0, -4)),                       # main bore, through
           Part.makeCylinder(15, 6, V(0, 0, 34))]                        # counterbore at the top
    for k in range(6):
        ang = math.radians(60 * k + 15)
        cut.append(Part.makeCylinder(3, 8, V(38 * math.cos(ang), 38 * math.sin(ang), 0)))
    cut.append(Part.makeCylinder(2.5, 12, V(46, 0, 4), V(-1, 0, 0)))     # blind, from the flange rim
    cut.append(Part.makeCylinder(2, 40, V(-40, 0, 30), V(1, 0, -0.5)))   # inclined
    for c in cut:
        body = body.cut(c)
    return body


tmp = tempfile.mkdtemp(prefix="steprecon_mach_")
path = os.path.join(tmp, "housing.step")
housing().exportStep(path)
ref = analyze.load_shape(path)
report("the test part has freeform faces (not rebuildable feature by feature)",
       any(type(f.Surface).__name__ not in analyze.ANALYTIC for f in ref.Faces))

holes = machined.find_holes(ref)
radii = sorted(round(h["radius"], 3) for h in holes)
report("holes recognised", radii.count(3.0) == 6 and 15.0 in radii and 2.5 in radii and 2.0 in radii and 10.0 in radii, str(radii))

sess, rep = machined.build(ref)
cmp_ = sess.compare()
report("at least 9 of the 10 holes became features", rep["holes_made"] >= 9, f"{rep['holes_made']} made, skipped={rep['skipped']}")
report("result identical to the reference", cmp_["converged"] and cmp_["xor_pct"] < 1e-3,
       f"xor={cmp_['xor_pct']} faces={cmp_['new_faces']}/{cmp_['ref_faces']} skipped={rep['skipped']}")
labels = [o.Label for o in sess.body.Group if o.TypeId == "PartDesign::Pocket"]
# four flange holes are open on both sides; two sit under the body and are blind from below: two features
report("equal holes drilled the same way share a feature", "Hole_D6_x4" in labels and "Hole_D6_x2" in labels
       and rep["holes_made"] + len(rep["skipped"]) == len(holes), str(labels))
blank = sess.body.BaseFeature.Shape
report("blank has the holes filled", blank.Volume > ref.Volume and len(blank.Solids) == 1)

out = sess.save(os.path.join(tmp, "housing.FCStd"))
report("every hole sketch is fully constrained", out["sketches"]["fully_constrained"] == out["sketches"]["sketches"] > 0,
       str(out["sketches"]))

# the flange holes are now a parameter: change the diameter 6 -> 8
pocket = next(o for o in sess.body.Group if o.Label == "Hole_D6_x4")
sk = pocket.Profile[0]
idx = [i for i, c in enumerate(sk.Constraints) if c.Type == "Diameter"]
v0 = sess.body.Tip.Shape.Volume
for i in idx:
    sk.setDatum(i, App.Units.Quantity("8 mm"))
sess.doc.recompute()
import math  # noqa: E402
report("one diameter dimension drives the four equal holes", len(idx) == 1 and sess.body.Tip.isValid()
       and abs((v0 - sess.body.Tip.Shape.Volume) - 4 * math.pi * (16 - 9) * 8) < 0.05,
       f"removed {v0 - sess.body.Tip.Shape.Volume:.3f}")

# a part without holes: the blank is the reference itself
s2, r2 = machined.build(Part.makeBox(10, 10, 10))
report("no holes -> blank only, still identical", r2["holes_made"] == 0 and s2.compare()["converged"])

# a pin standing in a bore is not a drilled hole
ring = Part.makeBox(30, 30, 10).cut(Part.makeCylinder(8, 10, V(15, 15, 0))).fuse(Part.makeCylinder(3, 10, V(15, 15, 0)))
ring = ring.fuse(Part.makeBox(30, 30, 2, V(0, 0, -2)))
s3, r3 = machined.build(ring)
report("bore with a pin inside is left in the blank", r3["holes_made"] == 0 and len(r3["skipped"]) == 1
       and s3.compare()["converged"], str(r3["skipped"]))

# command line
sys.argv = ["machined.py", path, "--out", os.path.join(tmp, "out")]
cli.main()
with open(os.path.join(tmp, "out", "summary.csv"), encoding="utf-8-sig") as f:
    row = list(csv.DictReader(f))[0]
report("command line writes model and summary", os.path.exists(os.path.join(tmp, "out", "housing.FCStd"))
       and row["identical"] == "True" and int(row["holes"]) == rep["holes_made"], str(row))

sys.exit(1 if fails else 0)
