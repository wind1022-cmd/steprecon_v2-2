"""Take chamfers and fillets off a part exactly and fast - for parts that are an extrusion with features on its ends.

OpenCascade's defeaturing needs the neighbours of every removed face to be extended until they meet again. On a part
like a gear (hundreds of small rounds and chamfers around teeth and splines) that takes minutes, and it can silently
return a broken solid. But such a part is simple in a different way: seen along its axis it is one outline, and the
chamfers sit on the two ends. So:

    1. cut the part half way along the axis: the outline (loops of lines, arcs, splines);
    2. in that outline replace every run of removed edges (rounds, chamfers that run along the axis) by the point
       where the two neighbouring edges meet - 2D, exact, no surface extension;
    3. extrude the new outline over the full length: the end chamfers are gone too.

    rebuild_prism(shape, face_ids) -> (new shape or None, info)

It needs: an axis n such that every face is straight along n (walls), an end face (flat, square to n, at the very
ends) or part of the rim of an end (chamfers / rounds that lead onto the end face). The rim is removed completely
whenever any face of the outline or the rim is picked: a chamfer on a rounded corner cannot stay when the round goes.
Anything else (steps in the middle, pockets, tapered walls) is left to the generic route and None is returned."""
import math

import FreeCAD as App
import Part

GAP = 1e-6


def straight_face(face, n, tol=1e-3):
    """True if the face runs along n: its normal is square to n everywhere (walls, cylinders, extrusions along n)."""
    s = face.Surface
    k = type(s).__name__
    if k == "Plane":
        return abs(App.Vector(s.Axis).dot(n)) < tol
    if k == "Cylinder":
        return App.Vector(s.Axis).cross(n).Length < tol
    if k in ("Cone", "Sphere", "Toroid"):
        return False
    u0, u1, v0, v1 = face.ParameterRange
    for a in (0.15, 0.5, 0.85):
        for b in (0.15, 0.5, 0.85):
            try:
                if abs(face.normalAt(u0 + (u1 - u0) * a, v0 + (v1 - v0) * b).dot(n)) > tol:
                    return False
            except Exception:
                return False
    return True


def _axes(faces):
    """Directions worth trying: the normals of the biggest flat faces and the coordinate axes."""
    flat = []
    for f in faces:
        if type(f.Surface).__name__ == "Plane":
            flat.append((f.Area, f))
    flat.sort(key=lambda x: -x[0])
    out = []
    for _, f in flat[:8]:
        u0, u1, v0, v1 = f.ParameterRange
        out.append(f.normalAt((u0 + u1) / 2, (v0 + v1) / 2))
    out += [App.Vector(0, 0, 1), App.Vector(0, 1, 0), App.Vector(1, 0, 0)]
    uniq = []
    for v in out:
        v = App.Vector(v)
        v.normalize()
        if not any(abs(abs(v.dot(u)) - 1.0) < 1e-6 for u in uniq):
            uniq.append(v)
    return uniq


def _classify(faces, n):
    """-> (walls, top, bottom, rim, tmax, tmin) for axis n; walls/rim are lists of face numbers, top/bottom the flat end
    faces. None if some flat face square to n lies between the ends."""
    walls, flat, other = [], [], []
    for i, f in enumerate(faces):
        s = f.Surface
        if type(s).__name__ == "Plane" and abs(abs(App.Vector(s.Axis).dot(n)) - 1.0) < 1e-6:
            flat.append(i)
        elif straight_face(f, n):
            walls.append(i)
        else:
            other.append(i)
    if not flat:
        return None
    pos = {}
    for i in flat:
        f = faces[i]
        u0, u1, v0, v1 = f.ParameterRange
        sign = 1 if f.normalAt((u0 + u1) / 2, (v0 + v1) / 2).dot(n) > 0 else -1
        pos[i] = (sign, n.dot(f.CenterOfMass))
    tmax = max(t for sgn, t in pos.values() if sgn > 0) if any(sgn > 0 for sgn, _ in pos.values()) else None
    tmin = min(t for sgn, t in pos.values() if sgn < 0) if any(sgn < 0 for sgn, _ in pos.values()) else None
    if tmax is None or tmin is None or tmax - tmin < 1e-6:
        return None
    top, bottom = [], []
    for i, (sgn, t) in pos.items():
        if sgn > 0 and abs(t - tmax) < 1e-5 * (1 + abs(tmax)):
            top.append(i)
        elif sgn < 0 and abs(t - tmin) < 1e-5 * (1 + abs(tmin)):
            bottom.append(i)
        else:
            return None             # a flat face in the middle (step, pocket floor): not a plain prism
    return walls, top, bottom, other, tmax, tmin


