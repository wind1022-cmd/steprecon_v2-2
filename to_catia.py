"""Rebuild a reconstructed model in CATIA V5 as a CATPart with history (sketches + pads + pockets).

Run it on a result of run.py while CATIA V5 is open:

    fc.cmd to_catia.py out_parts\\part.FCStd --ref C:\\parts\\part.stp

It writes part.catvbs (a CATIA macro), runs it with Windows Script Host, prints what CATIA did with each feature,
and - if CATIA could export STEP and --ref is given - compares CATIA's result with the original STEP.
Use --no-run to only write the macro and start it yourself (CATIA: Tools > Macro > Macros...).

Exported: pad, pocket, plain holes. Not yet: fillets, chamfers, patterns, revolutions and the other advanced features
(they are listed as SKIPPED)."""
import argparse
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from step_recon import analyze, catia_export, compare  # noqa: E402
import FreeCAD as App  # noqa: E402


def export(fcstd, out_dir=None):
    """Write <base>.catvbs next to the model (or into out_dir). Returns (script path, info dict)."""
    fcstd = os.path.abspath(fcstd)
    out_dir = os.path.abspath(out_dir or os.path.dirname(fcstd))
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(fcstd))[0]
    doc = App.openDocument(fcstd)
    try:
        feats, skipped = catia_export.collect(doc)
        tips = [b.Tip.Shape for b in doc.Objects if b.TypeId == "PartDesign::Body" and b.Tip is not None]
        diag = max([s.BoundBox.DiagonalLength for s in tips] or [100.0])
    finally:
        App.closeDocument(doc.Name)
    paths = {k: os.path.join(out_dir, base + ext) for k, ext in
             (("script", ".catvbs"), ("log", ".catia.log"), ("part", ".CATPart"), ("step", ".catia.stp"))}
    text = catia_export.build_script(feats, skipped, paths["log"], paths["part"], paths["step"], diag)
    with io.open(paths["script"], "w", encoding="utf-16", newline="") as f:      # cscript reads UTF-16 with BOM
        f.write(text)
    return paths["script"], {"features": len(feats), "skipped": skipped, **paths}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fcstd", help="reconstructed model (.FCStd) written by run.py")
    ap.add_argument("--ref", help="original STEP/IGES file: compare CATIA's result with it")
    ap.add_argument("--out", help="folder for the macro, the CATPart and the log (default: next to the model)")
    ap.add_argument("--no-run", action="store_true", help="only write the macro")
    a = ap.parse_args()
    if not os.path.isfile(a.fcstd):
        raise SystemExit(f"file not found: {a.fcstd}")
    script, info = export(a.fcstd, a.out)
    print(f"macro: {script}")
    print(f"features to rebuild: {info['features']}" + (f", skipped: {len(info['skipped'])}" if info["skipped"] else ""))
    for name, why in info["skipped"]:
        print(f"  SKIPPED {name}: {why}")
    if not info["features"]:
        raise SystemExit("nothing to export (no pad/pocket/hole features in this model)")
    if a.no_run:
        print("not run (--no-run). In CATIA: Tools > Macro > Macros... > Macro libraries > select the folder, "
              "then run the .catvbs file; or:  cscript //nologo \"" + script + "\"")
        return
    for p in (info["log"], info["step"]):
        if os.path.exists(p):
            os.remove(p)
    try:
        rc = subprocess.call(["cscript", "//nologo", "//E:vbscript", script])
    except OSError as e:
        raise SystemExit(f"could not start cscript: {e}")
    if os.path.exists(info["log"]):
        with io.open(info["log"], encoding="utf-16") as f:
            print(f.read().rstrip())
    else:
        print(f"no log written (cscript exit code {rc})")
    if rc == 2:
        raise SystemExit("CATIA is not running: start CATIA V5 and run this command again")
    if a.ref and os.path.exists(info["step"]):
        r = compare.compare(analyze.load_shape(a.ref), analyze.load_shape(info["step"]))
        print(f"CATIA result vs original: converged={r['converged']} xor={r['xor_pct']}% "
              f"faces={r['new_faces']}/{r['ref_faces']}")
    elif a.ref:
        print("CATIA did not export a STEP file, so the result could not be compared with the original; "
              "the per-feature mismatch values above are the check")


if __name__ == "__main__":
    main()
