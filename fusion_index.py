"""Classify the Fusion 360 Gallery reconstruction designs (in parallel) and write an index CSV.

  python fusion_index.py data\\fusion360\\r1.0.1\\reconstruction --out data\\fusion_idx.csv [--limit N] [--workers 4]
Run with FreeCAD's bundled python.exe. The CSV works with eval_batch.py and has extra gt_* columns.
"""
import argparse
import collections
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    from step_recon import dataset, fusion
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--out", default="data/fusion_idx.csv")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-faces", type=int, default=60)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()

    designs = fusion.list_designs(a.root)[:a.limit]
    jobs = [(s, j, a.max_faces) for s, j in designs]
    print(f"{len(jobs)} designs, {a.workers} workers", flush=True)
    t0 = time.time()
    rows = []
    with mp.Pool(a.workers) as pool:
        for i, r in enumerate(pool.imap(fusion.scan_one, jobs, chunksize=8), 1):
            rows.append(r)
            if i % 500 == 0:
                print(f"  {i}/{len(jobs)}  {time.time() - t0:.0f}s", flush=True)
    dataset.write_index(rows, a.out, extra_cols=fusion.EXTRA_COLS)
    print(f"-> {a.out}  ({time.time() - t0:.0f}s)")
    for cat, n in collections.Counter(r["category"] for r in rows).most_common():
        print(f"  {cat:12s} {n}")


if __name__ == "__main__":
    mp.freeze_support()
    main()
