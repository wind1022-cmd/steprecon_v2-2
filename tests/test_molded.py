import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import math
import FreeCAD as App
import Part
from step_recon import fc_tools

# Reference built independently with OCC primitives: drafted block -> shell (open top) -> rib -> bottom edge fillet skipped
W, D, H, ang, t = 60.0, 40.0, 20.0, 3.0, 2.0
d = H * math.tan(math.radians(ang))


def poly(x0, y0, x1, y1, z):
    return Part.makePolygon([App.Vector(x0, y0, z), App.Vector(x1, y0, z), App.Vector(x1, y1, z), App.Vector(x0, y1, z), App.Vector(x0, y0, z)])


outer = Part.makeLoft([poly(0, 0, W, D, 0), poly(d, d, W - d, D - d, H)], True)
top = [f for f in outer.Faces if abs(f.normalAt(0, 0).z - 1) < 1e-6 or abs(f.CenterOfMass.z - H) < 1e-6][0]
ref = outer.makeThickness([top], -t, 1e-4)
print("ref valid:", ref.isValid(), "vol", round(ref.Volume, 2), "faces", len(ref.Faces))

s = fc_tools.Session(ref)
print(s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [W, D]}], H))
faces = s.list_faces(only_type="Plane")["faces"]
sides = [f["face"] for f in faces if abs(f["normal"][2]) < 1e-3]
bottom = [f["face"] for f in faces if f["normal"][2] < -0.99][0]
topf = [f["face"] for f in faces if f["normal"][2] > 0.99][0]
print(s.draft(sides, ang, bottom, "Z"))
faces = s.list_faces(only_type="Plane")["faces"]
topf = [f["face"] for f in faces if f["normal"][2] > 0.99][0]
print(s.shell([topf], t))
c = s.compare()
print({k: v for k, v in c.items() if k in ("xor_pct", "ref_faces", "new_faces", "converged", "missing_volume", "extra_volume")})

assert c['converged'], 'drafted+shelled part did not converge'
print('MOLDED OK')
