"""Move the end of a part together with whatever sits on its rim (chamfers, fillets, small steps).

Pushing a flat end face outward with a Pad of its outline leaves the chamfer around its rim behind; milling it inward
cuts the chamfer away. What one usually means by "make the part 3 mm thicker" is different: the chamfer goes along.
That is a stretch: cut the part with a plane that lies below the rim, in the straight part of its walls, then

    grow   (d > 0): bottom + a slab of the cut outline, d thick + top moved by d
    shrink (d < 0): bottom cut d lower + top moved by d (the walls of that band must be straight)

The top - end face and rim - is moved as it is, so its chamfers keep their size. Pure geometry on a shape: no document
objects, no topological names; params.result_shape() applies it for every movable face that has the "with rim" option.

    stretch(shape, normal, plane, d) -> (new shape or None, info)    info: {"area", "error"}
    t = normal . point: `plane` is the position of the cutting plane along the (outward) normal of the moved end."""
import FreeCAD as App
import Part

SAME = 1e-4         # outlines of the cut at two heights count as equal if this fraction of the area overlaps


def _span(shape, n):
    """(lowest, highest) position of the shape along n (from its bounding box corners: an upper bound)."""
    bb = shape.BoundBox
    ts = [n.dot(App.Vector(x, y, z)) for x in (bb.XMin, bb.XMax) for y in (bb.YMin, bb.YMax) for z in (bb.ZMin, bb.ZMax)]
    return min(ts), max(ts)


def _cut_faces(shape, n, t):
    """Faces of `shape` that lie in the plane at position t, square to n."""
    out = []
    for f in shape.Faces:
        if type(f.Surface).__name__ != "Plane":
            continue
        u0, u1, v0, v1 = f.ParameterRange
        if abs(abs(f.normalAt((u0 + u1) / 2, (v0 + v1) / 2).dot(n)) - 1.0) > 1e-7:
            continue
        if abs(n.dot(f.CenterOfMass) - t) < 1e-5 * (1.0 + abs(t)):
            out.append(f)
    return out


def _solid(shape):
    solids = shape.Solids
    return solids[0] if len(solids) == 1 else (shape if solids else None)


def _tidy(shape):
    """Merge faces that were split by the cuts, if that keeps the volume (else leave it)."""
    try:
        refined = shape.copy().removeSplitter()
        if refined.isValid() and abs(refined.Volume - shape.Volume) <= 1e-6 * max(shape.Volume, 1.0):
            return refined
    except Exception:
        pass
    return shape


def stretch(shape, normal, plane, d):
    n = App.Vector(normal)
    n.normalize()
    info = {"area": 0.0, "error": ""}
    if abs(d) < 1e-12:
        return shape, info
    lo, hi = _span(shape, n)
    if not lo + 1e-6 < plane < hi - 1e-6:
        info["error"] = "the cutting plane is outside the part"
        return None, info
    bb = shape.BoundBox
    radius = bb.DiagonalLength + abs(d) + 1.0
    length = (hi - lo) + abs(d) + 2.0
    c = bb.Center
    p0 = c - n * (n.dot(c) - plane)                  # centre of the cutting plane
    up = shape.common(Part.makeCylinder(radius, length, p0, n))
    if up.isNull() or not up.Solids:
        info["error"] = "nothing above the cutting plane"
        return None, info
    if d > 0:
        low = shape.common(Part.makeCylinder(radius, length, p0, n * -1))
        sections = _cut_faces(low, n, plane)
        if not sections:
            info["error"] = "the cut leaves no outline"
            return None, info
        info["area"] = sum(f.Area for f in sections)
        moved = up.copy()
        moved.translate(n * d)
        parts = [f.extrude(n * d) for f in sections] + [moved]
        out = low.fuse(parts, 1e-6)
    else:
        m = -d
        low = shape.common(Part.makeCylinder(radius, length, p0 - n * m, n * -1))
        if low.isNull() or not low.Solids:
            info["error"] = "the part is not thick enough below the plane"
            return None, info
        below = _cut_faces(low, n, plane - m)        # outline m lower ...
        above = _cut_faces(up, n, plane)             # ... and the one the top comes down onto
        if not below or not above:
            info["error"] = "the cut leaves no outline"
            return None, info
        a_low, a_up = Part.makeCompound(below), Part.makeCompound(above)
        info["area"] = a_low.Area
        shifted = a_up.copy()
        shifted.translate(n * d)
        try:
            same = a_low.common(shifted).Area / max(a_low.Area, 1e-12)
        except Exception:
            same = 0.0
        if same < 1.0 - SAME or abs(a_low.Area - a_up.Area) > SAME * a_low.Area:
            info["error"] = ("the walls below the rim are not straight over %.3g mm (the outline changes): the rim "
                             "cannot come down with the face" % m)
            return None, info
        moved = up.copy()
        moved.translate(n * d)
        out = low.fuse([moved], 1e-6)
    out = _solid(out)
    if out is None or not out.isValid():
        info["error"] = "the stretched part is not a valid solid"
        return None, info
    return _tidy(out), info
