"""Stage 4: native Hole (multi-position, counterbore, countersink, drill point), sweep and loft."""
import math
import os
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import agent, analyze, dataset, fc_tools  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

V = App.Vector


def rect(x0, y0, x1, y1):
    return [{"type": "rect", "p1": [x0, y0], "p2": [x1, y1]}]


def report(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def check(name, ref, build, expect_features=None):
    s = fc_tools.Session(ref, "t")
    try:
        results = build(s) or []
        c = s.compare()
        ok = c["converged"] and all(r.get("ok") for r in results)
        if expect_features is not None:
            ok = ok and len(s.log) == expect_features
        report(name, ok, f"xor={c['xor_pct']}% ({c['xor_method']}) faces {c['ref_faces']}/{c['new_faces']} features={len(s.log)}")
        if not ok:
            print("   ", [r for r in results if not r.get("ok")],
                  {k: v for k, v in c.items() if "region" in k or k.endswith("types")})
    except Exception as e:
        print(f"FAIL  {name}: EXC {type(e).__name__}: {e}")
    return s


PLATE = Part.makeBox(60, 40, 10)


def plate(s):
    return s.pad("XY", rect(0, 0, 60, 40), 10)


# ---------- holes ----------
pos = [[10, 10], [27, 31], [50, 14]]
ref = PLATE
for x, y in pos:
    ref = ref.cut(Part.makeCylinder(3, 12, V(x, y, -1)))
check("hole: 3 irregular positions in one feature", ref,
      lambda s: [plate(s), s.hole("XY", diameter=6, positions=pos, through_all=True, offset=10)], expect_features=2)

ref = PLATE.cut(Part.makeCylinder(2.5, 12, V(20, 20, -1))).cut(Part.makeCylinder(4.5, 3, V(20, 20, 7)))
check("hole: counterbore", ref, lambda s: [plate(s), s.hole("XY", 20, 20, 5, through_all=True, offset=10,
                                                             counterbore_diameter=9, counterbore_depth=3)], 2)

ref = PLATE.cut(Part.makeCylinder(2.5, 12, V(20, 20, -1))).cut(Part.makeCone(2.5, 4.5, 2, V(20, 20, 8)))
check("hole: countersink 90 deg", ref, lambda s: [plate(s), s.hole("XY", 20, 20, 5, through_all=True, offset=10,
                                                                    countersink_diameter=9)], 2)

tip = 3 / math.tan(math.radians(59))
ref = PLATE.cut(Part.makeCylinder(3, 6, V(40, 20, 4))).cut(Part.makeCone(0, 3, tip, V(40, 20, 4 - tip)))
check("hole: blind with 118 deg drill point", ref,
      lambda s: [plate(s), s.hole("XY", 40, 20, 6, depth=6, offset=10, drill_angle=118)], 2)

ref = PLATE.cut(Part.makeCylinder(2, 8, V(30, 0, 5), V(0, 1, 0)))
check("hole: from the side (XZ plane, +Y into the part)", ref,
      lambda s: [plate(s), s.hole("XZ", 30, 5, 4, depth=8)], 2)

ref = Part.makeCylinder(30, 8)
for i in range(4):
    a = math.radians(90 * i)
    p = V(20 * math.cos(a), 20 * math.sin(a), 0)
    ref = ref.cut(Part.makeCylinder(2, 10, p - V(0, 0, 1))).cut(Part.makeCylinder(4, 3, p + V(0, 0, 5)))
check("hole: counterbored hole + polar pattern", ref,
      lambda s: [s.pad("XY", [{"type": "circle", "c": [0, 0], "r": 30}], 8),
                 s.hole("XY", 20, 0, 4, through_all=True, offset=8, counterbore_diameter=8, counterbore_depth=3),
                 s.polar_pattern(1, "Z", 4)], 3)

s = fc_tools.Session(PLATE, "bad")
plate(s)
bad = [s.hole("XY", diameter=4, through_all=True), s.hole("XY", 5, 5, 4), s.hole("XY", 5, 5, 4, depth=3, counterbore_diameter=3,
                                                                                  counterbore_depth=1)]
report("hole: bad arguments are refused without leaving objects", all(not b["ok"] for b in bad) and len(s.log) == 1
       and len(s.body.Group) == 2, str([b.get("error", "")[:30] for b in bad]))

# ---------- sweep ----------
PATH = [{"type": "line", "p1": [0, 0], "p2": [20, 0]}, {"type": "arc", "c": [20, 10], "r": 10, "a0": -90, "a1": 0}]


def elbow(r):
    t = Part.makeTorus(10, r, V(20, 10, 0), V(0, 0, 1), -180, 180, 90)
    t.rotate(V(20, 10, 0), V(0, 0, 1), -90)
    return Part.makeCylinder(r, 20, V(0, 0, 0), V(1, 0, 0)).fuse(t).removeSplitter()


ROD = elbow(3)
s = check("sweep: round bar along line + arc", ROD,
          lambda s: [s.sweep("YZ", [{"type": "circle", "c": [0, 0], "r": 3}], "XY", PATH)], 1)
n_before = len(s.doc.Objects)
u = s.undo()
report("sweep: undo removes the feature and both sketches", u["ok"] and len(s.doc.Objects) == n_before - 3
       and s.compare().get("ok") is False)

check("sweep: tube (two concentric circles)", elbow(3).cut(elbow(2)),
      lambda s: [s.sweep("YZ", [{"type": "circle", "c": [0, 0], "r": 3}, {"type": "circle", "c": [0, 0], "r": 2}],
                         "XY", PATH)], 1)

sector = Part.makeCylinder(12, 4, V(20, 10, -4), V(0, 0, 1), 90).cut(Part.makeCylinder(8, 4, V(20, 10, -4), V(0, 0, 1), 90))
sector.rotate(V(20, 10, 0), V(0, 0, 1), -90)
channel = Part.makeBox(25, 4, 4, V(-5, -2, -4)).fuse([sector, Part.makeBox(4, 20, 4, V(28, 10, -4))])
BLOCK = Part.makeBox(50, 40, 10, V(-5, -10, -10))
check("sweep: cut channel along a bent path", BLOCK.cut(channel).removeSplitter(),
      lambda s: [s.pad("XY", rect(-5, -10, 45, 30), 10, reversed=True),
                 s.sweep("YZ", rect(-2, -4, 2, 0), "XY",
                         [{"type": "line", "p1": [-5, 0], "p2": [20, 0]},
                          {"type": "arc", "c": [20, 10], "r": 10, "a0": -90, "a1": 0},
                          {"type": "line", "p1": [30, 10], "p2": [30, 30]}], offset=-5, cut=True)], 2)

s = fc_tools.Session(ROD, "bad2")
try:
    s.sweep("YZ", [{"type": "circle", "c": [0, 0], "r": 3}], "XY", [{"type": "spline", "pts": []}])
    raised = False
except ValueError:
    raised = True
r1 = s.sweep("YZ", [{"type": "circle", "c": [0, 0], "r": 3}], "XY", PATH, cut=True)
r2 = s.loft([{"plane": "XY", "geometry": rect(0, 0, 5, 5)}])
report("sweep/loft: bad input leaves nothing behind", raised and not r1["ok"] and not r2["ok"]
       and len(s.body.Group) == 0 and not s.log
       and not [o for o in s.doc.Objects if o.isDerivedFrom("Sketcher::SketchObject")], f"group={len(s.body.Group)}")

# ---------- loft ----------
check("loft: circle -> circle (cone boss on a plate)", PLATE.fuse(Part.makeCone(10, 5, 15, V(30, 20, 10))).removeSplitter(),
      lambda s: [plate(s), s.loft([{"plane": "XY", "offset": 10, "geometry": [{"type": "circle", "c": [30, 20], "r": 10}]},
                                   {"plane": "XY", "offset": 25, "geometry": [{"type": "circle", "c": [30, 20], "r": 5}]}],
                                  ruled=True)], 2)

# frustum with truly planar sides (wedge along +Y): XZ sketches, the plane sits at Y = -offset
check("loft: rectangle -> rectangle on XZ planes vs a planar wedge", Part.makeWedge(0, 0, 0, 5, 5, 20, 20, 20, 15, 15),
      lambda s: [s.loft([{"plane": "XZ", "offset": 0, "geometry": rect(0, 0, 20, 20)},
                         {"plane": "XZ", "offset": -20, "geometry": rect(5, 5, 15, 15)}], ruled=True)], 1)

check("loft: three sections, ruled", Part.makeCone(10, 5, 20).fuse(Part.makeCone(5, 8, 15, V(0, 0, 20))).removeSplitter(),
      lambda s: [s.loft([{"plane": "XY", "offset": z, "geometry": [{"type": "circle", "c": [0, 0], "r": r}]}
                         for z, r in ((0, 10), (20, 5), (35, 8))], ruled=True)], 1)

big = Part.makeBox(40, 40, 20, V(-20, -20, 0))
check("loft: cut (tapered round pocket)", big.cut(Part.makeCone(5, 10, 10, V(0, 0, 10))),
      lambda s: [s.pad("XY", rect(-20, -20, 20, 20), 20),
                 s.loft([{"plane": "XY", "offset": 20, "geometry": [{"type": "circle", "c": [0, 0], "r": 10}]},
                         {"plane": "XY", "offset": 10, "geometry": [{"type": "circle", "c": [0, 0], "r": 5}]}],
                        ruled=True, cut=True)], 2)

# ---------- a torus is analytic ("Toroid" in FreeCAD), not freeform ----------
tmp = tempfile.mkdtemp(prefix="steprecon_s4_")
step = os.path.join(tmp, "elbow.step")
ROD.exportStep(step)
a = analyze.analyze(step)
tor = [f for f in a["faces"] if f["type"] == "Toroid"]
report("analyze: torus face is analytic with radii/axis", a["freeform"] is None and len(tor) == 1
       and abs(tor[0]["major_radius"] - 10) < 1e-6 and abs(tor[0]["minor_radius"] - 3) < 1e-6 and "axis" in tor[0]
       and dataset.classify(a)[0] != "freeform", f"category={dataset.classify(a)[0]} types={a['face_type_histogram']}")

# ---------- scripted model through agent.run (argument names as the model sends them) ----------
SCRIPT = [("sweep", {"plane": "YZ", "geometry": [{"type": "circle", "c": [0, 0], "r": 3}],
                     "path_plane": "XY", "path_geometry": PATH}),
          ("compare", {}), ("finish", {})]


class FakeMessages:
    def __init__(self):
        self.i = 0

    def create(self, **kw):
        name, inp = SCRIPT[self.i]
        self.i += 1
        return NS(content=[NS(type="tool_use", id=f"t{self.i}", name=name, input=inp)])


res = agent.run(step, os.path.join(tmp, "out"), client=NS(messages=FakeMessages()), verbose=False)
report("agent loop with sweep converges", res["converged"] and len(res["features"]) == 1, f"xor={res['xor_pct']}")
names = {t["name"] for t in agent.TOOLS}
report("tools registered; basic profile still 9 tools", {"sweep", "loft"} <= names and len(agent.TOOLS) == 24
       and len(agent.select_tools("basic")) == 9 and "Swept shapes" not in agent.SYSTEM_BASIC)
