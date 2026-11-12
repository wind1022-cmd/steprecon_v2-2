"""Compare a reconstructed shape against the reference (runs inside freecadcmd)."""
import random

import FreeCAD as App
import Part

ANALYTIC = ("Plane", "Cylinder", "Cone", "Sphere", "Toroid")    # FreeCAD's class name for a torus is "Toroid"


def _r(x, n=4):
    return round(float(x), n)


def _hist(shape):
    h = {}
    for f in shape.Faces:
        t = type(f.Surface).__name__
        h[t] = h.get(t, 0) + 1
    return h


def _has_freeform(shape):
    return any(type(f.Surface).__name__ not in ANALYTIC for f in shape.Faces)


def sampled_xor_pct(ref, new, n=20000, seed=0):
    """Monte-Carlo estimate of XOR volume (% of ref volume) using point-in-solid tests.
    Robust against OCC boolean failures on coincident B-spline faces (but only ~ +-0.05% accurate)."""
    bb = ref.BoundBox
    bb.add(new.BoundBox)
    rnd = random.Random(seed)
    diff = 0
    for _ in range(n):
        p = App.Vector(rnd.uniform(bb.XMin, bb.XMax), rnd.uniform(bb.YMin, bb.YMax), rnd.uniform(bb.ZMin, bb.ZMax))
        if ref.isInside(p, 1e-7, True) != new.isInside(p, 1e-7, True):
            diff += 1
    box_vol = bb.XLength * bb.YLength * bb.ZLength
    return diff / n * box_vol / ref.Volume * 100


_FACES = {}


def face_count(shape):
    """Number of faces after merging faces that lie on the same surface.
    STEP files from other CAD systems usually store a cylinder as two half faces (and sometimes split planes);
    a rebuilt cylinder is one face. Counting raw faces would then never match although the shapes are identical."""
    key = (shape.hashCode(), len(shape.Faces), round(shape.Volume, 6))
    if key not in _FACES:
        n = len(shape.Faces)
        try:
            refined = shape.copy().removeSplitter()
            if refined.isValid() and abs(refined.Volume - shape.Volume) <= 2e-4 * max(1.0, abs(shape.Volume)):
                n = len(refined.Faces)
        except Exception:
            pass
        if len(_FACES) > 200:
            _FACES.clear()
        _FACES[key] = n
    return _FACES[key]


def compare(ref, new, tol=1e-4):
    """Return metrics + hints about where the shapes differ."""
    vr, vn = ref.Volume, new.Volume
    res = {"ref_volume": _r(vr), "new_volume": _r(vn),
           "volume_diff_pct": _r(abs(vr - vn) / vr * 100, 4)}
    inconsistent = False
    try:
        missing = ref.cut(new)   # material the reconstruction lacks
        extra = new.cut(ref)     # material the reconstruction should not have
        res["missing_volume"] = _r(missing.Volume)
        res["extra_volume"] = _r(extra.Volume)
        res["xor_pct"] = _r((missing.Volume + extra.Volume) / vr * 100, 4)
        res["xor_method"] = "boolean"
        # Exact cuts satisfy missing - extra == vr - vn. OCC sometimes gets one direction wrong on coincident
        # analytic faces (seen on real data: identical cylinders, new.cut(ref) returned all of new).
        if abs((missing.Volume - extra.Volume) - (vr - vn)) > max(1e-4 * abs(vr), tol):
            inconsistent = True
        for key, s in (("missing", missing), ("extra", extra)):
            if s.Volume > max(vr * 1e-6, tol):
                parts = [{"bbox": [_r(x.BoundBox.XMin, 2), _r(x.BoundBox.YMin, 2), _r(x.BoundBox.ZMin, 2),
                                   _r(x.BoundBox.XMax, 2), _r(x.BoundBox.YMax, 2), _r(x.BoundBox.ZMax, 2)],
                          "volume": _r(x.Volume)} for x in s.Solids[:8]]
                res[key + "_regions"] = parts
    except Exception as e:  # boolean can fail on bad geometry
        res["xor_error"] = str(e)
        res["xor_pct"] = 100.0
        res["xor_method"] = "boolean_failed"

    # Booleans between identical-but-distinct B-spline faces silently return garbage (e.g. common = 0).
    # With freeform faces present, cross-check with point sampling and trust it when they disagree.
    # Two stages to stay fast (a point test costs ~1 ms on B-spline solids): a coarse 400-point check that catches
    # gross boolean failures, then a 6000-point estimate only when the coarse check disagrees.
    if _has_freeform(ref) or _has_freeform(new) or res["xor_method"] == "boolean_failed" or inconsistent:
        coarse = sampled_xor_pct(ref, new, n=400)
        res["xor_coarse_check_pct"] = _r(coarse, 2)
        if abs(coarse - res["xor_pct"]) > 15.0 or inconsistent:     # inconsistent cuts are wrong by definition
            samp = sampled_xor_pct(ref, new, n=6000, seed=1)
            res["xor_boolean_pct_unreliable"] = res["xor_pct"]
            res["xor_pct"] = _r(samp, 4)
            res["xor_method"] = "sampled"
            for k in ("missing_volume", "extra_volume", "missing_regions", "extra_regions"):
                res.pop(k, None)    # boolean-derived hints are not trustworthy in this case
    res["ref_solids"], res["new_solids"] = len(ref.Solids), len(new.Solids)
    res["ref_faces"], res["new_faces"] = face_count(ref), face_count(new)
    res["ref_face_types"], res["new_face_types"] = _hist(ref), _hist(new)
    res["converged"] = bool(res["xor_pct"] < 0.1 and res["ref_faces"] == res["new_faces"])
    if inconsistent and res["volume_diff_pct"] >= 0.1:
        res["converged"] = False    # the sampled estimate is coarse; with unreliable booleans also demand equal volume
    return res
