"""Replay a scripted 'model' through agent.run (Anthropic code path) to verify the loop without an API key."""
import os
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import agent, synth  # noqa: E402

SCRIPT = [
    ("pad", {"plane": "XY", "geometry": [{"type": "rect", "p1": [0, 0], "p2": [40, 50]}], "length": 20}),
    ("pocket", {"plane": "XY", "offset": 20, "depth": 3,
                "geometry": [{"type": "rect", "p1": [12, 10], "p2": [27, 25]}]}),
    ("hole", {"plane": "XY", "offset": 20, "x": 28, "y": 28, "diameter": 4, "through_all": True}),
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


tmp = tempfile.mkdtemp(prefix="steprecon_test_")
step = synth.generate(os.path.join(tmp, "data"), n=1, start=3)[0]      # synth_003 matches SCRIPT
r = agent.run(step, os.path.join(tmp, "out"), client=NS(messages=FakeMessages()), verbose=False)
print(r)
assert r["converged"], "expected convergence"
assert os.path.exists(r["fcstd"])
print("MOCK AGENT OK")
