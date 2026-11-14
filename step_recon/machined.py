"""Turn what can be expressed of a complex part into editable features - no model involved.

For cast or moulded parts (housings, covers) a full feature tree is out of reach, but the things one wants to vary
are local: hole sizes and positions, fillet radii, chamfer sizes. This module recognises them in the reference,
removes them to get a "blank" (kept as a frozen, non-parametric base) and puts each one back as a feature:

    ImportedBlank -> Hole_D8_x6 -> Hole_D25 -> Fillet_R3 -> Chamfer_1 -> ...

How:
  1. fillets and chamfers are taken off with the CAD kernel's defeaturing (faces removed, neighbours extended);
  2. complete round holes are filled with plugs;
  3. the holes are cut again as dimensioned pockets, the fillets and chamfers applied again as features, with
     sizes measured from the reference.
Nothing is taken on trust: every step is checked against the reference, and whatever does not come back exactly is
left in the blank untouched. So the result always equals the reference; only the number of features varies.

Recognised: complete concave cylinders (through or blind, any direction, each step of a counterbore);
constant-radius fillets (straight, around circular edges, with their corner patches); chamfers (equal legs or two
different distances);
flat faces as movable faces (thickness, height): those with straight surroundings, and machined lands
bounded by chamfers, fillets or drafted walls that fall away from them.
Left in the blank: everything else (drill points, slots, threads, variable fillets, freeform shapes, drafts)."""
import math

import FreeCAD as App
import Part

from . import fc_tools, sharpen, stretch

TWO_PI = 2 * math.pi


def _canon(v):
    """Same direction for both orientations of an axis: the dominant component positive."""
    big = max(range(3), key=lambda i: abs(v[i]))
    return v if v[big] > 0 else v * -1


# ------------------------------------------------------------------ holes
def find_holes(ref, min_radius=0.2):
    """Complete concave cylinders of the shape: [{"axis", "foot", "radius", "t0", "t1", "faces"}], where a point
    of the axis is foot + axis * t and the hole runs from t0 to t1."""
    groups = {}
    for i, f in enumerate(ref.Faces):
        s = f.Surface
        if type(s).__name__ != "Cylinder" or s.Radius < min_radius:
            continue
        u0, u1, v0, v1 = f.ParameterRange
        um, vm = (u0 + u1) / 2, (v0 + v1) / 2
        pt = f.valueAt(um, vm)
        to_axis = s.Center - pt
        to_axis = to_axis - s.Axis * to_axis.dot(s.Axis)
        if f.normalAt(um, vm).dot(to_axis) <= 0:        # convex: a boss or shaft, not a hole
            continue
        a = _canon(App.Vector(s.Axis).normalize())
        foot = s.Center - a * s.Center.dot(a)
        t = sorted(((f.valueAt(um, v) - foot).dot(a)) for v in (v0, v1))
        key = (round(a.x, 5), round(a.y, 5), round(a.z, 5), round(foot.x, 3), round(foot.y, 3), round(foot.z, 3),
               round(s.Radius, 4), round(t[0], 3), round(t[1], 3))
        g = groups.setdefault(key, {"axis": a, "foot": foot, "radius": s.Radius, "t0": t[0], "t1": t[1],
                                    "span": 0.0, "faces": []})
        g["span"] += abs(u1 - u0)
        g["faces"].append(i)
    holes = [g for g in groups.values() if g["span"] >= TWO_PI - 1e-6 and g["t1"] - g["t0"] > 1e-6]
    holes.sort(key=lambda h: (-h["radius"], h["t0"]))
    return holes


def _hole_key(h):
    """Identity of a hole that survives rebuilding the shape (face numbers do not)."""
    return (round(h["axis"].x, 4), round(h["axis"].y, 4), round(h["axis"].z, 4), round(h["foot"].x, 3),
            round(h["foot"].y, 3), round(h["foot"].z, 3), round(h["radius"], 4), round(h["t0"], 3), round(h["t1"], 3))


def _line_key(h):
    """Identity of a hole that also survives taking its rim chamfer off (which changes its length): axis line and
    radius only."""
    return (round(h["axis"].x, 4), round(h["axis"].y, 4), round(h["axis"].z, 4), round(h["foot"].x, 3),
            round(h["foot"].y, 3), round(h["foot"].z, 3), round(h["radius"], 4))


def _plug(h):
    start = h["foot"] + h["axis"] * h["t0"]
    return Part.makeCylinder(h["radius"], h["t1"] - h["t0"], start, h["axis"])


def _open_end(ref, h):
    """Which end of the hole to drill from: the one with nothing beyond it. Returns (t of that end, direction
    pointing out of the hole there)."""
    eps = max(1e-3, 1e-4 * (h["t1"] - h["t0"]))
    for t, out in ((h["t1"], h["axis"]), (h["t0"], h["axis"] * -1)):
        probe = h["foot"] + h["axis"] * t + out * eps
        if not ref.isInside(probe, 1e-7, False):
            return t, out
    return h["t1"], h["axis"]


# ------------------------------------------------------------------ fillets and chamfers
def _neighbours(shape):
    """face index -> [(edge, other face index)] via shared edges."""
    by_edge = {}
    for i, f in enumerate(shape.Faces):
        for e in f.Edges:
            by_edge.setdefault(e.hashCode(), []).append((e, i))
    out = {i: [] for i in range(len(shape.Faces))}
    for items in by_edge.values():
        for a in range(len(items)):
            for b in range(len(items)):
                if a != b and items[a][1] != items[b][1] and items[a][0].isSame(items[b][0]):
                    out[items[a][1]].append((items[a][0], items[b][1]))
    return out


def _normal_at(face, p):
    try:
        return face.normalAt(*face.Surface.parameter(p))
    except Exception:
        return None


def _edge_mid(e):
    return e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)


MIN_TURN = math.radians(8.0)     # smallest angle a fillet's section turns through (see find_blends)


