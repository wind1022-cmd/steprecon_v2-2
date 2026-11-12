"""Parts made of several separate solids: one PartDesign body per solid (new_body / select_body)."""
import os
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402
from step_recon import agent, analyze, fc_tools  # noqa: E402


def report(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def rect(x0, y0, x1, y1):
    return [{"type": "rect", "p1": [x0, y0], "p2": [x1, y1]}]


# reference: block A (with a pocket) + block B 30 mm away (with a through hole) + a cylinder C
A = Part.makeBox(40, 30, 10).cut(Part.makeBox(10, 10, 4, App.Vector(5, 5, 6)))
B = Part.makeBox(20, 30, 15, App.Vector(70, 0, 0)).cut(Part.makeCylinder(3, 20, App.Vector(80, 15, -1)))
C = Part.makeCylinder(8, 25, App.Vector(120, 15, 0))
REF = Part.makeCompound([A, B, C])

# ---- 1) build three bodies by hand ----
s = fc_tools.Session(REF, "mb1")
r0 = s.new_body()
report("new_body refused while the first body is empty", not r0["ok"])
s.pad("XY", rect(0, 0, 40, 30), 10)
c = s.compare()
report("after body 0: not converged, hint says more solids", not c["converged"] and "new_body" in c.get("hint", "")
       and len(c.get("missing_regions", [])) == 2, f"xor={c['xor_pct']}")
s.pocket("XY", rect(5, 5, 15, 15), depth=4, offset=10)
r = s.new_body()
report("new_body", r["ok"] and r["active_body"] == 1)
r = s.pad("XY", rect(70, 0, 90, 30), 15)
report("pad lands in body 1", r["ok"] and r.get("body") == 1 and abs(r["volume"] - 20 * 30 * 15) < 1e-6, str(r))
s.hole("XY", 80, 15, 6, through_all=True, offset=15)
try:                                       # feature 1 = the pocket in body 0, but body 1 is active
    s.linear_pattern(1, "X", 10, 2)        # (agent.run turns the exception into an error result for the model)
    refused = False
except ValueError as e:
    refused = "belongs to body 0" in str(e)
report("pattern across bodies refused", refused and len(s.history) == 5)
s.new_body()
s.pad("XY", [{"type": "circle", "c": [120, 15], "r": 8}], 25)
c = s.compare()
report("three bodies converge", c["converged"] and c["ref_solids"] == 3 and c["new_solids"] == 3 and c["bodies"] == 3,
       f"xor={c['xor_pct']}% faces {c['ref_faces']}/{c['new_faces']}")

# ---- 2) select_body: go back and edit an earlier body ----
f = s.features()
report("features lists bodies", [x.get("body") for x in f["features"]] == [0, 0, 1, 1, 1, 2, 2]
       and [b["active"] for b in f["bodies"]] == [False, False, True], str([x.get("body") for x in f["features"]]))
r = s.select_body(0)
edges = [e["edge"] for e in s.list_edges(only_type="Line")["edges"]
         if abs(e["length"] - 10) < 1e-6 and e["p1"][:2] == e["p2"][:2] and e["p1"][:2] in ([0, 0], [40, 0], [40, 30], [0, 30])]
r = s.fillet(edges, 3)
ref2 = Part.makeCompound([A.makeFillet(3, [e for e in A.Edges if abs(e.Length - 10) < 1e-6
                                           and abs(e.Vertexes[0].Point.z - e.Vertexes[-1].Point.z) > 9
                                           and e.Vertexes[0].Point.x in (0, 40) and e.Vertexes[0].Point.y in (0, 30)]), B, C])
s.ref = ref2
c = s.compare()
report("fillet in body 0 after select_body", r["ok"] and r.get("body") == 0 and len(edges) == 4 and c["converged"],
       f"xor={c['xor_pct']}% faces {c['ref_faces']}/{c['new_faces']}")
s.ref = REF

# ---- 3) undo across bodies ----
u = s.undo()                               # the fillet (in body 0) although it was added last
report("undo removes the last step in its own body", u["ok"] and u.get("undone_in_body") == 0 and s.compare()["converged"])
s.select_body(2)
s.undo()                                   # cylinder pad
u = s.undo()                               # body 2 itself
report("undo removes an empty body", u["ok"] and u["bodies"] == 2 and u["active_body"] == 1 and len(s.bodies) == 2
       and len([o for o in s.doc.Objects if o.isDerivedFrom("PartDesign::Body")]) == 2, str(u))
c = s.compare()
report("after undo one solid is missing", not c["converged"] and c["new_solids"] == 2 and len(c["missing_regions"]) == 1)
s.new_body()
s.pad("XY", [{"type": "circle", "c": [120, 15], "r": 8}], 25)
report("rebuild converges again", s.compare()["converged"])

# ---- 4) overlapping bodies are merged for the comparison and reported ----
s2 = fc_tools.Session(Part.makeCompound([Part.makeBox(10, 10, 10), Part.makeBox(10, 10, 10, App.Vector(30, 0, 0))]), "mb2")
s2.pad("XY", rect(0, 0, 10, 10), 10)
s2.new_body()
s2.pad("XY", rect(5, 0, 40, 10), 10)       # wrong: overlaps body 0
c = s2.compare()
report("overlapping bodies: merged, not converged, hint", not c["converged"] and c["new_solids"] == 1
       and "overlap" in c.get("hint", ""), f"new_solids={c['new_solids']}")

# ---- 5) summary of a multi-solid STEP file ----
tmp = tempfile.mkdtemp(prefix="steprecon_mb_")
step = os.path.join(tmp, "three_bodies.step")
REF.exportStep(step)
a = analyze.analyze(step)
ok = a["n_solids"] == 3 and len(a["solids"]) == 3 and sum(x["n_faces"] for x in a["solids"]) == a["n_faces"]
for face in a["faces"]:                    # every face lies inside the bounding box of the solid it is tagged with
    so = a["solids"][face["solid"]]
    ok = ok and all(face["bbox"][i] >= so["bbox_min"][i] - 1e-3 and face["bbox"][i + 3] <= so["bbox_max"][i] + 1e-3
                    for i in range(3))
report("analyze tags faces with their solid", ok, str([(x["volume"], x["n_faces"]) for x in a["solids"]]))
one = os.path.join(tmp, "one.step")
A.exportStep(one)
a1 = analyze.analyze(one)
report("single solid summary unchanged", "solids" not in a1 and all("solid" not in f for f in a1["faces"]))
touch = os.path.join(tmp, "touch.step")    # two solids sharing a face are one body, as before
Part.makeCompound([Part.makeBox(10, 10, 10), Part.makeBox(10, 10, 10, App.Vector(10, 0, 0))]).exportStep(touch)
report("touching solids still merge on load", analyze.analyze(touch)["n_solids"] == 1)

# ---- 6) scripted model through agent.run ----
by_solid = sorted(a["solids"], key=lambda x: x["bbox_min"][0])      # file order is not guaranteed
SCRIPT = [
    ("pad", {"plane": "XY", "geometry": rect(0, 0, 40, 30), "length": 10}),
    ("pocket", {"plane": "XY", "offset": 10, "depth": 4, "geometry": rect(5, 5, 15, 15)}),
    ("new_body", {}),
    ("pad", {"plane": "XY", "geometry": rect(70, 0, 90, 30), "length": 15}),
    ("hole", {"plane": "XY", "offset": 15, "x": 80, "y": 15, "diameter": 6, "through_all": True}),
    ("new_body", {}),
    ("pad", {"plane": "XY", "geometry": [{"type": "circle", "c": [120, 15], "r": 8}], "length": 25}),
    ("compare", {}),
    ("finish", {}),
]


class FakeMessages:
    def __init__(self):
        self.i = 0

    def create(self, **kw):
        name, inp = SCRIPT[self.i]
        self.i += 1
        return NS(content=[NS(type="tool_use", id=f"t{self.i}", name=name, input=inp)])


res = agent.run(step, os.path.join(tmp, "out"), client=NS(messages=FakeMessages()), verbose=False)
doc = App.openDocument(res["fcstd"])
n_bodies = len([o for o in doc.Objects if o.isDerivedFrom("PartDesign::Body")])
report("agent loop with new_body converges and saves 3 bodies", res["converged"] and n_bodies == 3 and len(by_solid) == 3,
       f"xor={res['xor_pct']} bodies={n_bodies}")
names = {t["name"] for t in agent.TOOLS}
report("tools registered; basic profile unchanged", {"new_body", "select_body"} <= names
       and len(agent.select_tools("basic")) == 9 and "Multiple bodies" not in agent.SYSTEM_BASIC
       and "Multiple bodies" in agent.SYSTEM)
