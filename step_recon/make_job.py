"""Make the dimensions in a process of its own (started by studio.Studio.make).

The long steps of the geometry kernel (taking fillets off a part with hundreds of faces, rebuilding it) are single
calls that hold the Python interpreter until they finish. Run inside the app they would freeze it: no progress, no
elapsed time, no CPU change until they are done. Here they run in a child process; the app stays responsive, keeps
the clock running and applies CPU changes to this process while it works.

    python -m step_recon.make_job job.json

job.json: {"ref": BREP file of the part as read, "fcstd": where to save, "everything": bool,
           "holes": [line keys], "blends": [face ids], "planes": [face ids]}
      or {"scan": STEP/IGES file, "ref": BREP file to write}: read the file (a big STEP can take long) and find the
           candidates; final message {"done": true, "faces": n, "cands": machined.scan(...)}
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
    from step_recon import analyze, machined

    if job.get("remove") is not None:
        ref = Part.Shape()
        ref.importBrep(job["ref"])
        if len(ref.Solids) == 1 and ref.ShapeType != "Solid":
            ref = ref.Solids[0]
        say(p=0.0, t=f"면 {len(job['remove'])}개 없애는 중")
        res = machined.remove_faces_ex(ref, job["remove"], lambda f, i, n: say(
            p=0.05 + 0.8 * f, t=f"한꺼번에는 안 되어 부분별로 없애는 중 {i}/{n}"))
        new, done, failed = res["shape"], res["done"], res["failed"]
        if done:
            new.exportBrep(job["out"])
            new.exportStep(job["step"])
            say(p=0.9, t="바꿀 수 있는 후보 다시 찾는 중")
        say(done=True, removed=done, failed=failed, added=res["added"], method=res["method"], faces=len(new.Faces), volume=new.Volume,
            cands=machined.scan(new) if done else None)
        return

    if job.get("scan"):
        say(p=0.0, t="STEP 파일 읽는 중")
        ref = analyze.load_shape(job["scan"])
        ref.exportBrep(job["ref"])
        say(p=0.5, t=f"형상 읽음: 면 {len(ref.Faces)}개 · 바꿀 수 있는 후보 찾는 중")
        say(done=True, faces=len(ref.Faces), cands=machined.scan(ref))
        return

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
        json.dumps(saved.get("sketches", {}), default=str)), converged=bool(c.get("converged")), near=bool(c.get("near")), xor_pct=c.get("xor_pct"))


if __name__ == "__main__":
    main(sys.argv[1])
