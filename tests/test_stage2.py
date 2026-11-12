import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import math
import FreeCAD as App
import Part
from step_recon import fc_tools


def check(name, ref, build):
    s = fc_tools.Session(ref)
    try:
        build(s)
        c = s.compare()
        ok = c["xor_pct"] < 0.1 and c["ref_faces"] == c["new_faces"]
        print(f"{name}: xor={c['xor_pct']}% faces {c['ref_faces']}/{c['new_faces']} -> {'PASS' if ok else 'FAIL'}")
        if not ok:
            print("   ", {k: v for k, v in c.items() if "region" in k or k.endswith("types")})
    except Exception as e:
        print(f"{name}: EXC {type(e).__name__}: {e}")


# 1) flange + shaft revolved, 4 holes via polar pattern
ref = Part.makeCylinder(20, 10).fuse(Part.makeCylinder(12, 40))
for i in range(4):
    a = math.radians(90 * i)
    ref = ref.cut(Part.makeCylinder(2, 12, App.Vector(16 * math.cos(a), 16 * math.sin(a), -1)))
ref = ref.removeSplitter()


def b1(s):
    r = s.revolution("XZ", [{"type": "line", "p1": [0, 0], "p2": [20, 0]}, {"type": "line", "p1": [20, 0], "p2": [20, 10]},
                            {"type": "line", "p1": [20, 10], "p2": [12, 10]}, {"type": "line", "p1": [12, 10], "p2": [12, 40]},
                            {"type": "line", "p1": [12, 40], "p2": [0, 40]}, {"type": "line", "p1": [0, 40], "p2": [0, 0]}], axis="V")
    print(r)
    print(s.hole("XY", 16, 0, 4, depth=10, offset=10))
    print(s.polar_pattern(1, "Z", 4))


check("revolve+polar", ref, b1)

# 2) plate with 3 pockets via linear pattern, mirrored boss
ref = Part.makeBox(60, 30, 8)
for i in range(3):
    ref = ref.cut(Part.makeBox(8, 10, 4, App.Vector(6 + 20 * i, 10, 4)))


def b2(s):
    s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [60, 30]}], 8)
    s.pocket("XY", [{"type": "rect", "p1": [6, 10], "p2": [14, 20]}], depth=4, offset=8)
    print(s.linear_pattern(1, "X", 40, 3))


check("linear_pattern", ref, b2)

# 3) mirror: block + hole at x=10 mirrored across YZ => hole at x=-10
ref = Part.makeBox(40, 20, 10, App.Vector(-20, 0, 0)).cut(Part.makeCylinder(3, 12, App.Vector(10, 10, -1))).cut(
    Part.makeCylinder(3, 12, App.Vector(-10, 10, -1)))


def b3(s):
    s.pad("XY", [{"type": "rect", "p1": [-20, 0], "p2": [20, 20]}], 10)
    s.hole("XY", 10, 10, 6, through_all=True, offset=10)
    print(s.mirror(1, "YZ"))


check("mirror", ref, b3)

# 4) arbitrary plane: pad on a plane tilted 30 deg about X, anchored at origin
n = App.Vector(0, -math.sin(math.radians(30)), math.cos(math.radians(30)))
ref = Part.makeBox(20, 10, 5)
ref.rotate(App.Vector(0, 0, 0), App.Vector(1, 0, 0), 30)


def b4(s):
    s.pad({"origin": [0, 0, 0], "normal": [0, -math.sin(math.radians(30)), math.cos(math.radians(30))], "x_dir": [1, 0, 0]},
          [{"type": "rect", "p1": [0, 0], "p2": [20, 10]}], 5)


check("tilted_plane", ref, b4)
