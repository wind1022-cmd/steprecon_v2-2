"""Sketches are fully constrained and dimensioned when a reconstruction is saved, without changing the shape.
Also: face counts ignore how the source CAD split its faces."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze, compare, constrain, fc_tools  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def sketches(doc):
    return [o for o in doc.Objects if o.TypeId == "Sketcher::SketchObject"]


def kinds(sk):
    out = {}
    for c in sk.Constraints:
        out[c.Type] = out.get(c.Type, 0) + 1
    return out


tmp = tempfile.mkdtemp(prefix="steprecon_cons_")

# 1. plate drawn by hand: rectangle, hole, blind pocket
ref = Part.makeBox(40, 30, 10).cut(Part.makeCylinder(4, 10, V(10, 15, 0))).cut(Part.makeBox(12, 8, 3, V(22, 5, 7)))
s = fc_tools.Session(ref)
s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [40, 30]}], 10)
s.hole("XY", 10, 15, diameter=8, through_all=True, offset=10)
s.pocket("XY", [{"type": "rect", "p1": [22, 5], "p2": [34, 13]}], depth=3, offset=10)
free = [sk.DoF for sk in sketches(s.doc) if sk.solve() == 0]
out = s.save(os.path.join(tmp, "plate.FCStd"))
sk0, sk1, sk2 = sketches(s.doc)
report("sketches start free", all(d > 0 for d in free), str(free))
report("save reports all sketches fully constrained", out["sketches"]["fully_constrained"] == 3 == out["sketches"]["sketches"]
       and all(sk.FullyConstrained for sk in sketches(s.doc)), str(out["sketches"]))
k = kinds(sk0)
report("rectangle at the origin: 2 horizontal, 2 vertical, two sides on the axes, width and height",
       k.get("Horizontal") == 2 and k.get("Vertical") == 2 and k.get("PointOnObject") == 2
       and k.get("DistanceX") == 1 and k.get("DistanceY") == 1 and "Distance" not in k, str(k))
vals = sorted(round(c.Value, 6) for c in sk0.Constraints if c.Type in ("DistanceX", "DistanceY"))
report("the two dimensions are the plate size", vals == [30.0, 40.0], str(vals))
k = kinds(sk1)
report("hole: diameter and position of the centre", k.get("Diameter") == 1 and k.get("DistanceX") == 1
       and k.get("DistanceY") == 1, str(k))
vals = sorted(round(c.Value, 6) for c in sk1.Constraints if c.Type in ("Diameter", "DistanceX", "DistanceY"))
report("dimension values are the real ones", vals == [8.0, 10.0, 15.0], str(vals))
report("shape unchanged", s.state()["converged"])

# 2. changing a dimension drives the model
before = s.body.Tip.Shape.Volume
i = next(n for n, c in enumerate(sk1.Constraints) if c.Type == "Diameter")
sk1.setDatum(i, App.Units.Quantity("12 mm"))
s.doc.recompute()
import math  # noqa: E402
report("the sketch diameter drives the Hole feature",
       abs((before - s.body.Tip.Shape.Volume) - math.pi * (36 - 16) * 10) < 1e-3)

# 3. outline taken from a reference face: lines + tangent arcs, on a tilted part
plate = Part.makeBox(50, 30, 6)
plate = plate.makeFillet(6, [e for e in plate.Edges if abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 5])
plate.Placement = App.Placement(V(7, 3, -4), App.Rotation(V(1, 0.5, 0.2), 33))
p = os.path.join(tmp, "round.step")
plate.exportStep(p)
ref = analyze.load_shape(p)
n = App.Rotation(V(1, 0.5, 0.2), 33).multVec(V(0, 0, 1))
top = max((f.Area, i) for i, f in enumerate(ref.Faces) if type(f.Surface).__name__ == "Plane"
          and f.normalAt(*f.Surface.parameter(f.CenterOfMass)).getAngle(n) < 1e-6)[1]
s = fc_tools.Session(ref)
s.pad(face=top, length=6)
out = s.save(os.path.join(tmp, "round.FCStd"))
sk = sketches(s.doc)[0]
k = kinds(sk)
report("face outline: fully constrained with 8 tangent joints, one radius and 3 equal arcs",
       sk.FullyConstrained and k.get("Tangent") == 8 and k.get("Radius") == 1 and k.get("Equal") == 3
       and "Coincident" not in k, str(k))
i = next(n for n, c in enumerate(sk.Constraints) if c.Type == "Radius")
sk.setDatum(i, App.Units.Quantity("9 mm"))
s.doc.recompute()
report("one radius dimension drives all four corners", s.body.Tip.isValid()
       and abs(s.body.Tip.Shape.Volume - (50 * 30 - (4 - math.pi) * 81) * 6) < 1e-3, f"vol={s.body.Tip.Shape.Volume:.3f}")
sk.setDatum(i, App.Units.Quantity("6 mm"))
s.doc.recompute()
report("tilted outline keeps its shape (after setting the radius back)", s.state()["converged"], f"xor={s.state()['xor_pct']}")

# 4. already constrained sketches are left alone; saving twice adds nothing
again = constrain.constrain_document(s.doc)
report("second pass adds nothing", again["dimensions"] == 0 and again["geometric"] == 0 and again["fully_constrained"] == 1)

# 5. constrain=False keeps the old behaviour
s = fc_tools.Session(Part.makeBox(10, 10, 10))
s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [10, 10]}], 10)
out = s.save(os.path.join(tmp, "free.FCStd"), constrain=False)
report("constrain=False leaves the sketch free", "sketches" not in out and not sketches(s.doc)[0].FullyConstrained)

# 6. a cylinder stored as two half faces (usual in STEP files from other CAD systems) counts as one face
half = Part.makeCylinder(4, 10, V(20, 15, 0), V(0, 0, 1), 180)
other = half.copy()
other.rotate(V(20, 15, 0), V(0, 0, 1), 180)
split = Part.makeBox(40, 30, 10).cut(half).cut(other)
whole = Part.makeBox(40, 30, 10).cut(Part.makeCylinder(4, 10, V(20, 15, 0)))
r = compare.compare(split, whole)
report("split faces do not block convergence", len(split.Faces) > len(whole.Faces) and r["converged"]
       and r["ref_faces"] == r["new_faces"] == 7, f"raw {len(split.Faces)} vs {len(whole.Faces)}, counted {r['ref_faces']}/{r['new_faces']}")

sys.exit(1 if fails else 0)
