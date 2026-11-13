"""Make the dimensions in a process of its own (started by studio.Studio.make).

The long steps of the geometry kernel (taking fillets off a part with hundreds of faces, rebuilding it) are single
calls that hold the Python interpreter until they finish. Run inside the app they would freeze it: no progress, no
elapsed time, no CPU change until they are done. Here they run in a child process; the app stays responsive, keeps
the clock running and applies CPU changes to this process while it works.

    python -m step_recon.make_job job.json

job.json: {"ref": BREP file of the part as read, "fcstd": where to save, "everything": bool,
           "holes": [line keys], "blends": [face ids], "planes": [face ids]}
stdout, one JSON object per line: {"p": 0..1, "t": text} progress, {"t": text, "stage": "save"|"compare"},
finally {"done": true, "report": ..., "sketches": ..., "converged": bool, "xor_pct": number}."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def say(**msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main(path):
    with open(path, encoding="utf-8") as f:
        job = json.load(f)
    import FreeCAD as App  # noqa: F401  (bootstrapped by step_recon/__init__)
    import Part
    from step_recon import machined

    ref = Part.Shape()
    ref.importBrep(job["ref"])
    if len(ref.Solids) == 1 and ref.ShapeType != "Solid":
        ref = ref.Solids[0]

    def progress(f, stage, info):
        say(p=round(f, 4), t=machined.describe(stage, info))

    if job.get("everything"):
        sess, report = machined.build(ref, progress)
    else:
        sess, report = machined.build_selected(ref, [tuple(k) for k in job.get("holes", [])], job.get("blends", []),
                                               job.get("planes", []), progress)
    say(stage="save", t="모델 저장 중 (스케치에 치수 넣는 중)")
    saved = sess.save(job["fcstd"])
    say(stage="compare", t="원본과 비교하는 중")
    c = sess.compare()
    say(done=True, report=json.loads(json.dumps(report, default=str)), sketches=json.loads(
        json.dumps(saved.get("sketches", {}), default=str)), converged=bool(c.get("converged")), xor_pct=c.get("xor_pct"))


if __name__ == "__main__":
    main(sys.argv[1])
