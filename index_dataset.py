"""Classify every STEP/IGES file in a folder and write an index CSV.

  python index_dataset.py <folder> [--out index.csv] [--max-faces 60] [--limit N]
Run with FreeCAD's bundled python.exe.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from step_recon import dataset  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default="index.csv")
    ap.add_argument("--max-faces", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    rows = dataset.scan(a.folder, max_faces=a.max_faces, limit=a.limit)
    dataset.write_index(rows, a.out)
    print(f"{len(rows)} files -> {a.out}")
    for cat, n in collections.Counter(r["category"] for r in rows).most_common():
        print(f"  {cat:12s} {n}")


if __name__ == "__main__":
    main()
