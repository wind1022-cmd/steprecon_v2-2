import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze, fc_tools

R = os.path.join(ROOT, "data", "fusion360", "r1.0.1", "reconstruction")
OUT = os.path.join(ROOT, "out_fusion")
os.makedirs(OUT, exist_ok=True)


def run(name, build):
    ref = analyze.load_shape(os.path.join(R, name + ".step"))
    s = fc_tools.Session(ref)
    build(s)
    c = s.compare()
    print(f"{name}: xor={c['xor_pct']}% faces {c['ref_faces']}/{c['new_faces']} converged={c['converged']}")
    if not c["converged"]:
        print("   ", {k: v for k, v in c.items() if "region" in k or k.endswith("types")})
    s.save(os.path.join(OUT, name + ".FCStd"))
    print("    features:", s.log)


def b1(s):
    s.pad("XZ", [{"type": "rect", "p1": [-1457.103, -573.236], "p2": [342.897, -73.236]}], 5, reversed=True)


a, b = 143.684, 346.884   # key points of the outline
Y0, Z0 = 179.605, 310.963


def L(p1, p2):
    return {"type": "line", "p1": p1, "p2": p2}


def b2(s):
    g = [L([0, 203.2], [143.684, 346.884]),
         {"type": "arc", "c": [Y0, Z0], "r": 50.8, "a0": -45, "a1": 135},
         L([215.526, 275.042], [101.6, 161.116]),
         L([101.6, 161.116], [101.6, -161.116]),
         L([101.6, -161.116], [215.526, -275.042]),
         {"type": "arc", "c": [Y0, -Z0], "r": 50.8, "a0": -135, "a1": 45},
         L([143.684, -346.884], [0, -203.2]),
         L([0, -203.2], [0, 203.2]),
         {"type": "circle", "c": [Y0, Z0], "r": 4.763},
         {"type": "circle", "c": [Y0, -Z0], "r": 4.763}]
    r = s.pad("YZ", g, 6.35)
    print("   ", r)


def b3(s):
    s.pad("XZ", [{"type": "circle", "c": [-40, 0], "r": 2.5}, {"type": "circle", "c": [-40, 0], "r": 1.75}],
          190, reversed=True)


for n, f in (("24603_a4021250_0002", b1), ("23792_5b4c1ce0_0001", b2), ("21822_7d3db422_0023", b3)):
    try:
        run(n, f)
    except Exception as e:
        print(n, "EXC", type(e).__name__, e)
