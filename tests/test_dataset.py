"""Part classifier: prismatic / revolved / freeform / mixed detection on generated parts."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402
from step_recon import dataset, synth  # noqa: E402

d = tempfile.mkdtemp(prefix="steprecon_cls_")
synth.generate(d, n=3)
Part.makeCylinder(20, 10).fuse(Part.makeCylinder(12, 40)).exportStep(os.path.join(d, "rev_flange.step"))
Part.makeCone(10, 5, 20).fuse(Part.makeCylinder(10, 5, App.Vector(0, 0, -5))).exportStep(os.path.join(d, "rev_cone.step"))
Part.makeSphere(10).exportStep(os.path.join(d, "sphere.step"))
c1 = Part.Wire(Part.makeCircle(10, App.Vector(0, 0, 0)))
c2 = Part.Wire(Part.makeCircle(5, App.Vector(3, 0, 20)))
Part.makeLoft([c1, c2], True).exportStep(os.path.join(d, "loft_freeform.step"))
Part.makeBox(30, 30, 30).cut(Part.makeCylinder(4, 40, App.Vector(15, 15, -5))).cut(
    Part.makeCylinder(3, 40, App.Vector(-5, 15, 15), App.Vector(1, 0, 0))).exportStep(os.path.join(d, "cross_holes.step"))

got = {os.path.basename(r["path"]): r["category"] for r in dataset.scan(d)}
for k, v in sorted(got.items()):
    print(f"{k:22s} {v}")
expected = {"rev_flange.step": "revolved", "rev_cone.step": "revolved", "loft_freeform.step": "freeform",
            "cross_holes.step": "mixed", "sphere.step": "mixed", "synth_000.step": "prismatic",
            "synth_001.step": "prismatic", "synth_002.step": "prismatic"}
bad = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
assert not bad, f"misclassified (got, expected): {bad}"
print("DATASET OK")
