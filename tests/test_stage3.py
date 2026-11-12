import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import math
import FreeCAD as App
import Part
from step_recon import fc_tools


def face_with_normal(s, n):
    for f in s.list_faces(only_type="Plane")["faces"]:
        if all(abs(a - b) < 1e-3 for a, b in zip(f["normal"], n)):
            return f["face"]
    raise ValueError("no face with normal %s" % (n,))


def check(name, ref, build):
    s = fc_tools.Session(ref)
    try:
        build(s)
        c = s.compare()
        ok = c["xor_pct"] < 0.1 and c["ref_faces"] == c["new_faces"]
        print(f"{name}: xor={c['xor_pct']}% faces {c['ref_faces']}/{c['new_faces']} -> {'PASS' if ok else 'FAIL'}")
        if not ok:
            print("   ", {k: v for k, v in c.items() if "region" in k or k.endswith("types") or "volume" in k})
    except Exception as e:
        print(f"{name}: EXC {type(e).__name__}: {e}")


# 1) shell: open-top tray, wall 2
ref = Part.makeBox(40, 30, 20).cut(Part.makeBox(36, 26, 20, App.Vector(2, 2, 2)))


def b1(s):
    print(s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [40, 30]}], 20))
    top = face_with_normal(s, (0, 0, 1))
    print(s.shell([top], 2))


check("shell", ref, b1)

# 2) draft: 20x20x10 block, 5 degrees on the four side faces, neutral = bottom
d = 10 * math.tan(math.radians(5))
bottom = Part.Face(Part.makePolygon([App.Vector(0, 0, 0), App.Vector(20, 0, 0), App.Vector(20, 20, 0), App.Vector(0, 20, 0), App.Vector(0, 0, 0)]))
top = Part.Face(Part.makePolygon([App.Vector(d, d, 10), App.Vector(20 - d, d, 10), App.Vector(20 - d, 20 - d, 10),
                                  App.Vector(d, 20 - d, 10), App.Vector(d, d, 10)]))
ref = Part.makeLoft([bottom.OuterWire, top.OuterWire], True)


def b2(s):
    s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [20, 20]}], 10)
    sides = [f["face"] for f in s.list_faces(only_type="Plane")["faces"] if abs(f["normal"][2]) < 1e-3]
    neutral = face_with_normal(s, (0, 0, -1))
    print(s.draft(sides, 5, neutral, "Z"))


check("draft", ref, b2)

# 3) rib: 40x40x3 plate with a 2 mm thick, 10 mm tall web
ref = Part.makeBox(40, 40, 3).fuse(Part.makeBox(40, 2, 10, App.Vector(0, 19, 3))).removeSplitter()


def b3(s):
    s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [40, 40]}], 3)
    print(s.rib("XY", [0, 20], [40, 20], 2, 10, offset=3))


check("rib", ref, b3)