def find_blends(shape):
    """Faces that look like dress-up features: {("fillet", radius) | ("chamfer",): [face indices]}.

    fillet : a partial cylinder / torus / sphere that runs smoothly (tangent) into at least two different
             neighbours which are not the same surface (that would be the other half of a hole or shaft);
    chamfer: a narrow plane or a cone that meets two neighbours along its long edges at sharp angles (the same
             angle for an equal-leg chamfer, different ones for a chamfer with two distances).
    This is only a first guess - build() keeps a group only if putting it back reproduces the reference."""
    faces = shape.Faces
    nb = _neighbours(shape)
    groups = {}
    for i, f in enumerate(faces):
        s = f.Surface
        kind = type(s).__name__
        if kind in ("Cylinder", "Toroid", "Sphere"):
            radius = s.MinorRadius if kind == "Toroid" else s.Radius
            u0, u1, v0, v1 = f.ParameterRange
            if kind == "Cylinder" and abs(u1 - u0) > math.pi + 1e-6:
                continue                                    # more than half a turn: a hole, boss or shaft
            if kind == "Toroid" and abs(v1 - v0) > math.pi + 1e-6:
                continue
            turn = abs(u1 - u0) if kind == "Cylinder" else abs(v1 - v0) if kind == "Toroid" else math.pi
            smooth = set()
            for e, j in nb[i]:
                g = faces[j]
                gs = g.Surface
                if type(gs).__name__ == kind == "Cylinder" and abs(gs.Radius - s.Radius) < 1e-6 \
                        and gs.Axis.cross(s.Axis).Length < 1e-6 \
                        and (gs.Center - s.Center).cross(s.Axis).Length < 1e-6:
                    smooth = set()
                    break                                   # the other half of the same cylinder
                p = _edge_mid(e)
                n1, n2 = _normal_at(f, p), _normal_at(g, p)
                if n1 is not None and n2 is not None and n1.getAngle(n2) < math.radians(1.0):
                    smooth.add(j)
            if len(smooth) >= 2 and turn < MIN_TURN:
                # turns only a few degrees and runs into smaller rounds on both sides: a land between two rounds (the
                # tip / root of a gear's tooth stored as a cylinder of the tip radius, seen as "fillet R42.55"), not a
                # fillet. A small fillet that turns little (between two nearly parallel faces) has flat neighbours.
                def rounder(j):
                    gs = faces[j].Surface
                    k = type(gs).__name__
                    if k == "Cylinder":
                        return gs.Radius < 0.5 * radius
                    if k == "Toroid":
                        return gs.MinorRadius < 0.5 * radius
                    return k not in ("Plane", "Cone", "Sphere", "SurfaceOfExtrusion")
                if sum(1 for j in smooth if rounder(j)) >= 2:
                    continue
            if len(smooth) >= 2:
                groups.setdefault(("fillet", round(radius, 4)), []).append(i)
        else:       # plane, cone, or the ruled / spline strip of a chamfer along a curved edge (gear flanks ...)
            edges = sorted(f.Edges, key=lambda e: -e.Length)
            if len(edges) < 2 or (kind != "Cone" and len(f.Edges) != 4):
                continue
            long_edges = edges[:2]
            if kind != "Cone":
                width = f.Area / max(long_edges[0].Length, 1e-9)
                if width > 0.35 * long_edges[0].Length:
                    continue                                # not a narrow strip
            angles = []
            for e, j in nb[i]:
                if not any(e.isSame(x) for x in long_edges):
                    continue
                p = _edge_mid(e)
                n1, n2 = _normal_at(f, p), _normal_at(faces[j], p)
                if n1 is not None and n2 is not None:
                    angles.append(n1.getAngle(n2))
            if len(angles) == 2 and all(math.radians(10) < a < math.radians(80) for a in angles):
                groups.setdefault(("chamfer",), []).append(i)
    return groups


def _sane(old, new):
    """Taking small features off must not blow the part up or move its bounding box: the kernel sometimes returns a
    solid that passes isValid() and is garbage (seen: 8 times the volume after minutes of work)."""
    try:
        if new.isNull() or not new.isValid() or new.Volume <= 0:
            return False
        bb, nb_ = old.BoundBox, new.BoundBox
        slack = 0.05 * bb.DiagonalLength
        if (nb_.XMin < bb.XMin - slack or nb_.XMax > bb.XMax + slack or nb_.YMin < bb.YMin - slack
                or nb_.YMax > bb.YMax + slack or nb_.ZMin < bb.ZMin - slack or nb_.ZMax > bb.ZMax + slack):
            return False
        return new.Volume <= bb.XLength * bb.YLength * bb.ZLength * (1 + 1e-6) + 1e-6
    except Exception:
        return False


def _defeature(shape, face_ids):
    """Shape without those faces, or None if that cannot be done cleanly. Parts that are an extrusion with features on
    their ends (gears, splines, flanges) are rebuilt from their outline (sharpen.py): exact, a second instead of
    minutes. The faces that had to go with the picked ones (the rim of the ends) are left in _defeature.extra.
    Anything else goes to the kernel's defeaturing, and its answer is checked."""
    _defeature.extra = []
    faces = shape.Faces
    try:
        new, info = sharpen.rebuild_prism(shape, face_ids, faces)
    except Exception:
        new, info = None, ""
    if new is not None:
        _defeature.extra = sorted(set(info["removed"]) - set(face_ids))
        return new
    try:
        new, gone = sharpen.fill_wedges(shape, face_ids, faces)
    except Exception:
        new, gone = None, []
    if new is not None:
        left = [i for i in face_ids if i not in set(gone)]
        if not left:
            return new
        rest = _find_again(faces, left, new)
        more = _take_off(new, [new.Faces[j] for j in rest]) if len(rest) == len(left) else None
        return more                                 # None: the remaining faces resist; the caller tries smaller groups
    try:
        out = shape.defeaturing([faces[i] for i in face_ids])
    except Exception:
        return None
    if out.isNull() or not out.isValid() or len(out.Solids) != len(shape.Solids) or len(out.Faces) >= len(shape.Faces):
        return None
    if not _sane(shape, out) or abs(out.Volume - shape.Volume) > 0.2 * shape.Volume:    # a dress-up never changes the volume that much
        return None
    return out.Solids[0] if len(out.Solids) == 1 else out


_defeature.extra = []


# ------------------------------------------------------------------ progress
STAGES = {      # stage -> (English, Korean)
    "blends": ("finding fillets and chamfers", "필렛·모따기 찾는 중"),
    "defeature": ("taking fillets and chamfers off", "필렛·모따기 떼어 내는 중"),
    "holes": ("turning holes into features", "구멍을 피처로 만드는 중"),
    "dress": ("putting fillets and chamfers back", "필렛·모따기 다시 붙이는 중"),
    "faces": ("checking flat faces", "평면 확인 중"),
}
_progress = None        # callable(fraction 0..1, stage, info) while build() runs
_span = (0.0, 1.0)      # part of the whole that the running step covers
_last = 0.0             # never report less than before
_try = {}               # which attempt is running


def _tell(stage, frac=0.0, **info):
    global _last
    if _progress is None:
        return
    lo, hi = _span
    _last = max(_last, lo + (hi - lo) * max(0.0, min(1.0, frac)))
    info.update(_try)
    try:
        _progress(_last, stage, info)
    except Exception:       # a broken display must not stop the work
        pass


def describe(stage, info, korean=True):
    """One line for a progress report, e.g. '구멍을 피처로 만드는 중 4/7 (시도 2/3)'."""
    text = STAGES.get(stage, (stage, stage))[1 if korean else 0]
    if info.get("n"):
        text += f" {info.get('i', 0)}/{info['n']}"
    if info.get("attempts", 1) > 1:
        text += f" ({'시도' if korean else 'try'} {info['attempt']}/{info['attempts']})"
    return text


