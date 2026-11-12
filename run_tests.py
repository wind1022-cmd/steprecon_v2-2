"""Run every test script with the current interpreter and print a summary.

Use FreeCAD's python (see README.md):   <FreeCAD python> run_tests.py
Needs no API key, no GPU and no downloaded dataset.
"""
import glob
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
BAD = re.compile(r"\bFAIL\b|\bEXC\b|Traceback|AssertionError")


def main():
    only = sys.argv[1:]
    files = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    if only:
        files = [f for f in files if any(o in os.path.basename(f) for o in only)]
    failed = []
    for f in files:
        t0 = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")       # Korean text in the output, whatever the code page
        p = subprocess.run([sys.executable, f], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=ROOT, env=env)
        out = p.stdout + p.stderr
        ok = p.returncode == 0 and not BAD.search(out)
        print(f"{'PASS' if ok else 'FAIL'}  {os.path.basename(f):26s} {time.time() - t0:5.1f}s")
        if not ok:
            failed.append(f)
            print("\n".join("    " + ln for ln in out.strip().splitlines()[-15:]))
    print(f"\n{len(files) - len(failed)}/{len(files)} test files passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
