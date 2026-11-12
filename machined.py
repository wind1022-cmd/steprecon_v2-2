"""Complex parts (housings, castings): keep the shape as a frozen blank and turn the drilled holes, constant fillets
and equal chamfers into editable features with dimensions. No AI model is used - this is geometric recognition only, so it is fast and exact.

    fc.cmd machined.py C:\\parts\\housing.stp --out out_machined
    fc.cmd machined.py C:\\parts            (a folder: every STEP/IGES file in it)

Result per file: <name>.FCStd with  ImportedBlank -> Hole_D8_x6 -> Hole_D25 -> ...  (diameter, position and depth
of every hole are sketch dimensions / feature parameters), and a line in summary.csv."""
import argparse
import csv
import glob
import json
import os
import sys
import time

import FreeCAD as App

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from step_recon import analyze, cpu, machined  # noqa: E402
import doe  # noqa: E402  (parameter table and map)

EXTS = (".stp", ".step", ".igs", ".iges")


def expand(args):
    """Files, folders (all STEP/IGES inside, recursively) and wildcards (Windows shells do not expand them)."""
    out = []
    for a in args:
        if os.path.isdir(a):
            for root, _, files in os.walk(a):
                out += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(EXTS)]
        elif any(c in a for c in "*?["):
            out += [p for p in sorted(glob.glob(a, recursive=True)) if p.lower().endswith(EXTS)]
        else:
            out.append(a)
    return list(dict.fromkeys(out))


def show_progress(fraction, stage, info):
    """One line on the console that is rewritten as the analysis goes on (only in a console window)."""
    if sys.stderr.isatty():
        line = f"    {100 * fraction:5.1f}%  {machined.describe(stage, info, korean=False)}"
        sys.stderr.write("\r" + line[:78].ljust(78))
        sys.stderr.flush()


def clear_progress():
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 78 + "\r")
        sys.stderr.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="STEP/IGES files, folders or wildcards")
    ap.add_argument("--out", default="out_machined")
    ap.add_argument("--cpu", default=None, metavar="N",
                    help="processor cores the engine may use: a number, a percentage (75%%) or all "
                         "(default: all)")
    ap.add_argument("--priority", choices=["low", "normal"], default=None,
                    help="low lets other programs go first (default: normal)")
    a = ap.parse_args()
    try:
        cpu.apply(cpu.parse(a.cpu) if a.cpu else None, (a.priority == "low") if a.priority else None)
    except ValueError as e:
        raise SystemExit(f"--cpu: {e}")
    files = expand(a.files)
    missing = [f for f in files if not os.path.isfile(f)]
    if not files or missing:
        raise SystemExit("file not found: " + ", ".join(missing or a.files)
                         + "\n(put paths with spaces or parentheses in double quotes)")
    os.makedirs(a.out, exist_ok=True)
    rows = []
    for n, f in enumerate(files, 1):
        base = os.path.splitext(os.path.basename(f))[0]
        t0 = time.time()
        row = {"file": f, "holes": "", "fillets": "", "chamfers": "", "faces": "", "features": "", "identical": "", "xor_pct": "", "sketches_constrained": "",
               "seconds": "", "error": ""}
        try:
            ref = analyze.load_shape(f)
            sess, rep = machined.build(ref, show_progress)
            clear_progress()
            saved = sess.save(os.path.join(a.out, base + ".FCStd"))
            cmp_ = sess.compare()
            sk = saved.get("sketches", {})
            row.update(holes=rep["holes_made"], fillets=rep["fillets_made"], chamfers=rep["chamfers_made"],
                       faces=rep.get("faces_made", 0),
                       features=rep["features"], identical=bool(cmp_.get("converged")),
                       xor_pct=cmp_.get("xor_pct"),
                       sketches_constrained=f"{sk.get('fully_constrained', 0)}/{sk.get('sketches', 0)}")
            rep.update(file=f, identical=row["identical"], xor_pct=row["xor_pct"], sketches=sk, features_list=sess.log)
            with open(os.path.join(a.out, base + ".machined.json"), "w", encoding="utf-8") as fh:
                json.dump(rep, fh, indent=2)
            note = f" ({len(rep['skipped'])} skipped)" if rep["skipped"] else ""
            print(f"[{n}/{len(files)}] {base}: {rep['holes_made']} holes, {rep['fillets_made']} filleted and "
                  f"{rep['chamfers_made']} chamfered edges, {rep.get('faces_made', 0)} movable faces in "
                  f"{rep['features']} features{note}, "
                  f"identical={row['identical']} xor={row['xor_pct']}%, sketches fully constrained "
                  f"{row['sketches_constrained']}")
        except Exception as e:
            clear_progress()
            row["error"] = f"{type(e).__name__}: {e}"
            print(f"[{n}/{len(files)}] {base}: ERROR {row['error']}")
        if not row["error"]:
            try:
                App.closeDocument(sess.doc.Name)
                plist, _, page = doe.write_parameter_files(os.path.join(a.out, base + ".FCStd"))
                print(f"    {len(plist)} parameters" + (f"; where they are: {page}" if page else ""))
            except Exception as e:
                print(f"    (parameter table not written: {type(e).__name__}: {e})")
        row["seconds"] = round(time.time() - t0, 1)
        rows.append(row)
        with open(os.path.join(a.out, "summary.csv"), "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            w.writeheader()
            w.writerows(rows)
    print(f"\nresults: {os.path.join(a.out, 'summary.csv')}")


if __name__ == "__main__":
    main()
