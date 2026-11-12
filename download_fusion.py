"""Download the Fusion 360 Gallery reconstruction dataset (r1.0.1, ~2 GB) and unpack the STEP + JSON files.

  python download_fusion.py --yes [--dest data] [--keep-zip]
Works with any Python 3 (standard library only). Afterwards build the index:
  <FreeCAD python> fusion_index.py data/fusion360/r1.0.1/reconstruction --out data/fusion_idx.csv

LICENSE: the dataset may only be used for NON-COMMERCIAL RESEARCH purposes (Fusion 360 Gallery Dataset License,
included in the archive). Do not redistribute it. Pass --yes to confirm you accept those terms.
"""
import argparse
import os
import sys
import time
import urllib.request
import zipfile

URL = "https://fusion-360-gallery-dataset.s3.us-west-2.amazonaws.com/reconstruction/r1.0.1/r1.0.1.zip"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default="data")
    ap.add_argument("--yes", action="store_true", help="I accept the dataset license (non-commercial research use)")
    ap.add_argument("--keep-zip", action="store_true")
    a = ap.parse_args()
    if not a.yes:
        sys.exit("The Fusion 360 Gallery dataset is licensed for non-commercial research use only.\n"
                 "Re-run with --yes to confirm and start the ~2 GB download.")

    dl_dir = os.path.join(a.dest, "fusion360_dl")
    out_dir = os.path.join(a.dest, "fusion360")
    os.makedirs(dl_dir, exist_ok=True)
    zpath = os.path.join(dl_dir, "r1.0.1.zip")
    if not os.path.exists(zpath):
        print("downloading", URL)
        t0 = time.time()
        tmp = zpath + ".part"
        with urllib.request.urlopen(URL) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length", 0))
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r  {got / 1e6:7.0f} / {total / 1e6:.0f} MB  ({time.time() - t0:.0f}s)", end="", flush=True)
        os.replace(tmp, zpath)
        print()
    else:
        print("using existing", zpath)

    if os.path.isdir(out_dir) and os.listdir(out_dir):
        sys.exit(f"{out_dir} already exists and is not empty; delete it first to re-extract.")
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zpath) as z:                # zipfile strips absolute paths and '..' components
        for m in z.infolist():
            if m.filename.endswith((".json", ".step", ".docx")):   # skip png/obj/smt (not used here)
                z.extract(m, out_dir)
                n += 1
    print(f"extracted {n} files to {out_dir}")
    if not a.keep_zip:
        os.remove(zpath)
    print("next: <FreeCAD python> fusion_index.py", os.path.join(out_dir, "r1.0.1", "reconstruction"),
          "--out", os.path.join(a.dest, "fusion_idx.csv"))


if __name__ == "__main__":
    main()
