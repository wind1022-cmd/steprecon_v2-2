"""Batch-evaluate the agent on files selected from an index CSV.

  python eval_batch.py index.csv --cat prismatic revolved --max-faces 40 --n 20 --out out_eval
  python eval_batch.py index.csv --n 20 --backend openai --model qwen2.5:14b-instruct --out out_local
Claude needs ANTHROPIC_API_KEY; local models need a running OpenAI-compatible server (see README.md).
Writes <out>/results.csv and prints success rate per category.
"""
import argparse
import collections
import csv
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from step_recon import cli, dataset  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--cat", nargs="*", default=["prismatic", "revolved"])
    ap.add_argument("--max-faces", type=int, default=40)
    ap.add_argument("--max-solids", type=int, default=1,
                    help="largest number of separate solids per design, counted after merging overlapping/touching "
                         "ones (default 1 = single-body only; e.g. 4 also selects multi-body designs, which the "
                         "agent builds with new_body)")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--out", default="out_eval")
    ap.add_argument("--dry", action="store_true", help="only list the selected files")
    cli.add_llm_args(ap)
    a = ap.parse_args()

    rows = dataset.read_index(a.index, set(a.cat), a.max_faces, a.max_solids)
    random.Random(a.seed).shuffle(rows)
    rows = rows[:a.n]
    print(f"selected {len(rows)} files", dict(collections.Counter(r["category"] for r in rows)))
    if a.dry:
        for r in rows:
            print(" ", r["category"], r["n_faces"], r["path"])
        return
    from step_recon import agent
    backend = cli.backend_from_args(a)      # exits with a clear message if the key is missing (use --dry to skip)
    kw = cli.run_kwargs(a)
    tag = {"backend": backend.name, "model": getattr(backend, "model", ""), "profile": kw["profile"]}
    os.makedirs(a.out, exist_ok=True)
    res_path = os.path.join(a.out, "results.csv")
    results = []
    for r in rows:
        t0 = time.time()
        head = {"path": r["path"], "category": r["category"], "n_faces": r["n_faces"],
                "n_solids": int(r.get("n_solids") or 1)}
        try:
            out = agent.run(r["path"], a.out, max_turns=a.turns, backend=backend, verbose=False, **kw)
            row = {**head, "converged": out["converged"], "xor_pct": out["xor_pct"], "turns": out["turns"],
                   "n_features": len(out["features"]), "seconds": round(time.time() - t0, 1), "error": "", **tag}
        except Exception as e:
            row = {**head, "converged": False,
                   "xor_pct": "", "turns": 0, "n_features": 0, "seconds": round(time.time() - t0, 1),
                   "error": f"{type(e).__name__}: {e}"[:150], **tag}
            if "cannot reach LLM server" in row["error"] or "HTTP 401" in row["error"]:
                print("aborting:", row["error"])       # every remaining file would fail the same way
                sys.exit(1)
        results.append(row)
        print(f"{os.path.basename(r['path'])}: converged={row['converged']} xor={row['xor_pct']} turns={row['turns']}")
        with open(res_path, "w", newline="", encoding="utf-8") as f:   # rewrite so partial runs are kept
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            w.writeheader()
            w.writerows(results)

    print("\nsuccess rate by category")
    for cat in sorted({x["category"] for x in results}):
        sub = [x for x in results if x["category"] == cat]
        ok = sum(1 for x in sub if x["converged"])
        print(f"  {cat:10s} {ok}/{len(sub)}")
    multi = [x for x in results if x["n_solids"] > 1]
    if multi:
        single = [x for x in results if x["n_solids"] <= 1]
        print(f"single-body {sum(1 for x in single if x['converged'])}/{len(single)}, "
              f"multi-body {sum(1 for x in multi if x['converged'])}/{len(multi)}")


if __name__ == "__main__":
    main()
