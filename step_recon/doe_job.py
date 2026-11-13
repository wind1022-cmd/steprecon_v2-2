"""One worker of a parallel design of experiments (started several times by studio.Studio.doe_parallel).

    python -m step_recon.doe_job model.FCStd out_dir

Loads the model once, then reads one variant per line on stdin ({"index": n, "values": {name: number}, "names": [...]})
and answers each with one JSON line on stdout: the row of results.csv (run, ok, file, error, the factor values and
the measurements). Every variant starts from the model's own values. First line printed: {"ready": true}."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def say(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def measure(shape):
    bb = shape.BoundBox
    solids = shape.Solids or [shape]
    vol = sum(s.Volume for s in solids) or 1e-12
    c = [sum(getattr(s.CenterOfMass, ax) * s.Volume for s in solids) / vol for ax in "xyz"]
    return {"volume": round(vol, 4), "area": round(shape.Area, 4), "cog_x": round(c[0], 4), "cog_y": round(c[1], 4),
            "cog_z": round(c[2], 4), "size_x": round(bb.XLength, 4), "size_y": round(bb.YLength, 4),
            "size_z": round(bb.ZLength, 4)}


def main(fcstd, out_dir):
    import FreeCAD as App
    from step_recon import params as P

    doc = App.openDocument(fcstd)
    doc.recompute()
    plist = P.collect(doc)
    base = {p["name"]: p["value"] for p in plist}
    say({"ready": True})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        task = json.loads(line)
        index, values = int(task["index"]), task.get("values", {})
        row = {"run": index, "ok": False, "file": "", "error": ""}
        row.update({k: float(values.get(k, base.get(k, 0.0))) for k in task.get("names", [])})
        try:
            unknown = [k for k in values if k not in base]
            if unknown:
                raise ValueError("unknown parameter: " + ", ".join(unknown))
            P.apply(doc, plist, base)
            P.apply(doc, plist, {k: float(v) for k, v in values.items()})
            doc.recompute()
            bad = P.problems(doc)
            if bad:
                row["error"] = "; ".join(bad)[:300]
            else:
                shape = P.result_shape(doc)
                row.update(measure(shape))
                name = f"run_{index:03d}.step"
                shape.exportStep(os.path.join(out_dir, name))
                row["file"], row["ok"] = name, True
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"[:300]
        say(row)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
