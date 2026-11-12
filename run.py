"""Rebuild STEP/IGES files as FreeCAD feature trees with an LLM.

Use FreeCAD's python (see README.md), e.g. on Windows:
  "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\python.exe" run.py part.step

Claude API:   set ANTHROPIC_API_KEY, then   run.py part.step
Local model:  run.py part.step --backend openai --model qwen2.5:14b-instruct            (Ollama on :11434)
              run.py part.step --backend openai --base-url http://localhost:1234/v1 --model <name>   (LM Studio)
"""
import argparse
import csv
import glob
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from step_recon import agent, cli  # noqa: E402

EXTS = (".step", ".stp", ".iges", ".igs")


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


def _score(r):
    """Smaller is better: converged first, then the smallest XOR."""
    x = r.get("xor_pct")
    return (not r["converged"], x if isinstance(x, (int, float)) else 1e9)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="STEP/IGES files, folders or wildcards (e.g. C:\\parts or parts\\*.step)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--turns", type=int, default=40, help="tool-call turns per attempt (default 40)")
    ap.add_argument("--attempts", type=int, default=1,
                    help="start over up to N times per file until the shape matches; the best attempt is kept in "
                         "--out, every attempt in --out/tryK (default 1)")
    ap.add_argument("--skip-done", action="store_true",
                    help="skip files that already have a converged result in --out (resume an interrupted batch)")
    cli.add_llm_args(ap)
    a = ap.parse_args()
    files = expand(a.files)
    if not files:
        raise SystemExit("no STEP/IGES files found")
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        hint = ""
        if len(a.files) > 1:    # an unquoted path with spaces arrives as several pieces
            hint = ('\nIf the path contains spaces or parentheses it was split into pieces: put the whole path in '
                    'double quotes, e.g.\n  run.py "C:\\my folder (2)\\part.stp" ...')
        raise SystemExit("file not found: " + "\n                ".join(missing) + hint)
    backend = cli.backend_from_args(a)
    kw = cli.run_kwargs(a)
    os.makedirs(a.out, exist_ok=True)
    rows = []
    for n, f in enumerate(files, 1):
        base = os.path.splitext(os.path.basename(f))[0]
        if a.skip_done:
            try:
                import json
                with open(os.path.join(a.out, base + ".recon.json"), encoding="utf-8") as fh:
                    if json.load(fh).get("converged"):
                        print(f"[{n}/{len(files)}] {base}: already converged, skipped")
                        continue
            except (OSError, ValueError):
                pass
        best, t0 = None, time.time()
        for k in range(1, max(1, a.attempts) + 1):
            d = a.out if a.attempts <= 1 else os.path.join(a.out, f"try{k}")
            try:
                r = agent.run(f, d, max_turns=a.turns, backend=backend, **kw)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if "cannot reach LLM server" in msg or "HTTP 401" in msg:
                    raise SystemExit("aborting: " + msg)      # every remaining file would fail the same way
                print(f"[{n}/{len(files)}] {base} attempt {k}: ERROR {msg[:200]}")
                continue
            r["attempt"], r["dir"] = k, d
            print(f"[{n}/{len(files)}] {base} attempt {k}/{a.attempts}: converged={r['converged']} "
                  f"xor={r['xor_pct']}% features={len(r['features'])} turns={r['turns']}")
            if best is None or _score(r) < _score(best):
                best = r
            if r["converged"]:
                break
        if best is None:
            rows.append({"file": f, "converged": False, "xor_pct": "", "attempt": "", "attempts_used": k,
                         "turns": "", "n_features": "", "seconds": round(time.time() - t0, 1)})
            continue
        if best["dir"] != a.out:        # keep the best attempt's files at the top level
            for ext in (".FCStd", ".recon.json", ".transcript.json"):
                src = os.path.join(best["dir"], base + ext)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(a.out, base + ext))
        rows.append({"file": f, "converged": best["converged"], "xor_pct": best["xor_pct"], "attempt": best["attempt"],
                     "attempts_used": k, "turns": best["turns"], "n_features": len(best["features"]),
                     "seconds": round(time.time() - t0, 1)})
        with open(os.path.join(a.out, "summary.csv"), "w", newline="", encoding="utf-8") as fh:   # rewritten each file
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    ok = sum(1 for r in rows if r["converged"])
    print(f"\n{ok}/{len(rows)} converged  (backend={backend.name}, model={getattr(backend, 'model', '')})")
    if rows:
        print(f"results: {os.path.join(a.out, 'summary.csv')}")


if __name__ == "__main__":
    main()