# ------------------------------------------------------------------ building
def _cut_holes(sess, target, report, exclude, only=None):
    """Fill the holes of `target`, use that as blank and cut them again. Returns the set of holes that failed.
    only: line keys (_line_key) of the holes to treat; the others stay in the blank. None = all."""
    holes = []
    for h in find_holes(target):
        if only is not None and _line_key(h) not in only:
            continue
        key = _hole_key(h)
        if key in exclude:
            continue
        plug = _plug(h)
        try:
            overlap = plug.common(target).Volume
        except Exception:
            overlap = plug.Volume
        if overlap > 1e-6 * plug.Volume:        # something stands inside this cylinder: not a plain drilled hole
            report["skipped"].append(f"hole D{2 * h['radius']:g}: not empty inside")
            continue
        h["key"], h["plug"] = key, plug
        h["end"], h["out"] = _open_end(target, h)
        holes.append(h)
    if not holes:
        sess.import_shape(target, "blank (no holes turned into features)")
        return set()
    blank = target.fuse([h["plug"] for h in holes])
    try:
        refined = blank.copy().removeSplitter()
        if refined.isValid() and abs(refined.Volume - blank.Volume) <= 2e-4 * max(1.0, blank.Volume):
            blank = refined
    except Exception:
        pass
    if len(blank.Solids) == 1:
        blank = blank.Solids[0]
    sess.import_shape(blank, f"blank ({len(holes)} holes filled)")

    jobs = {}       # equal holes drilled from the same face in the same direction -> one feature
    for h in holes:
        plane_off = (h["foot"] + h["axis"] * h["end"]).dot(h["out"])
        key = (round(h["out"].x, 5), round(h["out"].y, 5), round(h["out"].z, 5), round(plane_off, 3),
               round(h["radius"], 4), round(h["t1"] - h["t0"], 3))
        jobs.setdefault(key, []).append(h)
    failed = set()
    for gi, group in enumerate(jobs.values()):
        _tell("holes", 0.1 + 0.5 * gi / len(jobs), i=gi + 1, n=len(jobs))
        h0 = group[0]
        n = h0["out"]
        entry0 = h0["foot"] + h0["axis"] * h0["end"]
        plane = {"origin": list(n * entry0.dot(n)), "normal": [n.x, n.y, n.z]}
        o, x, y, _ = sess._frame(plane)
        plane["x_dir"] = [x.x, x.y, x.z]
        circles = []
        for h in group:
            e = h["foot"] + h["axis"] * h["end"] - o
            circles.append({"type": "circle", "c": [e.dot(x), e.dot(y)], "r": h["radius"]})
        current = sess._tip_shape()
        before = current.Volume
        try:        # what is still there to remove (an earlier, crossing hole may have taken part of it already)
            want = sum(h["plug"].common(current).Volume for h in group)
        except Exception:
            want = sum(h["plug"].Volume for h in group)
        depth = h0["t1"] - h0["t0"]
        ok = False
        for attempt in (0, 1):
            if attempt == 1:    # second try: sketch on the far end of the hole, cutting back toward the entry
                far = {"origin": list(n * -1 * (entry0 - n * depth).dot(n * -1)), "normal": [-n.x, -n.y, -n.z],
                       "x_dir": plane["x_dir"]}
                o2, x2, y2, _ = sess._frame(far)
                circles = []
                for h in group:
                    e = h["foot"] + h["axis"] * h["end"] - n * depth - o2
                    circles.append({"type": "circle", "c": [e.dot(x2), e.dot(y2)], "r": h["radius"]})
                plane = far
            r = sess.pocket(plane, circles, depth=depth)
            if r.get("ok"):
                removed = before - sess.body.Tip.Shape.Volume
                if abs(removed - want) <= 5e-3 * max(want, 1e-9):
                    ok = True
                    break
                sess.undo()
        if not ok:
            failed.update(h["key"] for h in group)
            continue
        feat = sess.doc.getObject(r["feature"])
        d = round(2 * h0["radius"], 3)
        feat.Label = f"Hole_D{d:g}" + (f"_x{len(group)}" if len(group) > 1 else "")
        report["holes_made"] += len(group)
        report["features"] += 1
    return failed


def _dress(sess, report):
    """Put fillets and chamfers back: the sharp edges of the solid that the reference lacks, with the sizes
    measured there (Session.list_edges). Each group must bring the solid closer to the reference - by the matched
    faces AND locally: where each sharp edge was, the new solid must have material exactly where the reference has.
    Next to seams left by a removal the face count alone can be fooled (seen: the same wrong chamfer accepted six
    times on the short edges at the ends of gear-tooth strips). A suggestion is applied once only."""
    applied = set()

    def mids(ids):
        s = sess.result_shape()
        out = []
        for i in ids:
            try:
                e = s.Edges[i]
                out.append(e.valueAt((e.FirstParameter + e.LastParameter) / 2.0))
            except Exception:
                pass
        return out

    def agrees(points):
        s = sess.result_shape()
        if s is None:
            return False
        bad = sum(1 for p in points if sess.ref.isInside(p, 1e-6, False) != s.isInside(p, 1e-6, False))
        return bad <= len(points) // 4

    for k in range(40):
        todo = [e for e in sess.list_edges(only_missing=True).get("edges", []) if e.get("suggest")]
        todo = [e for e in todo if str(e["suggest"]) not in report["_refused"] and str(e["suggest"]) not in applied]
        if not todo:
            return
        left = len({str(e["suggest"]) for e in todo})
        _tell("dress", 0.6 + 0.4 * k / (k + left), i=k + 1, n=k + left)
        # largest first: a big fillet has to exist before a small one can run over its end
        first = max((e["suggest"] for e in todo), key=lambda s: max(list(s.values())[0]) if isinstance(
            list(s.values())[0], list) else list(s.values())[0])
        ids = [e["edge"] for e in todo if e["suggest"] == first]
        before = sess.compare(quick=True)["xor_pct"]       # quick: faces matched one to one, no booleans
        points = mids(ids)
        kind, size = list(first.items())[0]
        if kind == "chamfer2":      # the legs are listed for the edge's first / second face: the measured orientation
            flip = fc_tools.chamfer2_flip()
            r = sess.chamfer2(ids, size[0], size[1], flip)
            if r.get("ok") and not sess.compare(quick=True)["xor_pct"] < before - 1e-9:
                sess.undo()                 # not better: the other orientation, in case the convention differs here
                r = sess.chamfer2(ids, size[0], size[1], not flip)
        else:
            r = sess.fillet(ids, size) if kind == "fillet" else sess.chamfer(ids, size)
        if r.get("ok") and agrees(points) and sess.compare(quick=True)["xor_pct"] < before - 1e-9:
            applied.add(str(first))
            feat = sess.doc.getObject(r["feature"])
            feat.Label = (f"Fillet_R{size:g}" if kind == "fillet" else f"Chamfer_{size:g}" if kind == "chamfer"
                          else f"Chamfer_{max(size):g}x{min(size):g}") + (f"_x{len(ids)}" if len(ids) > 1 else "")
            report[("chamfer" if kind == "chamfer2" else kind) + "s_made"] += len(ids)
            report["features"] += 1
        else:
            if r.get("ok"):
                sess.undo()
            report["_refused"].add(str(first))


