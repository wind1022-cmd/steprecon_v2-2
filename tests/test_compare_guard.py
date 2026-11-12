"""compare must not trust OCC cuts that contradict the volumes (missing - extra == ref volume - new volume).

Seen on a real Fusion 360 Gallery design: for two identical cylinders new.cut(ref) returned all of `new`, which gave
XOR 100% for a perfect reconstruction. The failure depends on the exact file, so it is simulated here."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import compare  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402


class Wrapped:
    """A shape whose cut() is either correct or (like the OCC failure) returns the whole shape."""

    def __init__(self, shape, broken_cut):
        self._s, self._broken = shape, broken_cut

    def __getattr__(self, name):
        return getattr(self._s, name)

    def cut(self, other):
        return self._s if self._broken else self._s.cut(other._s)


def report(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


box = Part.makeBox(20, 10, 5)
same = Part.makeBox(20, 10, 5)
c = compare.compare(Wrapped(box, False), Wrapped(same, True))
report("identical shapes, one cut direction wrong -> sampled, converged",
       c["converged"] and c["xor_method"] == "sampled" and c["xor_pct"] == 0.0 and c["xor_boolean_pct_unreliable"] == 100.0
       and "extra_regions" not in c, str({k: c[k] for k in ("xor_pct", "xor_method", "converged")}))

smaller = Part.makeBox(20, 10, 4)          # 20% of the reference is missing
c = compare.compare(Wrapped(box, True), Wrapped(smaller, False))
report("different shapes, wrong cut -> sampled, not converged",
       not c["converged"] and c["xor_method"] == "sampled" and 15 < c["xor_pct"] < 25, f"xor={c['xor_pct']}")

# sampling alone is too coarse to see a tiny difference; unequal volumes must still block convergence
tiny = Part.makeBox(20, 10, 5).cut(Part.makeBox(1, 1, 1.2, App.Vector(0, 0, 0)))    # 0.12% removed, 3 more faces
c = compare.compare(Wrapped(box, False), Wrapped(tiny, True))
report("wrong cut + volume differs -> never converged", not c["converged"], f"vol diff={c['volume_diff_pct']}%")

c = compare.compare(box, same)
report("consistent booleans are left alone", c["converged"] and c["xor_method"] == "boolean"
       and "xor_coarse_check_pct" not in c)
c = compare.compare(box, smaller)
report("normal difference still reports regions", not c["converged"] and c["xor_method"] == "boolean"
       and len(c["missing_regions"]) == 1 and abs(c["xor_pct"] - 20) < 1e-3)
