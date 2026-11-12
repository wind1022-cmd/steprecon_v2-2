"""run.py: folder/wildcard input, --attempts (retry until converged, keep the best), --skip-done.
The model is a scripted fake: attempt 1 builds only the block, attempt 2 builds the whole part."""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import cli, llm, synth  # noqa: E402
import run  # noqa: E402

PAD = ("pad", {"plane": "XY", "geometry": [{"type": "rect", "p1": [0, 0], "p2": [40, 50]}], "length": 20})
REST = [("pocket", {"plane": "XY", "offset": 20, "depth": 3, "geometry": [{"type": "rect", "p1": [12, 10], "p2": [27, 25]}]}),
        ("hole", {"plane": "XY", "offset": 20, "x": 28, "y": 28, "diameter": 4, "through_all": True})]


class FakeBackend:
    """Half-finished on odd starts, complete on even starts (synth_003)."""
    name, model = "fake", "scripted"

    def __init__(self):
        self.starts = 0

    def start(self, system, tools, user):
        self.starts += 1
        self.script = [PAD] + (REST if self.starts % 2 == 0 else []) + [("finish", {})]
        self.i = 0

    def complete(self):
        name, args = self.script[min(self.i, len(self.script) - 1)]    # a refused early finish is simply repeated
        self.i += 1
        return llm.Reply(text="", calls=[llm.Call(id=f"c{self.i}", name=name, args=args)])

    def add_results(self, results):
        pass

    def add_user(self, text):
        pass

    def transcript(self):
        return []


def report(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


tmp = tempfile.mkdtemp(prefix="steprecon_cli_")
parts = os.path.join(tmp, "parts")
synth.generate(parts, n=1, start=3)                      # parts/synth_003.step (+ .history.txt, ignored)
out = os.path.join(tmp, "out")

got = run.expand([parts])
report("expand: folder", [os.path.basename(p) for p in got] == ["synth_003.step"])
report("expand: wildcard", run.expand([os.path.join(parts, "*.step")]) == got and run.expand([got[0], parts]) == got)

fake = FakeBackend()
cli.backend_from_args = lambda a: fake
sys.argv = ["run.py", parts, "--out", out, "--attempts", "3", "--backend", "openai"]
run.main()
with open(os.path.join(out, "summary.csv"), encoding="utf-8") as f:
    summary = f.read().splitlines()
with open(os.path.join(out, "synth_003.recon.json"), encoding="utf-8") as f:
    rec = json.load(f)
report("attempts: stops at the first converged attempt", fake.starts == 2 and os.path.isdir(os.path.join(out, "try2"))
       and not os.path.isdir(os.path.join(out, "try3")), f"starts={fake.starts}")
report("attempts: best attempt copied to --out", rec["converged"] and len(rec["features"]) == 3
       and os.path.exists(os.path.join(out, "synth_003.FCStd")) and ",True," in summary[1], summary[1])

sys.argv = ["run.py", parts, "--out", out, "--attempts", "3", "--backend", "openai", "--skip-done"]
run.main()
report("skip-done: converged file is not run again", fake.starts == 2, f"starts={fake.starts}")

fake2 = FakeBackend()
fake2.starts = 1                                         # next start is even -> would converge; force odd-only instead
fake2.start = lambda s, t, u: (setattr(fake2, "script", [PAD, ("finish", {})]), setattr(fake2, "i", 0),
                               setattr(fake2, "starts", fake2.starts + 1))[0]
cli.backend_from_args = lambda a: fake2
out2 = os.path.join(tmp, "out2")
sys.argv = ["run.py", parts, "--out", out2, "--attempts", "2", "--backend", "openai"]
run.main()
with open(os.path.join(out2, "synth_003.recon.json"), encoding="utf-8") as f:
    rec2 = json.load(f)
report("attempts: never converging still keeps the best result", not rec2["converged"] and fake2.starts == 3
       and os.path.exists(os.path.join(out2, "synth_003.FCStd")) and os.path.isdir(os.path.join(out2, "try2")))