def _adjacent(faces):
    """face number -> set of the numbers of the faces that share an edge with it."""
    by_edge = {}
    for i, f in enumerate(faces):
        for e in f.Edges:
            by_edge.setdefault(e.hashCode(), []).append((e, i))
    adj = {i: set() for i in range(len(faces))}
    for items in by_edge.values():
        for a in range(len(items)):
            for b in range(len(items)):
                if a != b and items[a][1] != items[b][1] and items[a][0].isSame(items[b][0]):
                    adj[items[a][1]].add(items[b][1])
    return adj


def _rim_is_connected(adj, top, bottom, rim_set):
    """Every rim face must lead to an end face through rim faces only: a boss or a groove somewhere in the middle of
    the part is not the rim of an end."""
    reach, todo = set(), list(top) + list(bottom)
    while todo:
        i = todo.pop()
        for j in adj[i]:
            if j in rim_set and j not in reach:
                reach.add(j)
                todo.append(j)
    return reach == rim_set


def _key(p):
    return (round(p.x * 1e5), round(p.y * 1e5), round(p.z * 1e5))


def _mid(e):
    return e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)


def _param_near(curve, p, ref):
    """Parameter of point p on curve, on the right turn of a periodic curve (the one closest to ref)."""
    u = curve.parameter(p)
    per = curve.period() if curve.isPeriodic() else 0.0
    if per:
        u += round((ref - u) / per) * per
    return u


def _sharpen_loop(wire, selected, n):
    """The loop with every run of selected edges replaced by the meeting point of its neighbours.
    Returns ([edges] closed loop, number of runs) / None if impossible / "gone" if all edges were selected."""
    items = [{"e": e, "sel": selected(e), "rev": e.Orientation == "Reversed"} for e in wire.OrderedEdges]
    if all(it["sel"] for it in items):
        return "gone"
    if not any(it["sel"] for it in items):
        return list(wire.Edges), 0
    k = next(i for i, it in enumerate(items) if not it["sel"])
    items = items[k:] + items[:k]               # starts with an edge that stays
    keep = []
    for it in items:
        if not it["sel"]:
            e = it["e"]
            keep.append({"curve": e.Curve, "u0": e.FirstParameter, "u1": e.LastParameter, "rev": it["rev"], "run": []})
            continue
        keep[-1]["run"].append(it)              # selected edges that follow this kept edge
    runs = 0
    for idx, a in enumerate(keep):
        if not a["run"]:
            continue
        b = keep[(idx + 1) % len(keep)]
        run = a["run"]
        first, last = run[0]["e"], run[-1]["e"]
        p_in = first.valueAt(first.LastParameter if run[0]["rev"] else first.FirstParameter)
        p_out = last.valueAt(last.FirstParameter if run[-1]["rev"] else last.LastParameter)
        mid = (p_in + p_out) * 0.5
        span = max((p_out - p_in).Length, sum(it["e"].Length for it in run))
        try:
            pts = a["curve"].intersectCC(b["curve"])
        except Exception:
            pts = []
        pts = [App.Vector(q.X, q.Y, q.Z) if hasattr(q, "X") else App.Vector(q) for q in pts if hasattr(q, "X") or isinstance(q, App.Vector)]
        if not pts:
            return None
        meet = min(pts, key=lambda q: (q - mid).Length)
        if (meet - mid).Length > 5.0 * span + 1e-6:
            return None
        # the kept edge before the run now ends in `meet`, the one after it starts there
        ua = _param_near(a["curve"], meet, a["u0"] if a["rev"] else a["u1"])
        ub = _param_near(b["curve"], meet, b["u1"] if b["rev"] else b["u0"])
        if a["rev"]:
            a["u0"] = ua
        else:
            a["u1"] = ua
        if b["rev"]:
            b["u1"] = ub
        else:
            b["u0"] = ub
        runs += 1
    edges = []
    for a in keep:
        u0, u1 = a["u0"], a["u1"]
        if u1 - u0 < 1e-9:
            return None
        try:
            edges.append(a["curve"].toShape(u0, u1))
        except Exception:
            return None
    return edges, runs


