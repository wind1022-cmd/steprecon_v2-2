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


def mesh_volume(shape):
    """Volume from the triangles of the faces. The integration behind Shape.Volume is sometimes off for solids the kernel
    just made (seen: +0.07% on a gear after chamfers, 33 mm3 on 47000, while the triangles gave the right value).
    A deflection below 0.01 changes nothing for the volume and makes it ten times slower."""
    defl = max(shape.BoundBox.DiagonalLength * 1e-4, 0.01)
    total = 0.0
    for f in shape.Faces:
        verts, tris = f.tessellate(defl)
        for a, b, c in tris:
            total += verts[a].dot(verts[b].cross(verts[c])) / 6.0
    return total


def mesh_area(shape):
    defl = max(shape.BoundBox.DiagonalLength * 1e-4, 0.01)
    total = 0.0
    for f in shape.Faces:
        verts, tris = f.tessellate(defl)
        for a, b, c in tris:
            total += (verts[b] - verts[a]).cross(verts[c] - verts[a]).Length / 2.0
    return total


def reliable_measure(shape, max_faces=4000):
    """(volume, area, corrected) of the shape: Shape.Volume / Area, checked against the triangles (about a second for a
    thousand faces, nothing for ordinary parts). If they disagree by more than 0.02% the triangle values are used."""
    v, a = shape.Volume, shape.Area
    if len(shape.Faces) > max_faces:
        return v, a, False
    try:
        mv = mesh_volume(shape)
        if mv > 0 and abs(v - mv) > 2e-4 * abs(v):
            return mv, mesh_area(shape), True
    except Exception:
        pass
    return v, a, False


_BOXES = {}


def _face_boxes(shape):
    """[(surface type, bounding box tuple, area)] of every face. The box comes from the geometry and is the same for the
    same face in two shapes; areas and volumes of faces made by the kernel in memory can be a little off."""
    key = (shape.hashCode(), len(shape.Faces))
    if key not in _BOXES:
        out = []
        for f in shape.Faces:
            b = f.BoundBox
            out.append((type(f.Surface).__name__, (b.XMin, b.XMax, b.YMin, b.YMax, b.ZMin, b.ZMax), f.Area))
        if len(_BOXES) > 40:
            _BOXES.clear()
        _BOXES[key] = out
    return _BOXES[key]


def unmatched_pct(ref, new, tol=1e-4):
    """How much of the faces of the two shapes has no partner in the other: percent of the reference's area, both
    directions added. 0.0 = every face has a partner of the same kind with the same bounding box, i.e. the same
    faces (what a part rebuilt from the same features gives). Much faster than booleans, which take minutes when
    hundreds of faces coincide."""
    a, b = _face_boxes(ref), _face_boxes(new)
    pool = {}
    for k, (t, box, area) in enumerate(a):
        pool.setdefault((t, round(box[0] / 0.01), round(box[2] / 0.01), round(box[4] / 0.01)), []).append(k)
    used = set()
    lost_new = 0.0
    for t, box, area in b:
        hit = None
        cx, cy, cz = round(box[0] / 0.01), round(box[2] / 0.01), round(box[4] / 0.01)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for k in pool.get((t, cx + dx, cy + dy, cz + dz), ()):
                        if k not in used and all(abs(p - q) <= tol for p, q in zip(a[k][1], box)):
                            hit = k
                            break
                    if hit is not None:
                        break
                if hit is not None:
                    break
            if hit is not None:
                break
        if hit is None:
            lost_new += area
        else:
            used.add(hit)
    lost_ref = sum(a[k][2] for k in range(len(a)) if k not in used)
    total = sum(x[2] for x in a) or 1e-12
    return (lost_ref + lost_new) / total * 100.0


def compare(ref, new, tol=1e-4, quick=False, faces=True):
    """Return metrics + hints about where the shapes differ.
    Faces are matched one to one first (unmatched_pct): if all of them have a partner the shapes are the same and the
    booleans are skipped. quick=True also skips them when they differ and returns the unmatched percentage as
    xor_pct - good for deciding whether a step brought the shape closer, never for claiming that it is identical."""
    vr, vn = ref.Volume, new.Volume
    res = {"ref_volume": _r(vr), "new_volume": _r(vn),
           "volume_diff_pct": _r(abs(vr - vn) / vr * 100, 4)}
    try:
        miss = unmatched_pct(ref, new) if faces else None
    except Exception:
        miss = None
    if miss is not None and miss == 0.0:
        res.update(xor_pct=0.0, xor_method="faces", missing_volume=0.0, extra_volume=0.0, near=False,
                   ref_solids=len(ref.Solids), new_solids=len(new.Solids), ref_faces=len(ref.Faces),
                   new_faces=len(new.Faces), ref_face_types=_hist(ref), new_face_types=_hist(new), converged=True)
        return res
    if quick and miss is not None:
        res.update(xor_pct=_r(miss, 6), xor_method="faces_unmatched", near=False, ref_solids=len(ref.Solids),
                   new_solids=len(new.Solids), ref_faces=len(ref.Faces), new_faces=len(new.Faces),
                   ref_face_types=_hist(ref), new_face_types=_hist(new), converged=False)
        return res
    inconsistent = False
    try:
        # A small fuzzy tolerance makes the booleans of two nearly equal shapes (hundreds of faces that coincide) take
        # seconds instead of minutes, and it is far below any feature that matters (1e-4 mm).
        fuzz = max(1e-5, min(1e-4, 1e-6 * ref.BoundBox.DiagonalLength))
        try:
            missing = ref.cut(new, fuzz)   # material the reconstruction lacks
            extra = new.cut(ref, fuzz)     # material the reconstruction should not have
        except Exception:
            missing = ref.cut(new)
            extra = new.cut(ref)
        res["missing_volume"] = _r(missing.Volume)
        res["extra_volume"] = _r(extra.Volume)
        res["xor_pct"] = _r((missing.Volume + extra.Volume) / vr * 100, 4)
        res["xor_method"] = "boolean"
        # Exact cuts satisfy missing - extra == vr - vn. OCC sometimes gets one direction wrong on coincident
        # analytic faces (seen on real data: identical cylinders, new.cut(ref) returned all of new).
        if abs((missing.Volume - extra.Volume) - (vr - vn)) > max(1e-4 * abs(vr), tol):
            try:        # before calling the booleans wrong: are the volumes themselves right?
                mr, mn = mesh_volume(ref), mesh_volume(new)
                if abs((missing.Volume - extra.Volume) - (mr - mn)) > max(2e-4 * abs(mr), tol):
                    inconsistent = True
                else:
                    res["volume_note"] = f"Shape.Volume looks off; triangles give {mr:.3f} / {mn:.3f}"
            except Exception:
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
    # same geometry to within 0.005% of the volume but not the same faces: tiny details (an arc of a tenth of a
    # millimetre with its chamfer) that were not reproduced. Useful as a model, reported as "near", not "identical".
    res["near"] = bool(not res["converged"] and res["xor_pct"] < 0.005 and res["volume_diff_pct"] < 0.1)
    if inconsistent and res["volume_diff_pct"] >= 0.1:
        res["converged"] = False    # the sampled estimate is coarse; with unreliable booleans also demand equal volume
    return res