def _thickness(shape, face, normal):
    """Size of the part straight behind a planar face (plate thickness, boss height, overall width): probed at
    several points, the value met most often wins (under a boss or a chamfer it differs locally). 0 if unknown."""
    try:
        verts, tris = face.tessellate(max(shape.BoundBox.DiagonalLength * 0.01, 1e-3))
        reach = shape.BoundBox.DiagonalLength * 1.1
        found = []
        step = max(1, len(tris) // 24)
        for a, b, c in tris[::step][:24]:
            p = (verts[a] + verts[b] + verts[c]) * (1.0 / 3.0)
            line = Part.makeLine(p - normal * 1e-4, p - normal * reach)
            # distance to where the probe leaves the part for the last time: the size of the part behind the
            # face, not interrupted by holes or pockets on the way
            far = 0.0
            for e in line.common(shape).Edges:
                for v in (e.valueAt(e.FirstParameter), e.valueAt(e.LastParameter)):
                    far = max(far, v.distanceToPoint(p))
            if far > 1e-3:
                found.append(round(far, 3))
        if not found:
            return 0.0
        counts = {}
        for v in found:
            counts[v] = counts.get(v, 0) + 1
        top = max(counts.values())
        return min(v for v, k in counts.items() if k == top)
    except Exception:
        return 0.0


def _edges_to_geometry(edges, loc):
    """Line / arc / circle edges -> sketch geometry items, `loc` mapping a 3D point to sketch (x, y)."""
    geo = []
    for e in edges:
        c = e.Curve
        t = type(c).__name__
        a, b = e.FirstParameter, e.LastParameter
        if t in ("Line", "LineSegment"):
            geo.append({"type": "line", "p1": loc(e.valueAt(a)), "p2": loc(e.valueAt(b))})
        elif t == "Circle":
            cen = loc(c.Center)
            if e.isClosed() or abs(abs(b - a) - TWO_PI) < 1e-9:
                geo.append({"type": "circle", "c": cen, "r": c.Radius})
                continue

            def ang(q):
                return math.degrees(math.atan2(q[1] - cen[1], q[0] - cen[0])) % 360.0

            s0, m, s1 = ang(loc(e.valueAt(a))), ang(loc(e.valueAt((a + b) / 2))), ang(loc(e.valueAt(b)))
            a0, a1 = (s0, s1) if (m - s0) % 360.0 <= (s1 - s0) % 360.0 else (s1, s0)
            if a1 <= a0:
                a1 += 360.0
            geo.append({"type": "arc", "c": cen, "r": c.Radius, "a0": a0, "a1": a1})
        else:
            raise ValueError("outline with a " + t)
    return geo


def _straight_face(face, n, tol=1e-3):
    """True if the face runs along n: its normal is square to n everywhere (planes, cylinders, extrusions along n)."""
    u0, u1, v0, v1 = face.ParameterRange
    for a in (0.15, 0.5, 0.85):
        for b in (0.15, 0.5, 0.85):
            try:
                if abs(face.normalAt(u0 + (u1 - u0) * a, v0 + (v1 - v0) * b).dot(n)) > tol:
                    return False
            except Exception:
                return False
    return True


def _rim_plan(shape, nb, i, n, thick, faces=None):
    """Where to cut the part so that the rim around flat end face i (chamfers, fillets) can move with it:
    the position of the cutting plane along n, or None if the face is not an end of the part, has no rim, or the rim
    is too deep. The plane lies a little below the lowest point of the rim, in the straight part of the walls."""
    faces = faces if faces is not None else shape.Faces
    f = faces[i]
    tf = n.dot(f.CenterOfMass)
    bb = shape.BoundBox
    c, big = bb.Center, bb.DiagonalLength + 1.0
    try:        # an end face: nothing of the part lies beyond it
        above = shape.common(Part.makeCylinder(big, 2 * big, c - n * (n.dot(c) - (tf + 1e-4)), n))
        if above.Volume > 1e-9 * shape.Volume:
            return None
    except Exception:
        return None
    rim = {j for _, j in nb[i] if not _straight_face(faces[j], n)}
    todo = list(rim)
    while todo and len(rim) < 3000:
        j = todo.pop()
        for _, k in nb[j]:
            if k != i and k not in rim and not _straight_face(faces[k], n):
                rim.add(k)
                todo.append(k)
    if not rim or len(rim) >= 3000:
        return None
    depth = max(tf - n.dot(v.Point) for j in rim for v in faces[j].Vertexes)
    gap = max(0.25 * depth, 0.05)
    if depth < 1e-6 or depth + gap > 0.6 * thick:
        return None
    return tf - depth - gap


def _surroundings(shape, nb, i, n, faces=None, want_reach=True):
    """How the faces around planar face i behave, seen from that face:
    "straight" - all of them run along the normal (planes square to it, cylinders parallel to it);
    "land"     - none rises above the face (chamfers, fillets, drafted or stepped walls falling away from it);
    None       - something rises above it without being straight: the face cannot simply be moved.
    For a land also the widest in-plane reach of the non-straight neighbours (how far a deeper cut must extend);
    only computed with want_reach (it is slow, and the scan does not need it)."""
    faces = faces if faces is not None else shape.Faces
    f = faces[i]
    p0 = f.CenterOfMass
    straight, land, reach = True, True, 0.0
    for _, j in nb[i]:
        g = faces[j]
        gs = g.Surface
        kind = type(gs).__name__
        if kind == "Plane":
            ok = abs(App.Vector(gs.Axis).normalize().dot(n)) < 1e-6
        elif kind == "Cylinder":
            ok = App.Vector(gs.Axis).cross(n).Length < 1e-6
        elif kind in ("Cone", "Sphere", "Toroid"):
            ok = False
        else:
            ok = sharpen.straight_face(g, n)       # splines extruded along n (gear flanks ...)
        if ok:
            continue
        straight = False
        pts = [v.Point for v in g.Vertexes] + [g.CenterOfMass]
        if any((q - p0).dot(n) > 1e-6 for q in pts):
            land = False
            break
        for q in (pts if want_reach else ()):
            flat = q - n * (q - p0).dot(n)
            try:
                reach = max(reach, f.distToShape(Part.Vertex(flat))[0])
            except Exception:
                pass
    if straight:
        return "straight", 0.0
    return ("land", reach) if land else (None, 0.0)


def _pt(v):
    return f"({v.x:.4g}, {v.y:.4g}, {v.z:.4g})"


def add_face_moves(sess, report, limit=12, only=None):
    """Make the position of flat faces a parameter (plate thickness, boss height, height of a machined land ...).

    Each movable face gets one feature for moving out and one for moving in, both switched off until its parameter
    asks for a move; they sit at the end of the tree so nothing depends on them.
      out: a Pad taken from the face itself (it follows upstream changes such as hole sizes);
      in : for a face with straight surroundings a Pocket taken from the face;
           for a land (bounded by chamfers, fillets or drafted walls that fall away) a Pocket whose outline reaches
           past the face by the width of those neighbours, so that milling the land lower also takes their upper
           part away instead of leaving a rim.
    Every pair is tried out and in when created and dropped if the volume does not change as expected.
    The largest `limit` faces are offered, or exactly the faces in `only` (faces of the reference, found again in
    the rebuilt part by position); a face of `only` that cannot be made movable is reported in report["skipped"]."""
    body = sess.body
    anchor = body.Tip
    if anchor is None:
        return
    shape = anchor.Shape
    nb = _neighbours(shape)

    def plane_key(f):
        a = _canon(App.Vector(f.Surface.Axis).normalize())
        return (round(a.x, 4), round(a.y, 4), round(a.z, 4), round(a.dot(f.Surface.Position), 3))

    # planes that exist before the fillets and chamfers: a chamfer's own faces are not faces to move
    plain = [o for o in body.Group if o.isDerivedFrom("PartDesign::Feature")
             and o.TypeId not in ("PartDesign::Fillet", "PartDesign::Chamfer")]
    real = {plane_key(f) for f in plain[-1].Shape.Faces if type(f.Surface).__name__ == "Plane"} if plain else None
    pool = range(len(shape.Faces))
    if only is not None:
        pool = []
        for g in only:
            hit = next((i for i, f in enumerate(shape.Faces) if type(f.Surface).__name__ == "Plane"
                        and abs(f.Area - g.Area) <= 1e-5 * max(g.Area, 1.0)
                        and f.CenterOfMass.distanceToPoint(g.CenterOfMass) < 1e-4), None)
            if hit is None:
                report["skipped"].append(f"flat face at {_pt(g.CenterOfMass)}: not found in the rebuilt part")
            else:
                pool.append(hit)
    cands = []
    for i in pool:
        f = shape.Faces[i]
        if type(f.Surface).__name__ != "Plane" or not nb[i]:
            continue
        if only is None and real is not None and plane_key(f) not in real:
            continue
        u0, u1, v0, v1 = f.ParameterRange
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        kind, reach = _surroundings(shape, nb, i, n)
        if kind:
            cands.append((f.Area, i, n, kind, reach))
        elif only is not None:
            report["skipped"].append(f"flat face at {_pt(f.CenterOfMass)}: cannot be moved (a curved face rises "
                                     "above its edge)")
    cands.sort(key=lambda c: -c[0])
    made = 0
    chosen = cands[:limit] if only is None else cands
    for ci, (area, i, n, kind, reach) in enumerate(chosen):
        _tell("faces", ci / len(chosen), i=ci + 1, n=len(chosen))
        name = f"Face{i + 1}"
        face = shape.Faces[i]
        thick = _thickness(shape, face, n)
        test = min(1.0, 0.25 * thick) if thick > 0 else 1.0
        before = body.Tip.Shape.Volume
        tip_before = body.Tip
        made_objs = []
        good = False
        rim_only = False
        try:
            pad = body.newObject("PartDesign::Pad", "FaceMove")
            made_objs.append(pad.Name)
            pad.Profile = (anchor, [name])
            pad.Length = test
            sess.doc.recompute()
            grew = pad.isValid() and abs((pad.Shape.Volume - before) - area * test) <= 2e-3 * area * test
            pad.Suppressed = True
            sess.doc.recompute()
            if kind == "land":
                try:        # the outline of the land as sketch geometry: lines and arcs only
                    _edges_to_geometry(face.OuterWire.Edges, lambda q: [q.x, q.y])
                except ValueError:
                    rim_only = True     # curved (spline) outline, e.g. gear flanks: no widened pocket can be sketched
            if kind == "straight" or rim_only:
                cut = body.newObject("PartDesign::Pocket", "FaceMoveIn")
                made_objs.append(cut.Name)
                cut.Profile = (anchor, [name])
                cut.Length = test
                lo, hi = 0.998, 1.002
            else:
                # outline of the land, widened by the reach of its neighbours (at most half the land's width)
                bb = face.BoundBox
                margin = min(reach * 1.05 + 1e-3, 0.5 * min(d for d in (bb.XLength, bb.YLength, bb.ZLength) if d > 1e-6))
                outer = face.OuterWire
                try:
                    wide = outer.makeOffset2D(margin)
                    edges = wide.Edges
                except Exception:
                    edges = outer.Edges
                c0 = face.CenterOfMass
                plane = {"origin": [c0.x, c0.y, c0.z], "normal": [n.x, n.y, n.z]}
                o, x, y, _ = sess._frame(plane)
                plane["x_dir"] = [x.x, x.y, x.z]
                geo = _edges_to_geometry(edges, lambda q: [(q - o).dot(x), (q - o).dot(y)])
                sk = sess._make_sketch(plane, 0, geo)
                made_objs.append(sk.Name)
                cut = body.newObject("PartDesign::Pocket", "FaceMoveIn")
                made_objs.append(cut.Name)
                cut.Profile = sk
                cut.Length = test
                lo, hi = 0.8, 3.0       # the neighbours' upper part goes too
            sess.doc.recompute()
            if cut.isValid() and abs(cut.Shape.Volume - before) < 1e-9 * before:
                cut.Reversed = not cut.Reversed
                sess.doc.recompute()
            took = (before - cut.Shape.Volume) / (area * test) if cut.isValid() else 0.0
            cut.Suppressed = True
            sess.doc.recompute()
            good = grew and lo <= took <= hi and abs(body.Tip.Shape.Volume - before) <= 1e-9 * max(before, 1.0)
        except Exception:
            good = False
        rim_plane = None
        if good and kind == "land":      # the rim (chamfers, fillets) may be moved along: stretch the part under the face
            try:
                rim_plane = _rim_plan(shape, nb, i, n, thick)
                if rim_plane is not None:
                    v0 = shape.Volume
                    for d in (test, -test):
                        moved, sinfo = stretch.stretch(shape, n, rim_plane, d)
                        want = sinfo["area"] * d
                        if moved is None or abs((moved.Volume - v0) - want) > 2e-3 * abs(want):
                            rim_plane = None
                            break
            except Exception:
                rim_plane = None
        if rim_only and rim_plane is None:
            good = False        # a plain pocket of the face outline would leave the rim standing: not worth offering
        if not good:
            if only is not None:
                report["skipped"].append(f"flat face at {_pt(face.CenterOfMass)}: moving it does not give a clean "
                                         "result")
            for nm in reversed(made_objs):
                try:
                    sess.doc.removeObject(nm)
                except Exception:
                    pass
            body.Tip = tip_before
            sess.doc.recompute()
            continue
        made += 1
        pad.Label = f"Face_{made}"
        cut.Label = f"Face_{made}_in"
        pad.addProperty("App::PropertyString", "SR_Pair", "steprecon", "pocket that moves this face inward")
        pad.addProperty("App::PropertyFloat", "SR_Thickness", "steprecon", "material behind the face at the start")
        pad.addProperty("App::PropertyString", "SR_Kind", "steprecon", "straight surroundings, or a land")
        pad.SR_Pair, pad.SR_Thickness, pad.SR_Kind = cut.Name, float(thick), kind
        if rim_plane is not None:
            pad.addProperty("App::PropertyBool", "SR_Rim", "steprecon", "the rim (chamfers, fillets) moves with the face")
            pad.addProperty("App::PropertyFloat", "SR_Offset", "steprecon", "how far the face is moved (with the rim)")
            pad.addProperty("App::PropertyFloat", "SR_RimPlane", "steprecon", "cutting plane position along the normal")
            pad.addProperty("App::PropertyVector", "SR_RimNormal", "steprecon", "outward normal of the face")
            pad.SR_Rim, pad.SR_Offset, pad.SR_RimPlane = True, 0.0, float(rim_plane)
            pad.SR_RimNormal = App.Vector(n.x, n.y, n.z)
        sess._record(list(reversed(made_objs)), f"movable face {made} ({kind}, thickness {thick:g})")
    report["faces_made"] = made
    report["features"] += made


def _attempt(ref, blend_faces, exclude_holes, only_holes=None):
    """One build: take `blend_faces` off, holes out, everything back on. Returns (session, report, identical)."""
    report = {"holes_made": 0, "fillets_made": 0, "chamfers_made": 0, "faces_made": 0, "features": 0, "skipped": [],
              "_refused": set()}
    target = ref
    if blend_faces:
        _tell("defeature", 0.0)
        target = _defeature(ref, blend_faces)
        if target is None:
            return None, report, False
        report["auto_added"] = list(_defeature.extra)   # faces that had to go with the picked ones (rim of the ends)
    sess = fc_tools.Session(ref)
    failed = _cut_holes(sess, target, report, exclude_holes, only_holes)
    if failed:
        App.closeDocument(sess.doc.Name)
        return _attempt(ref, blend_faces, exclude_holes | failed, only_holes)
    c = None
    if blend_faces:
        # strips along curved faces (gear flanks): the kernel's chamfer does not take them, and trying costs long and
        # can leave odd small chamfers at their ends - cut them by tool first, exactly
        plans = sharpen.strip_plan(ref, blend_faces)
        if plans and len(plans) == len(set(blend_faces)) and not all(d["straight"] for d in plans):
            _tell("dress", 0.6)
            c = _strips_by_tool(sess, ref, blend_faces, report, plans)
        if c is None:
            _dress(sess, report)
    c = c or sess.compare()
    if blend_faces and not (c.get("converged") or c.get("near")):
        c = _strips_by_tool(sess, ref, blend_faces, report) or c
    report["_excluded_holes"] = exclude_holes
    report["_cmp"] = {k: c.get(k) for k in ("converged", "near", "xor_pct")}     # so the caller need not compare again
    if c.get("near") and not c.get("converged"):
        report["skipped"].append(f"tiny details were not reproduced: the result differs from the file by "
                                 f"{c.get('xor_pct')}% of the volume ({c.get('ref_faces')} faces in the file, "
                                 f"{c.get('new_faces')} here)")
    return sess, report, bool(c.get("converged") or c.get("near"))


def _strips_by_tool(sess, ref, blend_faces, report, plans=None):
    """Chamfer strips the kernel's chamfer could not put back (curved flanks, ends running into other chamfers): keep
    them as tool cuts with one size per group (sharpen.apply_strips, params.with_strips). -> the comparison with the
    reference if the part now equals it, else None (and nothing is kept)."""
    import json
    from . import compare as cmp_mod
    try:
        cur = sess.result_shape()
        plans = [d for d in (plans if plans is not None else sharpen.strip_plan(ref, blend_faces))
                 if cur.isInside(App.Vector(*d["probe"]), 1e-7, False) == d["convex"]]   # still sharp there
        if not plans:
            return None
        groups = {}
        for d in plans:
            kind = d.get("kind", "chamfer")
            groups.setdefault((kind, round(d.get("size", d["leg"]), 3 if kind == "fillet" else 2)), []).append(d)
        made = []
        for (kind, size), ds in sorted(groups.items()):
            o = sess.doc.addObject("App::FeaturePython", "StripChamfer")
            o.addProperty("App::PropertyString", "SR_StripData", "steprecon", "the strips: sections along each one")
            o.addProperty("App::PropertyFloat", "SR_Size", "steprecon", "chamfer size")
            o.addProperty("App::PropertyFloat", "SR_Size0", "steprecon", "chamfer size in the file")
            o.addProperty("App::PropertyString", "SR_Body", "steprecon", "the body it is cut from")
            o.addProperty("App::PropertyString", "SR_Kind", "steprecon", "chamfer or fillet")
            o.SR_Kind = kind
            o.SR_StripData = json.dumps(ds, separators=(",", ":"))
            o.SR_Size = o.SR_Size0 = float(size)
            o.SR_Body = sess.body.Name
            o.Label = (f"Fillet_R{size:g}" if kind == "fillet" else f"Chamfer_{size:g}") + (f"_x{len(ds)}" if len(ds) > 1 else "")
            made.append(o)
        s = sess.result_shape()
        c = {}
        if s is not None:       # faces matched one to one first (a fraction of a second); the booleans only if needed
            c = cmp_mod.compare(ref, s, quick=True)
            if not c.get("converged"):
                c = cmp_mod.compare(ref, s)
        if c.get("converged") or c.get("near"):
            nf = sum(1 for d in plans if d.get("kind") == "fillet")
            report["fillets_made"] += nf
            report["chamfers_made"] += len(plans) - nf
            report["features"] += len(made)
            report["strip_tools"] = len(plans)
            report["_refused"] = set()      # what the kernel's chamfer refused is covered now
            return c
        report["_strip_try"] = {k: c.get(k) for k in ("xor_pct", "volume_diff_pct", "ref_faces", "new_faces")}
        for o in made:
            sess.doc.removeObject(o.Name)
    except Exception as e:
        report["_strip_try"] = f"{type(e).__name__}: {e}"
    return None


def build(ref, progress=None):
    """Reference -> (Session with blank + features, report dict). The result is always identical to the reference:
    dress-ups that do not come back exactly are given up (first as a whole, then group by group).
    progress: callable(fraction 0..1, stage, info) told how far the work is (describe() makes a line of it)."""
    global _progress, _span, _last, _try
    _progress, _span, _last, _try = progress, (0.0, 0.05), 0.0, {}
    try:
        return _build(ref)
    finally:
        _progress, _try = None, {}


def _build(ref, blends=None, only_holes=None, plane_faces=None):
    """blends: {group: face ids} to treat (None: find all); only_holes: line keys (None: all);
    plane_faces: ref face indices to make movable (None: pick the largest automatically)."""
    global _span, _try
    if blends is None:
        _tell("blends", 0.0)
        blends = find_blends(ref)
    everything = sorted(i for ids in blends.values() for i in ids)
    plans = [everything] if everything else []
    if len(blends) > 1:
        plans += [sorted(ids) for _, ids in sorted(blends.items(), key=lambda kv: str(kv[0]))]
    best = None
    good_groups = []
    for k, faces in enumerate(plans):
        _span = (0.05 + 0.75 * k / len(plans), 0.05 + 0.75 * (k + 1) / len(plans))
        _try = {"attempt": k + 1, "attempts": len(plans)}
        sess, report, same = _attempt(ref, faces, set(), only_holes)
        if sess is None:
            continue
        if same and faces == everything:
            best = (sess, report)
            break
        if same:
            good_groups.append(faces)
        App.closeDocument(sess.doc.Name)
    if best is None and good_groups:        # combine the groups that worked on their own, else take the biggest
        for faces in ([sorted(i for g in good_groups for i in g)] if len(good_groups) > 1 else []) \
                + [max(good_groups, key=len)]:
            _span, _try = (_last, _last + (0.8 - _last) / 2), {}
            sess, report, same = _attempt(ref, faces, set(), only_holes)
            if sess is not None and same:
                best = (sess, report)
                break
            if sess is not None:
                App.closeDocument(sess.doc.Name)
    if best is None:                        # holes only: identical by construction
        _span, _try = (_last, 0.8), {}
        sess, report, _ = _attempt(ref, [], set(), only_holes)
        best = (sess, report)
    sess, report = best
    _span, _try = (0.8, 1.0), {}
    try:
        if plane_faces is None:
            add_face_moves(sess, report)
        elif plane_faces:
            add_face_moves(sess, report, only=[ref.Faces[i] for i in plane_faces])
    except Exception as e:      # an extra: the part itself is complete without it
        report["skipped"].append(f"movable faces: {type(e).__name__}: {e}")
    for key in sorted(report.pop("_excluded_holes", set())):
        report["skipped"].append(f"hole D{2 * key[6]:g}: could not be cut again cleanly")
    refused = report.pop("_refused", set())
    found = {("fillet R%g" % k[1]) if k[0] == "fillet" else "chamfer": len(v) for k, v in blends.items()}
    report["blend_faces_found"] = found
    if found and not (report["fillets_made"] or report["chamfers_made"]):
        report["skipped"].append("fillets/chamfers: could not be reproduced exactly, left in the blank")
    elif refused:
        report["skipped"].append("some fillets/chamfers left in the blank: " + ", ".join(sorted(refused)))
    return sess, report


# ------------------------------------------------------------------ pick what to treat
def _chamfer_size(shape, i, nb, faces=None):
    """Leg lengths (larger, smaller) of a chamfer face. In the triangle corner - leg 1 - leg 2 the chamfer is the side
    of width w, its angles at the two legs are the angles a1, a2 between the chamfer and the neighbours, so
    leg on neighbour 1 = w sin(a2) / sin(a1 + a2) and the other way round."""
    faces = faces if faces is not None else shape.Faces
    f = faces[i]
    long_edges = sorted(f.Edges, key=lambda e: -e.Length)[:2]
    angles = []
    for x in long_edges:
        for e, j in nb[i]:
            if e.isSame(x):
                p = _edge_mid(e)
                n1, n2 = _normal_at(f, p), _normal_at(faces[j], p)
                if n1 is not None and n2 is not None:
                    angles.append(n1.getAngle(n2))
                break
    width = f.Area / max(1e-12, sum(e.Length for e in long_edges) / max(1, len(long_edges)))
    if len(angles) != 2 or math.sin(sum(angles)) < 1e-6:
        return width / math.sqrt(2), width / math.sqrt(2)
    a1, a2 = angles
    legs = (width * math.sin(a2) / math.sin(a1 + a2), width * math.sin(a1) / math.sin(a1 + a2))
    return max(legs), min(legs)


def _side(n):
    k = max(range(3), key=lambda i: abs(n[i]))
    return (("+" if n[k] > 0 else "-") + "XYZ"[k]) if abs(n[k]) > 0.999 else ""


def _odd_rim(shape, nb, hole_faces, known, faces=None):
    """Faces touching a hole that are neither flat, nor another cylinder, nor a recognised fillet / chamfer: a rim
    shape the hole's diameter cannot carry along (it stays as in the file)."""
    faces = faces if faces is not None else shape.Faces
    return {j for i in hole_faces for _, j in nb[i]
            if j not in known and j not in hole_faces and type(faces[j].Surface).__name__ not in ("Plane", "Cylinder")}


def _curved_rounds(ref, nb, faces):
    """{face: radius} of rounds along curved edges, which find_blends (analytic surfaces only) does not see."""
    try:
        adj = [{j for _, j in nb[i]} for i in range(len(faces))]
        return sharpen.curved_fillets(ref, faces, adj)
    except Exception:
        return {}


def scan(ref):
    """What could become a parameter, without building anything (well under a second for most parts).

    items : one per hole, per fillet / chamfer face and per movable flat face
            {"id", "kind": hole|fillet|chamfer|face, "faces": [ref face indices], "size", "group", ...}
            holes also carry "key" (for build_selected), "depth", "at"; faces "side", "area", "at".
    groups: same kind and size together {"id", "kind", "size", "items": [ids]}; flat faces are not grouped.
    owner : ref face index -> item number (1-based position in items), 0 = not a candidate."""
    nb = _neighbours(ref)
    faces = ref.Faces           # one list: every ref.Faces[i] builds the whole list again (tens of ms on big parts)
    items, groups = [], {}

    def add(item, gkey=None):
        items.append(item)
        if gkey is not None:
            g = groups.setdefault(gkey, {"id": "g%d" % (len(groups) + 1), "kind": item["kind"], "size": item["size"],
                                         "items": []})
            if item.get("size2"):
                g["size2"] = item["size2"]
            g["items"].append(item["id"])
            item["group"] = g["id"]

    for n, h in enumerate(find_holes(ref), 1):
        d = round(2 * h["radius"], 4)
        at = h["foot"] + h["axis"] * h["t1"]
        add({"id": f"h{n}", "kind": "hole", "faces": list(h["faces"]), "size": d, "key": _line_key(h),
             "depth": round(h["t1"] - h["t0"], 4), "at": [round(at.x, 3), round(at.y, 3), round(at.z, 3)]},
            ("hole", d))
    blend_faces = set()
    for key, ids in sorted(find_blends(ref).items(), key=lambda kv: str(kv[0])):
        for i in ids:
            blend_faces.add(i)
            item = {"id": f"b{i}", "kind": key[0], "faces": [i]}
            if key[0] == "fillet":
                item["size"] = round(key[1], 4)
            else:
                big, small = _chamfer_size(ref, i, nb, faces)
                item["size"] = round(big, 3)
                if big - small > max(1e-3, 0.02 * big):       # two different distances
                    item["size2"] = round(small, 3)
                else:
                    item["size"] = round((big + small) / 2, 3)
            add(item, (key[0], item["size"], item.get("size2")))
    for i, r in sorted(_curved_rounds(ref, nb, faces).items()):     # rounds along curved edges (spline flanks)
        if i not in blend_faces:
            blend_faces.add(i)
            add({"id": f"b{i}", "kind": "fillet", "faces": [i], "size": round(r, 4)}, ("fillet", round(r, 4), None))
    for i, f in enumerate(faces):
        if i in blend_faces or type(f.Surface).__name__ != "Plane" or not nb[i]:
            continue
        u0, u1, v0, v1 = f.ParameterRange
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        try:
            kind, _ = _surroundings(ref, nb, i, n, faces, want_reach=False)
        except Exception:
            kind = None
        if kind:
            c = f.CenterOfMass
            add({"id": f"p{i}", "kind": "face", "faces": [i], "size": round(f.Area, 3), "side": _side(n),
                 "surround": kind, "at": [round(c.x, 3), round(c.y, 3), round(c.z, 3)]})
    for it in items:
        if it["kind"] == "hole":
            odd = _odd_rim(ref, nb, it["faces"], blend_faces, faces)
            if odd:
                it["rim_unknown"] = len(odd)
                g = next(g for g in groups.values() if it["id"] in g["items"])
                g["rim_unknown"] = g.get("rim_unknown", 0) + len(odd)
    taken = {i for it in items for i in it["faces"]}
    for i, f in enumerate(faces):       # every other face: cannot become a dimension, but can be removed
        if i not in taken:
            add({"id": f"f{i}", "kind": "other", "faces": [i], "size": round(f.Area, 3)})
    owner = [0] * len(ref.Faces)
    for k, it in enumerate(items, 1):
        for i in it["faces"]:
            owner[i] = k
    order = {"hole": 0, "fillet": 1, "chamfer": 2}
    gl = sorted(groups.values(), key=lambda g: (order[g["kind"]], -g["size"] if g["kind"] == "hole" else g["size"]))
    return {"items": items, "groups": gl, "owner": owner}


def _take_off(shape, faces):
    """shape without these Face objects, or None if the kernel cannot close the gap cleanly."""
    try:
        out = shape.defeaturing(faces)
    except Exception:
        return None
    if out.isNull() or not out.isValid() or len(out.Solids) != len(shape.Solids) or len(out.Faces) >= len(shape.Faces) \
            or not _sane(shape, out):
        return None
    return out.Solids[0] if len(out.Solids) == 1 else out


def remove_faces(shape, face_ids, progress=None):
    """Remove faces from the part - fillets, chamfers, holes, small details - letting the neighbours close the gap.
    Returns (new shape, number of faces removed, number that could not be removed); see remove_faces_ex."""
    res = remove_faces_ex(shape, face_ids, progress)
    return res["shape"], res["done"], res["failed"]


def remove_faces_ex(shape, face_ids, progress=None):
    """Remove faces from the part - fillets, chamfers, holes, small details. Three routes, the first that works:
    the outline route (sharpen.py: parts that are an extrusion with features on the ends - exact, fast; it also takes
    the whole rim of an end along, `added` faces more than were picked), the kernel's defeaturing for everything at
    once, then connected patches and single faces in repeated passes. Every answer of the kernel is checked (_sane).
    -> {"shape", "done", "failed", "added", "method"}"""
    ids = sorted(set(int(i) for i in face_ids))
    if not ids:
        return {"shape": shape, "done": 0, "failed": 0, "added": 0, "method": ""}
    faces = shape.Faces
    try:
        new, info = sharpen.rebuild_prism(shape, ids, faces)
    except Exception:
        new, info = None, ""
    if new is not None:
        gone = set(info["removed"])
        added = len(gone - set(ids))
        left = [i for i in ids if i not in gone]
        if not left:
            return {"shape": new, "done": len(gone & set(ids)), "failed": 0, "added": added, "method": "outline"}
        # faces the outline route does not handle: the kernel way, on the new part (found again by position)
        marks = [(faces[i].CenterOfMass, faces[i].Area) for i in left]
        rest = []
        for c, a in marks:
            hit = next((j for j, f in enumerate(new.Faces) if abs(f.Area - a) <= 1e-6 * max(a, 1.0)
                        and f.CenterOfMass.distanceToPoint(c) < 1e-5), None)
            if hit is not None:
                rest.append(hit)
        out, done, failed = _remove_generic(new, rest, progress)
        return {"shape": out, "done": len(gone & set(ids)) + done, "failed": failed + len(left) - len(rest),
                "added": added, "method": "outline+kernel"}
    try:
        new, gone = sharpen.fill_wedges(shape, ids, faces)
    except Exception:
        new, gone = None, []
    if new is not None:
        left = [i for i in ids if i not in set(gone)]
        if not left:
            return {"shape": new, "done": len(gone), "failed": 0, "added": 0, "method": "wedges"}
        rest = _find_again(faces, left, new)
        out, done, failed = _remove_generic(new, rest, progress)
        return {"shape": out, "done": len(gone) + done, "failed": failed + len(left) - len(rest), "added": 0,
                "method": "wedges+kernel"}
    out, done, failed = _remove_generic(shape, ids, progress)
    return {"shape": out, "done": done, "failed": failed, "added": 0, "method": "kernel"}


def _find_again(faces, ids, new):
    """Numbers in `new` of the faces `ids` of the old face list (same position and area); missing ones are left out."""
    out = []
    cand = [(f.CenterOfMass, f.Area) for f in new.Faces]
    for i in ids:
        c, a = faces[i].CenterOfMass, faces[i].Area
        hit = next((j for j, (c2, a2) in enumerate(cand) if abs(a2 - a) <= 1e-6 * max(a, 1.0)
                    and c2.distanceToPoint(c) < 1e-5), None)
        if hit is not None:
            out.append(hit)
    return out


KERNEL_BUDGET = 240.0       # seconds the kernel fallback may try patch by patch before it stops


def _remove_generic(shape, ids, progress=None):
    """The kernel route of remove_faces_ex: (new shape, removed, not removed). Stops after KERNEL_BUDGET seconds:
    on a part with thousands of faces every attempt costs seconds to minutes, and hundreds of them would take hours."""
    import time as _time
    deadline = _time.time() + KERNEL_BUDGET
    ids = sorted(set(ids))
    if not ids:
        return shape, 0, 0
    out = _take_off(shape, [shape.Faces[i] for i in ids])
    if out is not None:
        return out, len(ids), 0
    nb = _neighbours(shape)
    left, patches = set(ids), []
    while left:                     # connected patches of the chosen faces
        todo, patch = [left.pop()], []
        while todo:
            i = todo.pop()
            patch.append(i)
            for _, j in nb[i]:
                if j in left:
                    left.discard(j)
                    todo.append(j)
        patches.append(patch)
    mark = {i: (shape.Faces[i].CenterOfMass, shape.Faces[i].Area) for i in ids}
    cur, gone = shape, set()

    def find(i):                    # the same face in the shape as it is now (numbers change with every removal)
        c, a = mark[i]
        return next((f for f in cur.Faces if abs(f.Area - a) <= 1e-6 * max(a, 1.0)
                     and f.CenterOfMass.distanceToPoint(c) < 1e-5), None)

    def attempt(group):
        nonlocal cur
        faces = [find(i) for i in group]
        new = _take_off(cur, faces) if all(f is not None for f in faces) else None
        if new is not None:
            cur = new
            gone.update(group)
        return new is not None

    for k, patch in enumerate(patches):
        if _time.time() > deadline:
            break
        if progress:
            progress(0.5 * k / len(patches), k + 1, len(patches))
        if len(patch) < len(ids):
            attempt(patch)
    for rnd in range(4):            # single faces, again while a pass still gets somewhere
        rest = [i for i in ids if i not in gone]
        before = len(gone)
        for k, i in enumerate(rest):
            if _time.time() > deadline:
                break
            if progress:
                progress(0.5 + 0.125 * (rnd + k / len(rest)), k + 1, len(rest))
            attempt([i])
        if _time.time() > deadline:
            break
        if len(gone) == before or len(gone) == len(ids):
            break
    return cur, len(gone), len(ids) - len(gone)


def build_selected(ref, holes=(), blend_faces=(), planes=(), progress=None):
    """Like build(), but only for what was picked - the rest stays in the blank, untouched, and costs nothing.

    holes: line keys of holes (scan() items' "key"); blend_faces: ref face indices of fillets / chamfers;
    planes: ref face indices of flat faces to make movable. Fillet / chamfer faces on the rim of a picked hole are
    taken along (otherwise the rim would keep its old size when the diameter changes): report["auto_added"]."""
    global _progress, _span, _last, _try
    _progress, _span, _last, _try = progress, (0.0, 0.05), 0.0, {}
    try:
        _tell("blends", 0.0)
        found = find_blends(ref)
        chosen = set(blend_faces)
        added = []
        holes = {tuple(k) for k in holes}
        warnings = []
        if holes:
            nb = _neighbours(ref)
            known = {i for ids in found.values() for i in ids}
            for h in find_holes(ref):
                if _line_key(h) in holes:
                    odd = _odd_rim(ref, nb, h["faces"], known)
                    if odd:
                        warnings.append({"hole": round(2 * h["radius"], 4), "faces": len(odd),
                                         "text": f"hole D{2 * h['radius']:g}: {len(odd)} face(s) at its rim are not "
                                                 "recognised and stay as in the file - they do not follow its diameter"})
                    for fi in h["faces"]:
                        for _, j in nb[fi]:
                            if j in known and j not in chosen:
                                chosen.add(j)
                                added.append(j)
        known_all = {i for ids in found.values() for i in ids}
        if chosen - known_all:
            for i, r in _curved_rounds(ref, _neighbours(ref), ref.Faces).items():
                if i in chosen and i not in known_all:
                    found.setdefault(("fillet", round(r, 4)), []).append(i)
        blends = {k: sorted(i for i in ids if i in chosen) for k, ids in found.items()}
        blends = {k: v for k, v in blends.items() if v}
        sess, report = _build(ref, blends, holes, sorted(set(planes)))
        report["auto_added"] = sorted(set(added) | set(report.get("auto_added", [])))
        refused = [s for s in report["skipped"] if "fillets/chamfers" in s]
        if added and refused:
            warnings.append({"hole": None, "faces": 0, "text": "some fillets/chamfers at the rim of a chosen hole could "
                                                               "not be rebuilt exactly and stay as in the file"})
        report["warnings"] = warnings
        return sess, report
    finally:
        _progress, _try = None, {}
