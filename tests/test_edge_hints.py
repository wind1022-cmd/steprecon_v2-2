"""list_edges tells which sharp edges of the rebuilt solid the reference does not have and what it has there
instead (fillet radius / chamfer size), so the model does not have to guess edges and sizes."""
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


def edge_at(shape, p, q):
    for e in shape.Edges:
        a, b = e.Vertexes[0].Point, e.Vertexes[-1].Point
        if (a.distanceToPoint(p) < 1e-6 and b.distanceToPoint(q) < 1e-6) or \
                (a.distanceToPoint(q) < 1e-6 and b.distanceToPoint(p) < 1e-6):
            return e
    raise AssertionError("edge not found")


# block 40x30x10 with a through hole; one long top edge chamfered 1.5, two vertical edges filleted r=3,
# the hole's top rim chamfered 0.5
ref = Part.makeBox(40, 30, 10).cut(Part.makeCylinder(4, 10, V(20, 15, 0)))
ref = ref.makeChamfer(1.5, [edge_at(ref, V(0, 0, 10), V(40, 0, 10))])
ref = ref.makeFillet(3, [e for e in ref.Edges if type(e.Curve).__name__ == "Line"
                         and abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 5
                         and e.Vertexes[0].Point.y > 29])
rim = [e for e in ref.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Curve.Radius - 4) < 1e-6
       and abs(e.Vertexes[0].Point.z - 10) < 1e-6]
ref = ref.makeChamfer(0.5, rim)
tmp = tempfile.mkdtemp(prefix="steprecon_hint_")
path = os.path.join(tmp, "part.step")
ref.exportStep(path)
ref = analyze.load_shape(path)

s = fc_tools.Session(ref)
s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [40, 30]}], 10)
s.hole("XY", 20, 15, diameter=8, through_all=True, offset=10)
every = s.list_edges()["edges"]
miss = s.list_edges(only_missing=True)
report("all edges carry in_reference", all("in_reference" in e for e in every) and len(every) > len(miss["edges"]))
sug = sorted(str(e.get("suggest")) for e in miss["edges"])
report("exactly the four dressed edges are reported, with what the reference has there",
       sug == ["{'chamfer': 0.5}", "{'chamfer': 1.5}", "{'fillet': 3.0}", "{'fillet': 3.0}"], str(sug))

# following the suggestions, group by group, reaches the reference
for _ in range(4):
    todo = s.list_edges(only_missing=True)["edges"]
    todo = [e for e in todo if e.get("suggest")]
    if not todo:
        break
    first = todo[0]["suggest"]
    ids = [e["edge"] for e in todo if e["suggest"] == first]
    r = s.fillet(ids, first["fillet"]) if "fillet" in first else s.chamfer(ids, first["chamfer"])
    assert r["ok"], r
st = s.state()
report("applying the suggestions converges", st["converged"], f"xor={st['xor_pct']} faces={st['faces']}/{st['target_faces']}")
report("nothing left to dress", s.list_edges(only_missing=True)["edges"] == [])

# a 60 degree wedge edge: the formulas use the real angle between the faces, not 90 degrees
import math  # noqa: E402
prof = Part.makePolygon([V(0, 0, 0), V(30, 0, 0), V(15, 15 * math.sqrt(3), 0), V(0, 0, 0)])
wedge = Part.Face(prof).extrude(V(0, 0, 8))
sharp = edge_at(wedge, V(0, 0, 0), V(0, 0, 8))
for label, dressed, want in (("fillet", wedge.makeFillet(2, [sharp]), {"fillet": 2.0}),
                             ("chamfer", wedge.makeChamfer(2, [sharp]), {"chamfer": 2.0})):
    p = os.path.join(tmp, label + ".step")
    dressed.exportStep(p)
    s = fc_tools.Session(analyze.load_shape(p))
    s.pad("XY", [{"type": "line", "p1": [0, 0], "p2": [30, 0]}, {"type": "line", "p1": [30, 0], "p2": [15, 15 * math.sqrt(3)]},
                 {"type": "line", "p1": [15, 15 * math.sqrt(3)], "p2": [0, 0]}], 8)
    got = [e.get("suggest") for e in s.list_edges(only_missing=True)["edges"]]
    report(f"60 degree corner: {label} size from the real angle", got == [want], str(got))

le = next(t for t in agent.TOOLS if t["name"] == "list_edges")
report("prompt + schema mention only_missing", "only_missing=true" in agent.SYSTEM_BASIC
       and "only_missing" in le["input_schema"]["properties"])

sys.exit(1 if fails else 0)
