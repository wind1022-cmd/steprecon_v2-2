"""Generate small synthetic test parts (block + optional pocket / holes / fillets) with a known history.

  <FreeCAD python> make_synth.py --n 10 --out data/synth
Each part is written as synth_NNN.step plus synth_NNN.step.history.txt (how it was built).
Needs no dataset and no network: use these for a first smoke test of a local model, e.g.
  <FreeCAD python> run.py data/synth/synth_000.step --backend openai --model qwen3:8b
  <FreeCAD python> index_dataset.py data/synth --out data/synth_idx.csv
  <FreeCAD python> eval_batch.py data/synth_idx.csv --n 10 --backend openai --model qwen3:8b --out out_local
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from step_recon import synth  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--start", type=int, default=0, help="first seed (same seed = same part on every PC)")
    ap.add_argument("--out", default=os.path.join("data", "synth"))
    a = ap.parse_args()
    for p in synth.generate(a.out, n=a.n, start=a.start):
        with open(p + ".history.txt", encoding="utf-8") as f:
            print(f"{p}: " + "; ".join(f.read().splitlines()))


if __name__ == "__main__":
    main()
