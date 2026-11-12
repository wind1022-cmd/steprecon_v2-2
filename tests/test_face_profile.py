"""pad/pocket with face=<id>: reuse the exact outline of a planar face of the reference instead of making the
model retype coordinates. Seen on a real part (ring with four arc-ended lugs): from the face list alone the model
built a plain disc with invented holes."""
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import agent, analyze, fc_tools  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def lug_ring(chamfer=False):
    """Ring (R 30, bore 18, 5 thick) with four lugs reaching R 45, lug sides rounded."""
    body = Part.makeCylinder(30, 5)
    lugs = []
    for k in range(4):
        box = Part.makeBox(30, 20, 5, V(20, -10, 0))
        box = box.common(Part.makeCylinder(45, 5))                  # arc-shaped end
        box.rotate(V(0, 0, 0), V(0, 0, 1), 90 * k + 20)
        lugs.append(box)
    s = body.fuse(lugs).removeSplitter()
    vertical = [e for e in s.Edges if type(e.Curve).__name__ == "Line"
                and abs(e.valueAt(e.FirstParameter).z - e.valueAt(e.LastParameter).z) > 4.9]
    s = s.makeFillet(1.5, vertical)
    s = s.cut(Part.makeCylinder(18, 5))
    if chamfer:
        top = [e for e in s.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Curve.Radius - 18) < 1e-6
               and abs(e.valueAt(e.FirstParameter).z - 5) < 1e-6]
        s = s.makeChamfer(1.0, top)
    return s


def roundtrip(shape, name):
    path = os.path.join(tmp, name + ".step")
    shape.exportStep(path)
    return path, analyze.load_shape(path)


def plane_face(ref, normal, pick=max):
    """Index of the largest (or smallest) planar face with the given outward normal."""
    c = []
    for i, f in enumerate(ref.Faces):
        if type(f.Surface).__name__ == "Plane":
            pt, u, v = analyze._face_point(f)
            if f.normalAt(u, v).getAngle(normal) < 1e-6:
                c.append((f.Area, i))
    return pick(c)[1]


tmp = tempfile.mkdtemp(prefix="steprecon_face_")

# 1. the whole lug ring from one call
path, ref = roundtrip(lug_ring(), "lugs")
top = plane_face(ref, V(0, 0, 1))
info = analyze.analyze(path)["faces"][top]
report("summary describes the outline", info.get("outline", {}).get("arcs", 0) >= 8 and info.get("holes") == 1
       and abs(info.get("max_depth") - 5) < 1e-6, str({k: info.get(k) for k in ("outline", "holes", "max_depth")}))
s = fc_tools.Session(ref)
r = s.pad(face=top, length=5, holes=True)
st = s.state()
report("pad face=top holes=true rebuilds the part", r["ok"] and st["converged"], f"xor={st['xor_pct']} faces={st['faces']}/{st['target_faces']}")

# 2. designer-like: outline only, then the bore from the same face's inner loop
s = fc_tools.Session(ref)
a = s.pad(face=top, length=5)
b = s.pocket(face=top, through_all=True)
report("pad face (outer) + pocket face (inner loops)", a["ok"] and b["ok"] and s.state()["converged"], str(s.log))

# 3. same part tilted and moved: no base plane fits, the face still does
tilted = lug_ring()
tilted.Placement = App.Placement(V(12, -7, 30), App.Rotation(V(1, 1, 0), 37))
path, ref = roundtrip(tilted, "tilted")
n = App.Rotation(V(1, 1, 0), 37).multVec(V(0, 0, 1))
s = fc_tools.Session(ref)
r = s.pad(face=plane_face(ref, n), length=5, holes=True)
report("works on a tilted part", r["ok"] and s.state()["converged"], f"xor={s.state()['xor_pct']}")

# 4. chamfered bore: the top face's inner loop is the chamfer rim, so outline first, then hole + chamfer
path, ref = roundtrip(lug_ring(chamfer=True), "chamfer")
s = fc_tools.Session(ref)
s.pad(face=plane_face(ref, V(0, 0, 1)), length=5)
s.hole("XY", 0, 0, diameter=36, through_all=True, offset=5)
rim = [e["edge"] for e in s.list_edges(only_type="Circle")["edges"]
       if abs(e.get("radius", 0) - 18) < 1e-6 and abs(e["p1"][2] - 5) < 1e-6]
s.chamfer(rim, 1.0)
st = s.state()
report("outline + hole + chamfer", st["converged"], f"xor={st['xor_pct']} faces={st['faces']}/{st['target_faces']}")

# 5. blind recess: its floor face, cut outward
plate = Part.makeBox(40, 30, 10).cut(Part.makeBox(20, 10, 4, V(10, 10, 6)))
path, ref = roundtrip(plate, "recess")
s = fc_tools.Session(ref)
s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [40, 30]}], 10)
floor = plane_face(ref, V(0, 0, 1), pick=min)
r = s.pocket(face=floor, loops="outer", depth=4)
report("pocket face=floor loops=outer", r["ok"] and s.state()["converged"], f"xor={s.state()['xor_pct']}")

# 6. errors
s = fc_tools.Session(ref)
cyl = next(i for i, f in enumerate(lug_ring().Faces) if type(f.Surface).__name__ != "Plane")
for label, call in (("non-planar face", lambda: fc_tools.Session(lug_ring()).pad(face=cyl, length=5)),
                    ("face out of range", lambda: s.pad(face=999, length=5)),
                    ("neither face nor plane", lambda: s.pad(length=5))):
    try:
        call()
        report(label + " is refused", False)
    except ValueError as e:
        report(label + " is refused", True, str(e)[:50])
report("nothing left behind after errors", len([o for o in s.doc.Objects if o.TypeId.startswith("Sketcher")]) == 0)

# 7. the model is told about it, in both profiles, and the schema no longer demands plane/geometry
pad = next(t for t in agent.TOOLS if t["name"] == "pad")
report("prompt + schema", "face=<id>" in agent.SYSTEM_BASIC and "face" in pad["input_schema"]["properties"]
       and pad["input_schema"]["required"] == ["length"])

sys.exit(1 if fails else 0)