def rebuild_prism(shape, face_ids, faces=None, progress=None):
    """Remove the picked fillets / chamfers (face numbers of `shape`) by rebuilding the part as an extrusion.
    -> (new shape, info) or (None, reason). info: {"removed": [face numbers actually gone], "axis": n, "runs": k}"""
    faces = faces if faces is not None else shape.Faces
    picked = set(int(i) for i in face_ids)
    adj = None
    for n in _axes(faces):
        cls = _classify(faces, n)
        if cls is None:
            continue
        walls, top, bottom, rim, tmax, tmin = cls
        thick = tmax - tmin
        rim_set = set(rim)
        if rim_set:
            if adj is None:
                adj = _adjacent(faces)
            if not _rim_is_connected(adj, top, bottom, rim_set):
                continue
            depth = max(min(tmax - n.dot(v.Point), n.dot(v.Point) - tmin) for i in rim for v in faces[i].Vertexes)
            if depth > 0.45 * thick:
                continue
        walls_picked = [i for i in picked if i in set(walls)]
        if not walls_picked and not (picked & rim_set):
            continue                             # nothing of this axis' prism was asked for
        # --- the outline half way along the axis
        tm = (tmax + tmin) / 2.0
        wires = shape.slice(n, tm)
        if not wires:
            continue
        if progress:
            progress(0.15)
        sel_keys = set()
        for i in walls_picked:
            for w in faces[i].slice(n, tm):
                for e in w.Edges:
                    sel_keys.add(_key(_mid(e)))
        used = set()

        def selected(e):
            k = _key(_mid(e))
            if k in sel_keys:
                used.add(k)
                return True
            return False

        new_wires, runs, gone = [], 0, 0
        ok = True
        for w in wires:
            res = _sharpen_loop(w, selected, n)
            if res == "gone":
                gone += 1
                continue
            if res is None:
                ok = False
                break
            edges, r = res
            runs += r
            try:
                nw = Part.Wire(Part.__sortEdges__(edges)) if r else w
            except Exception:
                ok = False
                break
            if not nw.isClosed():
                ok = False
                break
            new_wires.append(nw)
        if not ok or not new_wires or len(used) < len(sel_keys):
            continue                             # some picked wall could not be found in the outline
        try:
            face = Part.makeFace(new_wires, "Part::FaceMakerBullseye")
            if face.isNull() or not face.isValid():
                continue
            area = sum(f.Area for f in face.Faces) if hasattr(face, "Faces") else face.Area
            moved = face.copy()
            moved.translate(n * (tmin - tm))
            solid = moved.extrude(n * thick)
        except Exception:
            continue
        if solid.isNull() or not solid.isValid() or not solid.Solids:
            continue
        solid = solid.Solids[0] if len(solid.Solids) == 1 else solid
        old = shape.Volume
        # sanity: a few percent change at most, and inside the old bounding box (plus the width of the features)
        if abs(solid.Volume - old) > 0.25 * old:
            continue
        bb, nb_ = shape.BoundBox, solid.BoundBox
        slack = 0.05 * bb.DiagonalLength
        if nb_.XMin < bb.XMin - slack or nb_.XMax > bb.XMax + slack or nb_.YMin < bb.YMin - slack or nb_.YMax > bb.YMax + slack \
                or nb_.ZMin < bb.ZMin - slack or nb_.ZMax > bb.ZMax + slack:
            continue
        removed = sorted(picked & set(walls)) + sorted(rim_set)
        return solid, {"removed": removed, "axis": [n.x, n.y, n.z], "runs": runs, "rim": sorted(rim_set), "holes": gone}
    return None, "not an extrusion with features on its ends"


# ------------------------------------------------------------------ flat chamfers: fill the wedge
def _plane_of(face):
    s = face.Surface
    if type(s).__name__ != "Plane":
        return None
    n = App.Vector(s.Axis)
    n.normalize()
    return n, n.dot(App.Vector(s.Position))


def _line_of(pa, pb):
    """Intersection line of two planes (normal, offset): (point, direction) or None if parallel."""
    (n1, d1), (n2, d2) = pa, pb
    u = n1.cross(n2)
    den = u.dot(u)
    if den < 1e-12:
        return None
    p = (n2.cross(u) * d1 + u.cross(n1) * d2) * (1.0 / den)
    u.normalize()
    return p, u


def _meet(line, plane):
    (p, u), (n, d) = line, plane
    den = n.dot(u)
    if abs(den) < 1e-9:
        return None
    return p + u * ((d - n.dot(p)) / den)


