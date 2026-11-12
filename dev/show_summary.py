import os
import sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze
a = analyze.analyze(sys.argv[1])
print({k: a[k] for k in ("volume", "bbox", "n_faces", "face_type_histogram")})
for f in a["faces"]:
    d = {k: v for k, v in f.items() if k not in ("edges", "area")}
    print(json.dumps(d))
