"""Adapter for the Fusion 360 Gallery reconstruction dataset (r1.0.1).

Layout: <root>/<id>.json (full timeline) and <id>.step (final shape, mm); <id>_NNNN.step are intermediate
states after extrude features (NNNN = timeline index). JSON lengths are in cm, STEP files are in mm.
Dataset license: non-commercial research use only.
"""
import glob
import json
import os

from . import analyze, dataset

EXTRA_COLS = ("gt_sketches", "gt_extrudes", "gt_ops", "gt_extents", "gt_json")


def ground_truth(json_path):
    """Summarize the human modeling history of one design."""
    with open(json_path, encoding="utf-8") as f:
        d = json.load(f)
    seq = d["sequence"]
    ents = d["entities"]
    ex = [ents[s["entity"]] for s in seq if s["type"] == "ExtrudeFeature"]
    ops = [e.get("operation", "?").replace("FeatureOperation", "") for e in ex]
    extents = [e.get("extent_type", "?").replace("FeatureExtentType", "") for e in ex]
    return {"gt_sketches": sum(1 for s in seq if s["type"] == "Sketch"), "gt_extrudes": len(ex),
            "gt_ops": "|".join(ops), "gt_extents": "|".join(extents), "gt_json": json_path}


def list_designs(root):
    """Final-shape STEP paths that have a matching JSON, sorted."""
    out = []
    for jp in sorted(glob.glob(os.path.join(root, "*.json"))):
        sp = os.path.splitext(jp)[0] + ".step"
        if os.path.exists(sp):
            out.append((sp, jp))
    return out


def scan_one(args):
    """Worker: classify one design (picklable for multiprocessing)."""
    step_path, json_path, max_faces = args
    row = {"path": step_path, "category": "error", "n_faces": 0, "volume": 0.0, "notes": ""}
    try:
        row.update(ground_truth(json_path))
        a = analyze.analyze(step_path, with_adjacency=False)
        if a["volume"] <= 0:
            row["notes"] = "zero volume"
        else:
            cat, notes = dataset.classify(a)
            row.update(category=cat, n_faces=a["n_faces"], volume=a["volume"], bbox=a["bbox"]["size"],
                       n_solids=a["n_solids"], n_solids_raw=a["n_solids_raw"],
                       notes=",".join(notes), face_types=json.dumps(a["face_type_histogram"]))
            if max_faces and a["n_faces"] > max_faces:
                row["category"] = "too_complex"
    except Exception as e:
        row["notes"] = f"{type(e).__name__}: {e}"[:120]
    return row
