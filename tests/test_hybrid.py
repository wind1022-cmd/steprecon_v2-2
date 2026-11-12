import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import FreeCAD as App
import Part
from step_recon import fc_tools

# Reference: 80x50x10 plate with a freeform (lofted B-spline) knob on top and a through hole.
c1 = Part.Wire(Part.makeCircle(12, App.Vector(60, 25, 10)))
c2 = Part.Wire(Part.makeCircle(6, App.Vector(63, 25, 24)))
ref = Part.makeBox(80, 50, 10).fuse(Part.makeLoft([c1, c2], True))
ref = ref.cut(Part.makeCylinder(3, 12, App.Vector(15, 25, -1))).removeSplitter()
print("ref faces", len(ref.Faces), {type(f.Surface).__name__ for f in ref.Faces})

s = fc_tools.Session(ref)
print("freeform bbox", s.freeform_bbox())
print(s.import_freeform(margin=0.5))
c = s.compare()
print("after import: xor", c["xor_pct"], "missing regions:", c.get("missing_regions"))

# remaining analytic work: base plate and hole
print(s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [80, 50]}], 10))
print(s.hole("XY", 15, 25, 6, through_all=True, offset=10))
c = s.compare()
print({k: v for k, v in c.items() if "region" not in k})
print("features", s.log)

# undo of the import must work too
for _ in range(3):
    print(s.undo())
print("tip after undo-all:", s._tip_shape())

assert c['converged'] and s._tip_shape() is None, 'hybrid failed'
print('HYBRID OK')
