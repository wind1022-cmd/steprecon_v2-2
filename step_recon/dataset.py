"""Scan a folder of STEP/IGES files, classify each part, and write an index used for batch evaluation."""
import csv
import json
import math
import os

from . import analyze

EXTS = (".step", ".stp", ".iges", ".igs")
ANALYTIC = set(analyze.ANALYTIC)


def _axis_line(face):
    return (face["center"] if "center" in face else None, face.get("axis"))


def _same_axis(a, b, tol=1e-3):
    """Axis (point, direction) pairs describe the same line."""
    (p1, d1), (p2, d2) = a, b
    cross = [d1[1] * d2[2] - d1[2] * d2[1], d1[2] * d2[0] - d1[0] * d2[2], d1[0] * d2[1] - d1[1] * d2[0]]
    if math.sqrt(sum(c * c for c in cross)) > tol:
        return False
    w = [p2[i] - p1[i] for i in range(3)]
    c2 = [d1[1] * w[2] - d1[2] * w[1], d1[2] * w[0] - d1[0] * w[2], d1[0] * w[1] - d1[1] * w[0]]
    return math.sqrt(sum(c * c for c in c2)) < 0.05


def classify(summary):
    """Return (category, notes). category in prismatic | revolved | mixed | freeform."""
    faces = summary["faces"]
    types = {f["type"] for f in faces}
    if not types <= ANALYTIC:
        return "freeform", sorted(types - ANALYTIC)

    # revolved: every curved face shares a single axis and every plane is perpendicular to it
    curved = [f for f in faces if f["type"] in ("Cylinder", "Cone", "Toroid") and "axis" in f]
    if curved and not any(f["type"] == "Sphere" for f in faces):
        ref_axis = (curved[0].get("center", [0, 0, 0]), curved[0]["axis"])
        if all(_same_axis(ref_axis, (f.get("center", [0, 0, 0]), f["axis"])) for f in curved):
            d = ref_axis[1]
            planes_ok = all(abs(abs(sum(a * b for a, b in zip(f["normal"], d))) - 1) < 1e-3
                            for f in faces if f["type"] == "Plane")
            if planes_ok:
                return "revolved", []

    # prismatic: planes + cylinders whose axes are parallel to a common direction (cones/tori = chamfer/fillet)
    cyl = [f for f in faces if f["type"] == "Cylinder"]
    if not any(f["type"] in ("Sphere",) for f in faces):
        if all(abs(abs(sum(a * b for a, b in zip(cyl[0]["axis"], f["axis"]))) - 1) < 1e-3 for f in cyl) if cyl else True:
            return "prismatic", []
    return "mixed", []


def scan(folder, max_faces=None, limit=None):
    rows = []
    for root, _, files in os.walk(folder):
        for fn in sorted(files):
            if not fn.lower().endswith(EXTS):
                continue
            path = os.path.join(root, fn)
            row = {"path": path, "category": "error", "n_faces": 0, "volume": 0.0, "notes": ""}
            try:
                a = analyze.analyze(path, with_adjacency=False)
                if a["volume"] <= 0:
                    row["notes"] = "zero/negative volume"
                else:
                    cat, notes = classify(a)
                    row.update(category=cat, n_faces=a["n_faces"], volume=a["volume"],
                               n_solids=a["n_solids"], n_solids_raw=a["n_solids_raw"],
                               bbox=a["bbox"]["size"], notes=",".join(notes),
                               face_types=json.dumps(a["face_type_histogram"]))
                    if max_faces and a["n_faces"] > max_faces:
                        row["category"] = "too_complex"
            except Exception as e:
                row["notes"] = f"{type(e).__name__}: {e}"[:120]
            rows.append(row)
            if limit and len(rows) >= limit:
                return rows
    return rows


def write_index(rows, out_csv, extra_cols=()):
    cols = ["path", "category", "n_faces", "volume", "n_solids", "n_solids_raw", "bbox", "face_types", "notes",
            *extra_cols]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_index(csv_path, categories=None, max_faces=None, max_solids=None):
    """Rows of an index CSV, optionally filtered. Older indexes without n_solids are treated as 1 solid."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        if categories and r["category"] not in categories:
            continue
        if max_faces and int(r["n_faces"]) > max_faces:
            continue
        if max_solids and int(r.get("n_solids") or 1) > max_solids:
            continue
        out.append(r)
    return out