def _wedge(face, others):
    """The piece a flat chamfer strip cut off: bounded by the strip, the planes of the two faces along its long edges
    and the planes of the faces at its two ends. `others` = [(edge of the strip, neighbouring face)], one per edge.
    Returns a solid or None if the strip is not of that simple kind."""
    if len(others) != 4 or _plane_of(face) is None:
        return None
    others = sorted(others, key=lambda x: -x[0].Length)
    (ea, fa), (eb, fb), (ec, fc), (ed, fd) = others
    if ec.Length > 0.5 * eb.Length:
        return None                                 # not a strip
    pa, pb, pc, pd = _plane_of(fa), _plane_of(fb), _plane_of(fc), _plane_of(fd)
    if None in (pa, pb, pc, pd):
        return None
    line = _line_of(pa, pb)
    if line is None:
        return None
    p1, p2 = _meet(line, pc), _meet(line, pd)
    if p1 is None or p2 is None or (p1 - p2).Length < 1e-7:
        return None
    width = face.Area / max(ea.Length, 1e-12)
    for q, e in ((p1, ec), (p2, ed)):               # the corner the strip cut off lies close to the strip's end
        if e.distToShape(Part.Vertex(q))[0] > 6.0 * width + 1e-6:
            return None

    def ends(edge, cap):                            # (vertex at this cap, the other vertex) of a long edge
        v = [x.Point for x in edge.Vertexes]
        if len(v) != 2:
            return None
        d0, d1 = cap.distToShape(Part.Vertex(v[0]))[0], cap.distToShape(Part.Vertex(v[1]))[0]
        return (v[0], v[1]) if d0 < d1 else (v[1], v[0])

    a, b = ends(ea, ec), ends(eb, ec)
    if a is None or b is None:
        return None
    a0, a1 = a
    b0, b1 = b

    def poly(*pts):
        return Part.Face(Part.makePolygon(list(pts) + [pts[0]]))

    try:
        shell = Part.Shell([poly(a0, a1, b1, b0), poly(a0, a1, p2, p1), poly(b0, b1, p2, p1), poly(a0, b0, p1), poly(a1, b1, p2)])
        solid = Part.Solid(shell)
        if solid.Volume < 0:
            solid.reverse()
        if not solid.isValid() or solid.Volume < 1e-12:
            return None
        return solid
    except Exception:
        return None


def fill_wedges(shape, face_ids, faces=None):
    """Remove flat chamfer strips that lie between flat faces by putting back exactly the wedge each one cut off (or,
    in a hollow corner, taking the wedge it left away). Works where the outline route does not: teeth with a step,
    chamfers that stop somewhere along an edge. Strips that touch another picked face at an end are not handled here.
    -> (new shape, [face numbers removed]) or (None, [])"""
    faces = faces if faces is not None else shape.Faces
    picked = set(int(i) for i in face_ids)
    adj = _adjacent(faces)
    add, cut, done = [], [], []
    for i in sorted(picked):
        f = faces[i]
        if type(f.Surface).__name__ != "Plane" or len(f.Edges) != 4:
            continue
        others = []
        for e in f.Edges:
            hit = [j for j in adj[i] if any(e.isSame(x) for x in faces[j].Edges)]
            if len(hit) != 1:
                break
            others.append((e, faces[hit[0]], hit[0]))
        if len(others) != 4:
            continue
        short = sorted(others, key=lambda x: -x[0].Length)[2:]
        if any(j in picked for _, _, j in short):
            continue                                # its end is another face to be removed: not this simple case
        w = _wedge(f, [(e, g) for e, g, _ in others])
        if w is None:
            continue
        c = w.CenterOfMass
        (cut if shape.isInside(c, 1e-7, False) else add).append(w)
        done.append(i)
    if not done:
        return None, []
    try:
        out = shape
        if add:
            out = out.fuse(add, 1e-6)
        if cut:
            out = out.cut(cut, 1e-6)
        if not out.Solids or len(out.Solids) != len(shape.Solids):
            return None, []
        out = out.Solids[0] if len(out.Solids) == 1 else out
        want = shape.Volume + sum(w.Volume for w in add) - sum(w.Volume for w in cut)
        if not out.isValid() or abs(out.Volume - want) > 1e-6 * max(abs(want), 1.0) + 1e-4:
            return None, []
        refined = out.copy().removeSplitter()
        if refined.isValid() and abs(refined.Volume - out.Volume) <= 1e-6 * max(out.Volume, 1.0):
            out = refined
        return out, done
    except Exception:
        return None, []
