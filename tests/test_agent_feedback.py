"""Feedback the loop gives a weak/slow model without costing it extra turns:
state attached to shape-changing results, automatic stop on convergence, one refusal of a premature finish,
nudges after empty answers, and errors for features that change nothing."""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import agent, fc_tools, llm  # noqa: E402
import Part  # noqa: E402

fails = 0


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


RECT = [{"type": "rect", "p1": [0, 0], "p2": [20, 10]}]
HOLE = [{"type": "circle", "c": [10, 5], "r": 2}]


class Scripted:
    name, model = "fake", "scripted"

    def __init__(self, script):
        self.script, self.i, self.results, self.users = script, 0, [], []

    def start(self, system, tools, user):
        pass

    def complete(self):
        step = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if step is None:
            return llm.Reply(text="", calls=[])
        return llm.Reply(text="", calls=[llm.Call(id=f"c{self.i}", name=step[0], args=step[1])])

    def add_results(self, results):
        self.results += [(c.name, json.loads(s)) for c, s in results]

    def add_user(self, text):
        self.users.append(text)

    def transcript(self):
        return []


tmp = tempfile.mkdtemp(prefix="steprecon_fb_")
step = os.path.join(tmp, "plate.step")
Part.makeBox(20, 10, 5).cut(Part.makeCylinder(2, 5, fc_tools.App.Vector(10, 5, 0))).exportStep(step)
PAD = ("pad", {"plane": "XY", "geometry": RECT, "length": 5})
CUT = ("pocket", {"plane": "XY", "offset": 5, "geometry": HOLE, "through_all": True})

# 1. state on results, automatic stop when converged (the script never calls compare or finish)
be = Scripted([PAD, CUT, ("pad", {"plane": "XY", "geometry": RECT, "length": 99})])
r = agent.run(step, os.path.join(tmp, "o1"), backend=be, profile="basic", verbose=False)
st = be.results[0][1].get("state", {})
report("state attached to pad result", st.get("converged") is False and st.get("bbox") == st.get("target_bbox")
       and st.get("target_faces") == 7, str(st)[:120])
report("stops by itself when converged", r["converged"] and r["turns"] == 2 and os.path.exists(r["fcstd"]),
       f"turns={r['turns']}")

# 2. a premature finish is refused once, then accepted
be = Scripted([PAD, ("finish", {})])
r = agent.run(step, os.path.join(tmp, "o2"), backend=be, profile="basic", verbose=False)
fin = [x for n, x in be.results if n == "finish"]
report("early finish refused once", len(fin) == 2 and fin[0]["ok"] is False and "state" in fin[0] and fin[1]["ok"]
       and not r["converged"], f"turns={r['turns']}")

# 3. empty answers: nudged with the current state, counter restarts after a real call, gives up after 3 in a row
be = Scripted([None, PAD, None, None, CUT])
r = agent.run(step, os.path.join(tmp, "o3"), backend=be, profile="basic", verbose=False)
report("survives scattered empty answers", r["converged"] and len(be.users) == 3 and "Nothing is built" in be.users[0]
       and "Current state" in be.users[1], f"nudges={len(be.users)}")
be = Scripted([PAD, None])
r = agent.run(step, os.path.join(tmp, "o4"), backend=be, profile="basic", verbose=False)
report("gives up after 3 empty answers in a row", r["turns"] == 5 and len(be.users) == 3, f"turns={r['turns']}")

# 4. features that change nothing are refused and leave no trace
s = fc_tools.Session(Part.makeBox(20, 10, 5))
s.pad("XY", RECT, 5)
n0 = len(s.doc.Objects)
a = s.pocket("XY", [{"type": "circle", "c": [100, 100], "r": 2}], through_all=True, offset=5)
b = s.pad("XY", [{"type": "rect", "p1": [5, 2], "p2": [10, 6]}], 3)
report("pocket outside the solid is an error", a["ok"] is False and a.get("solid_bbox") == [0, 0, 0, 20, 10, 5], str(a)[:90])
report("pad inside the solid is an error", b["ok"] is False and "added nothing" in b["error"])
report("nothing left behind", len(s.doc.Objects) == n0 and len(s.log) == 1 and s.body.Tip.Name == "Pad")
c = s.pocket("XY", HOLE, through_all=True, offset=5)
report("normal pocket still works afterwards", c["ok"] and s.state()["converged"] is False or c["ok"])

sys.exit(1 if fails else 0)
