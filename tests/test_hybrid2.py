import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import FreeCAD as App
import Part
from step_recon import fc_tools


def knob(x):
    c1 = Part.Wire(Part.makeCircle(8, App.Vector(x, 20, 10)))
    c2 = Part.Wire(Part.makeCircle(4, App.Vector(x + 2, 20, 22)))
    return Part.makeLoft([c1, c2], True)


# 140x40x10 plate, freeform knobs at x=20 and x=120, hole and a pocket in the middle (analytic)
ref = Part.makeBox(140, 40, 10).fuse(knob(20)).fuse(knob(120))
ref = ref.cut(Part.makeCylinder(4, 12, App.Vector(70, 20, -1))).cut(Part.makeBox(20, 10, 4, App.Vector(60, 5, 6))).removeSplitter()
print("ref faces", len(ref.Faces), "volume", round(ref.Volume, 1))


def run(label, **kw):
    s = fc_tools.Session(ref)
    r = s.import_freeform(**kw)
    print(f"[{label}] ->", {k: v for k, v in r.items() if k != "box_list"})
    if not r.get("ok"):
        return
    p1 = s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [140, 40]}], 10)
    p2 = s.pocket("XY", [{"type": "rect", "p1": [60, 5], "p2": [80, 15]}], depth=4, offset=10)
    p3 = s.hole("XY", 70, 20, 8, through_all=True, offset=10)
    print("   pad", p1.get("ok"), "pocket", p2.get("ok"), "hole", p3.get("ok"), p3.get("error", ""))
    c = s.compare()
    print("   compare:", {k: c[k] for k in ("xor_pct", "xor_method", "ref_faces", "new_faces", "converged")})
    print("   undo chain:", [s.undo().get("ok") for _ in range(4)], "tip:", s._tip_shape())
    return c["converged"], r["imported_fraction_pct"]


single = run("single big box", split=False)
split = run("split per cluster", split=True)
partial = run("only largest cluster", split=True, clusters=[0])
assert single[0] and split[0], "full imports must converge"
assert split[1] < single[1], "per-cluster import should freeze less volume than one big box"
assert not partial[0], "importing only one of two knobs must leave a visible difference"
print("HYBRID2 OK")

