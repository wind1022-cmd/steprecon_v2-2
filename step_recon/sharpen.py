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


def _half_space(plane, inside, size):
    """A big block on the side of plane (normal, offset) that contains point `inside`."""
    n, d = plane
    if n.dot(inside) - d < 0:
        n, d = n * -1, -d
    x = n.cross(App.Vector(1, 0, 0))
    if x.Length < 1e-6:
        x = n.cross(App.Vector(0, 1, 0))
    x.normalize()
    y = n.cross(x)
    foot = inside - n * (n.dot(inside) - d)
    corner = foot - x * size - y * size
    return Part.makePlane(2 * size, 2 * size, corner, n, x).extrude(n * size)


def _wide_face(g):
    """The surface of face g as a face reaching past the trimmed face where the surface allows (a spline flank stops
    at its own end; a cylinder may run on past its seam, but not round more than once). None if it cannot be made."""
    try:
        srf = g.Surface
        u0, u1, v0, v1 = g.ParameterRange
        du, dv = (u1 - u0), (v1 - v0)
        try:
            b0, b1, c0, c1 = srf.bounds()
            pu = srf.isUPeriodic() and srf.UPeriod() > 0
            pv = srf.isVPeriodic() and srf.VPeriod() > 0
        except Exception:
            b0 = b1 = c0 = c1 = None
            pu = pv = False

        def span(lo, hi, d, grow, blo, bhi, periodic, period):
            a_, b_ = lo - grow * d, hi + grow * d
            if periodic:
                extra = max(0.0, (b_ - a_) - 0.95 * period)
                return a_ + extra / 2, b_ - extra / 2
            return (max(a_, blo) if blo is not None else a_), (min(b_, bhi) if bhi is not None else b_)

        # a narrow face (the land between the roots of two teeth) needs several times its own width: the corner lies
        # beyond its end
        for grow in (3.0, 1.0, 0.5, 0.2, 0.05):
            try:
                ua, ub = span(u0, u1, du, grow, b0, b1, pu, srf.UPeriod() if pu else 0)
                va, vb = span(v0, v1, dv, grow, c0, c1, pv, srf.VPeriod() if pv else 0)
                wide = srf.toShape(ua, ub, va, vb)
                if wide is not None and not wide.isNull() and wide.isValid():
                    return wide
            except Exception:
                pass
    except Exception:
        pass
    return None


def _side_solid(g, edge, inside, size, thick=None, direction=None):
    """The region on the `inside` side of face g's surface, near `edge`, as a solid: a half-space for a plane; for a
    curved face (cylinder, extruded profile, spline: the flank of a tooth) its surface, widened where it can be,
    thickened towards `inside`. None if that cannot be built."""
    pl = _plane_of(g)
    if pl is not None:
        return _half_space(pl, inside, size)
    try:
        srf = g.Surface
        mid = edge.valueAt((edge.FirstParameter + edge.LastParameter) / 2.0)
        u, v = srf.parameter(mid)
        n = srf.normal(u, v)
        if n.dot(inside - mid) < 0:
            n = n * -1
        if direction is not None:               # thicken along this direction instead of the normal
            n = direction
        wide = _wide_face(g)
        if wide is None or wide.isNull():
            return None
        slab = wide.extrude(n * (thick or size))
        return slab if slab.isValid() and abs(slab.Volume) > 1e-12 else None
    except Exception:
        return None


def _toward_strip(edge, c):
    """A point a little way from the middle of `edge` towards c (the middle of the strip)."""
    m = edge.valueAt((edge.FirstParameter + edge.LastParameter) / 2.0)
    return m + (c - m) * 0.5


def _wedge(face, others):
    """The piece a flat chamfer strip cut off: what lies between the strip, the surfaces of the two faces along its
    long edges (continued past the strip) and the planes of the faces at its ends - however many those are (a mitred
    end touches two). `others` = [(edge of the strip, neighbouring face)]. Built as an intersection of solids, so no
    corner has to be computed by hand. Returns a solid or None if the strip is not of that kind."""
    pf = _plane_of(face)
    if len(others) < 4:
        return None
    others = sorted(others, key=lambda x: -x[0].Length)
    (ea, fa), (eb, fb) = others[0], others[1]
    ends = others[2:]
    if ends[0][0].Length > 0.5 * eb.Length:
        return None                                 # not a strip
    width = face.Area / max(ea.Length, 1e-12)
    c = face.CenterOfMass
    size = face.BoundBox.DiagonalLength + 8.0 * width + 1e-3      # reach of the flat blocks: past both ends of the strip
    thick = 6.0 * width + 1e-3                                    # thickness of a slab made from a curved face
    # the corner the chamfer took away lies on the side of the strip where the side faces would meet: for two planes
    # the nearest point of their line, else a point just off the strip on the side away from the neighbours' insides
    pa, pb = _plane_of(fa), _plane_of(fb)
    if pf is None or pa is None or pb is None:
        _wedge.curved += 1
    toward = None
    try:
        um, vm = face.Surface.parameter(c)
        c = face.valueAt(um, vm)                    # on the strip also when it is not flat
        out = face.normalAt(um, vm)
    except Exception:
        return None
    if pf is not None and pa is not None and pb is not None:
        line = _line_of(pa, pb)
        if line is None:
            return None
        q = line[0] + line[1] * (c - line[0]).dot(line[1])
        if (q - c).Length > 4.0 * width:
            return None
        toward = q
    else:                                           # curved side: decide by the turn of the side faces at the strip
        ma = ea.valueAt((ea.FirstParameter + ea.LastParameter) / 2.0)
        mb = eb.valueAt((eb.FirstParameter + eb.LastParameter) / 2.0)
        try:
            ta = fa.normalAt(*fa.Surface.parameter(ma))
            tb = fb.normalAt(*fb.Surface.parameter(mb))
        except Exception:
            return None
        # convex corner: outward normals of the sides point away from each other across the strip -> corner is outside
        convex = (ta + tb).dot(out) > 0
        # only a hair off the strip: at a blunt corner the missing piece is much thinner than the strip is wide
        toward = c + out * (0.02 * width if convex else -0.02 * width)
    bb = face.BoundBox
    pad = 3.0 * width
    try:
        w = Part.makeBox(bb.XLength + 2 * pad, bb.YLength + 2 * pad, bb.ZLength + 2 * pad,
                         App.Vector(bb.XMin - pad, bb.YMin - pad, bb.ZMin - pad))
        box_bb = w.BoundBox
        own = _half_space(pf, toward, size) if pf is not None else _side_solid(face, ea, toward, size, thick)
        if own is None:
            return None
        w = w.common(own)
        # A curved side face is thickened along the OTHER side face, away from the corner: every point of the missing
        # piece, moved back along that direction, meets the continued face. (Along its own normal the points near
        # the other side would miss the end of the surface at a blunt corner, and the piece would come out too small.)
        dirs = {}
        try:
            for key, (e1, g1, e2) in {"a": (eb, fb, ea), "b": (ea, fa, eb)}.items():
                m1 = e1.valueAt((e1.FirstParameter + e1.LastParameter) / 2.0)
                m2 = e2.valueAt((e2.FirstParameter + e2.LastParameter) / 2.0)
                n1 = g1.normalAt(*g1.Surface.parameter(m1))
                t1 = e1.tangentAt((e1.FirstParameter + e1.LastParameter) / 2.0)
                dvec = n1.cross(t1)
                if dvec.Length < 1e-9:
                    return None
                dvec.normalize()
                if dvec.dot(m1 - m2) < 0:
                    dvec = dvec * -1
                dirs[key] = dvec
        except Exception:
            return None
        for key, e, g in (("a", ea, fa), ("b", eb, fb)):
            side = _side_solid(g, e, _toward_strip(e, c), size, thick, dirs[key])
            if side is None:
                return None
            w = w.common(side)
        for e, g in ends:
            pe = _plane_of(g)
            if pe is None:
                # a curved face at the end (the chamfer around the end of a tooth with curved flanks): it is too small
                # to be continued, so the end is closed with its tangent plane at the shared edge. Over the width of a
                # chamfer that differs from the true continuation by microns; a tiny facet stays at that end.
                try:
                    m = e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)
                    ne = g.normalAt(*g.Surface.parameter(m))
                    pe = (ne, ne.dot(m))
                    _wedge.facets += 1
                except Exception:
                    return None
            w = w.common(_half_space(pe, c, size))
        if not w.Solids or len(w.Solids) != 1:
            return None
        w = w.Solids[0]
        wb = w.BoundBox
        tol = 1e-6
        if not w.isValid() or w.Volume < 1e-12:
            return None
        # closed by the given faces only: it must not reach the box it was cut from (except where the strip does)
        grown = (wb.XMin <= box_bb.XMin + tol or wb.XMax >= box_bb.XMax - tol or wb.YMin <= box_bb.YMin + tol
                 or wb.YMax >= box_bb.YMax - tol or wb.ZMin <= box_bb.ZMin + tol or wb.ZMax >= box_bb.ZMax - tol)
        if grown or w.Volume > 2.0 * width * width * ea.Length:
            return None
        return w
    except Exception:
        return None


_wedge.facets = 0
_wedge.curved = 0


def _round_piece(f, d, faces, adj, picked, emap=None, mode="split"):
    """What a round along a curved edge took away (convex) or added (hollow): the triangle chord - corner of each
    section, clipped by the two side surfaces continued (exact) and by the faces at the ends. The chord lies on the far
    side of the arc, so the piece holds the whole rounded region; the rest of it overlaps material (convex) or air
    (hollow) and changes nothing.
    mode "split": the triangle cut by the two side surfaces (continued), keeping the cell that holds the round - exact
    wherever the side surfaces reach the corner (a full-round root between two curved flanks; a land continued to the
    flank). "normal" / "other": each side surface thickened along its own normal / along the other side face, a
    fallback where a side surface ends short of the corner."""
    try:
        i = d["face"]
        if d.get("rev"):
            return _rev_piece(d, faces) if mode == "split" else None
        others = _strip_others(i, faces, adj, picked, any_surface=True, emap=emap)
        if others is None:
            return None
        (ea, fa, _), (eb, fb, _) = others[0], others[1]
        tool = strip_tool(dict(d, kind="chamfer"), 1.0)
        if tool is None:
            return None
        width = d["width"]
        c = App.Vector(*d["probe"])
        if mode == "split":
            wa, wb = _wide_face(fa), _wide_face(fb)
            if wa is None or wb is None:
                return None
            cells = [x for x in tool.generalFuse([wa, wb])[0].Solids if x.BoundBox.isInside(c)]
            cells = [x for x in cells if x.isInside(c, 1e-7, False)]
            if len(cells) != 1:
                return None
            w = cells[0]
            if not w.isValid() or w.Volume < 1e-12 or w.Volume > 3.0 * width * width * ea.Length                     or w.Volume > tool.Volume * (1 - 1e-6):     # the side surfaces did not cut it: not a corner piece
                return None

            def bounded_by(wide):
                for x in w.Faces:
                    try:
                        u, v = x.Surface.parameter(x.CenterOfMass) if hasattr(x.Surface, "parameter") else (0, 0)
                        if wide.distToShape(Part.Vertex(x.valueAt(u, v)))[0] < 1e-6:
                            return True
                    except Exception:
                        continue
                return False
            # both side surfaces must close the cell (one that ends inside the triangle lets it run round its end)
            if not (bounded_by(wa) and bounded_by(wb)):
                return None
            return w
        size, thick = f.BoundBox.DiagonalLength + 8.0 * width + 1e-3, 6.0 * width + 1e-3
        w = tool
        # each side surface thickened along the OTHER side face, away from the corner (as in _wedge): a spline flank
        # cannot be continued past its end, and the corner lies beyond it
        for (e1, g1, e2), (e, g) in (((eb, fb, ea), (ea, fa)), ((ea, fa, eb), (eb, fb))):
            m1 = e1.valueAt((e1.FirstParameter + e1.LastParameter) / 2.0)
            m2 = e2.valueAt((e2.FirstParameter + e2.LastParameter) / 2.0)
            n1 = g1.normalAt(*g1.Surface.parameter(m1))
            dvec = n1.cross(e1.tangentAt((e1.FirstParameter + e1.LastParameter) / 2.0))
            if dvec.Length < 1e-9:
                return None
            dvec.normalize()
            if dvec.dot(m1 - m2) < 0:
                dvec = dvec * -1
            side = _side_solid(g, e, _toward_strip(e, c), size, thick, None if mode == "normal" else dvec)
            if side is None:
                return None
            w = w.common(side)
        if not w.Solids or len(w.Solids) != 1:
            return None
        w = w.Solids[0]
        if not w.isValid() or w.Volume < 1e-12 or w.Volume > 3.0 * width * width * ea.Length:
            return None
        return w
    except Exception:
        return None


def _rev_piece(d, faces):
    """_round_piece for a torus piece: corner - arc turned about the axis, split by the faces at its ends (continued);
    the cell that holds the round."""
    tool = _rev_tool(d, 1.0, exact=True)
    if tool is None:
        return None
    walls = [_wide_face(faces[j]) for j in d["rev"]["end_faces"]]
    if any(w is None for w in walls):
        return None
    c = App.Vector(*d["probe"])
    cells = [x for x in tool.generalFuse(walls)[0].Solids if x.BoundBox.isInside(c)]
    cells = [x for x in cells if x.isInside(c, 1e-7, False)]
    if len(cells) != 1:
        return None
    w = cells[0]
    if not w.isValid() or w.Volume < 1e-12 or w.Volume > tool.Volume * (1 - 1e-6):
        return None
    def on(wall, x):
        try:
            return wall.distToShape(Part.Vertex(x.valueAt(*x.Surface.parameter(x.CenterOfMass))))[0] < 1e-6
        except Exception:
            return False
    for wall in walls:              # every end must close the cell
        if not any(on(wall, x) for x in w.Faces):
            return None
    return w


def _ring(i, faces, adj):
    """The ring a conical chamfer cut off between a coaxial cylinder (hole or shaft) and a flat face square to the
    axis: (solid, key, test point) or None. The key is the same for both halves of a cone stored as two faces."""
    f = faces[i]
    axis = App.Vector(f.Surface.Axis)
    axis.normalize()
    cyl = pln = None
    for e in f.Edges:
        if type(e.Curve).__name__ != "Circle":
            continue
        for j in adj[i]:
            g = faces[j]
            if not any(e.isSame(x) for x in g.Edges):
                continue
            k = type(g.Surface).__name__
            if k == "Cylinder" and App.Vector(g.Surface.Axis).cross(axis).Length < 1e-6 \
                    and abs(g.Surface.Radius - e.Curve.Radius) < 1e-6:
                cyl = e
            elif k == "Plane" and abs(abs(App.Vector(g.Surface.Axis).dot(axis)) - 1.0) < 1e-6:
                pln = e
    if cyl is None or pln is None:
        return None
    r_c, r_p = cyl.Curve.Radius, pln.Curve.Radius
    z_c, z_p = axis.dot(cyl.Curve.Center), axis.dot(pln.Curve.Center)
    if abs(r_c - r_p) < 1e-7 or abs(z_c - z_p) < 1e-7:
        return None
    base = App.Vector(cyl.Curve.Center) - axis * z_c
    u = axis.cross(App.Vector(1, 0, 0))
    if u.Length < 1e-6:
        u = axis.cross(App.Vector(0, 1, 0))
    u.normalize()

    def at(r, z):
        return base + axis * z + u * r

    try:
        tri = Part.Face(Part.makePolygon([at(r_c, z_c), at(r_p, z_p), at(r_c, z_p), at(r_c, z_c)]))
        solid = tri.revolve(base, axis, 360)
        if solid.ShapeType != "Solid":
            solid = Part.Solid(Part.Shell(solid.Faces))
        if solid.Volume < 0:
            solid.reverse()
        if not solid.isValid() or solid.Volume < 1e-12:
            return None
    except Exception:
        return None
    key = (round(base.x, 4), round(base.y, 4), round(base.z, 4), round(abs(axis.x), 5), round(abs(axis.y), 5),
           round(r_c, 5), round(r_p, 5), round(z_c, 4), round(z_p, 4))
    return solid, key, at((2 * r_c + r_p) / 3.0, (z_c + 2 * z_p) / 3.0)


def fill_wedges(shape, face_ids, faces=None):
    """Remove flat chamfer strips that lie between flat faces, and conical chamfers between a cylinder and a flat face
    (rims of holes, ends of shafts), by putting back exactly the wedge / ring each one cut off (or, in a hollow
    corner, taking away what it left). Local and exact - no surface of the part has to be extended by the kernel,
    so it stays fast on parts with thousands of faces. Works where the outline route does not: teeth with a step,
    chamfers that stop somewhere along an edge. Strips that touch another picked face at an end are not handled here.
    -> (new shape, [face numbers removed]) or (None, [])"""
    faces = faces if faces is not None else shape.Faces
    picked = set(int(i) for i in face_ids)
    adj = _adjacent(faces)
    add, cut, done = [], [], []
    rings = {}
    _wedge.facets = _wedge.curved = 0
    rounds = {}
    loose = []          # round pieces: they also hold the sliver between chord and arc, which is material (convex) or
                        # air (hollow) already, so the volume they change is less than their own
    curved_ids = [i for i in picked if type(faces[i].Surface).__name__ not in ("Plane", "Cylinder", "Cone", "Sphere")]
    if curved_ids:
        try:
            rounds = {d["face"]: d for d in strip_plan(shape, sorted(picked), faces) if d["kind"] == "fillet"}
        except Exception:
            rounds = {}
    emap = _edge_map(faces, adj, list(rounds)) if rounds else None
    for i in sorted(picked):
        f = faces[i]
        if i in rounds:                             # rounds along curved edges: below, two ways
            continue
        if type(f.Surface).__name__ == "Cone":      # chamfer on the rim of a hole or the end of a shaft
            r = _ring(i, faces, adj)
            if r is not None:
                solid, key, probe = r
                if key not in rings:
                    rings[key] = True
                    (cut if shape.isInside(probe, 1e-7, False) else add).append(solid)
                done.append(i)
            continue
        if type(f.Surface).__name__ in ("Cylinder", "Toroid", "Sphere") or not 4 <= len(f.Edges) <= 8:
            continue                                # rounds are not chamfers; a strip has two long edges and its ends
        others = []
        for e in f.Edges:
            hit = [j for j in adj[i] if any(e.isSame(x) for x in faces[j].Edges)]
            if len(hit) != 1:
                break
            others.append((e, faces[hit[0]], hit[0]))
        if len(others) != len(f.Edges):
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
    if not rounds:
        return _settle(shape, faces, add, cut, done, loose, len(rings))
    best = (None, [])
    for mode in ("split", "normal", "other"):
        a2, c2, l2, d2 = list(add), list(cut), [], list(done)
        _wedge.curved = 0
        for i in sorted(rounds):
            w = _round_piece(faces[i], rounds[i], faces, adj, picked, emap, mode=mode)
            if w is not None:
                (a2 if rounds[i]["convex"] else c2).append(w)
                l2.append(w)
                d2.append(i)
                _wedge.curved += 1
        if not l2:
            continue
        res = _settle(shape, faces, a2, c2, d2, l2, len(rings))
        # both ways stop at the side surfaces, so neither takes too much: the one that changes more left less behind
        if res[0] is not None and (best[0] is None or len(res[1]) > len(best[1]) or (len(res[1]) == len(best[1]) and
                                   abs(res[0].Volume - shape.Volume) > abs(best[0].Volume - shape.Volume) + 1e-9)):
            best = res
        if mode == "split" and best[0] is not None and len(best[1]) == len(done) + len(rounds):
            break                                   # every round cut exactly along its own sides: nothing to better
    if best[0] is not None:
        return best
    if done:                                        # the rounds did not work out: the rest on its own
        _wedge.curved = 0
        best = _settle(shape, faces, add, cut, done, loose, len(rings))
    return best


def _settle(shape, faces, add, cut, done, loose, n_rings):
    """fill_wedges: put the pieces in / take them out and check the result."""
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
        ids_loose = {id(w) for w in loose}
        want = shape.Volume + sum(w.Volume for w in add if id(w) not in ids_loose)             - sum(w.Volume for w in cut if id(w) not in ids_loose)
        tol = 1e-5 * max(abs(want), 1.0) + 1e-3
        lo = want - sum(w.Volume for w in cut if id(w) in ids_loose) - tol
        hi = want + sum(w.Volume for w in add if id(w) in ids_loose) + tol
        if not out.isValid() or not lo <= out.Volume <= hi:
            return None, []
        if loose and abs(out.Volume - shape.Volume) < 1e-9:
            return None, []     # nothing changed at all: the pieces did not reach the part
        refined = out.copy().removeSplitter()
        if refined.isValid() and abs(refined.Volume - out.Volume) <= 1e-6 * max(out.Volume, 1.0):
            out = refined
        # curved neighbours are not merged with the piece that continues them (a seam line stays along the old chamfer),
        # and a tangent-plane end leaves a facet: allow for those, but not for anything else
        if len(out.Faces) > len(faces) - len(done) + n_rings + 2 * _wedge.facets + 3 * _wedge.curved:
            return None, []         # pieces did not close cleanly against the neighbours (slivers left): not this way
        return out, done
    except Exception:
        return None, []


# ------------------------------------------------------------------ strips as a parameter, without the kernel's chamfer
# Where the CAD kernel cannot put a chamfer back on the restored edge (curved flanks, ends that run into other chamfers)
# the strip is still a parameter: its cross-section is a triangle corner - leg - leg, known all along the strip, and a
# chamfer of another size is the same triangle scaled about the corner. The part is then the sharp solid minus (or, in
# a hollow corner, plus) a tool swept through the scaled triangles and stopped by the faces at the strip's ends.
def _closest(p1, d1, p2, d2):
    """Where the lines p1 + s d1 and p2 + r d2 come closest: (midpoint, gap, s, r) or None if parallel."""
    w = p1 - p2
    a, b, c = d1.dot(d1), d1.dot(d2), d2.dot(d2)
    d, e = d1.dot(w), d2.dot(w)
    den = a * c - b * b
    if abs(den) < 1e-12:
        return None
    s, r = (b * e - c * d) / den, (a * e - b * d) / den
    q1, q2 = p1 + d1 * s, p2 + d2 * r
    return (q1 + q2) * 0.5, (q1 - q2).Length, s, r


def _sections(ea, fa, eb, fb, width, n):
    """n cross-sections of a strip between edge ea (on face fa) and eb (on fb): points A on ea, B on eb and the corner
    P where the two side faces, continued straight on from the edges, meet. None if they do not meet sensibly."""
    t0, t1 = ea.FirstParameter, ea.LastParameter
    ts = sorted(min(max(ea.Curve.parameter(v.Point), t0), t1) for v in eb.Vertexes)
    if len(ts) != 2 or ts[1] - ts[0] < 0.3 * (t1 - t0):
        return None
    A, B, P = [], [], []
    for k in range(n):
        t = ts[0] + (ts[1] - ts[0]) * k / (n - 1.0)
        a = ea.valueAt(t)
        tb = min(max(eb.Curve.parameter(a), eb.FirstParameter), eb.LastParameter)
        b = eb.valueAt(tb)
        da = fa.normalAt(*fa.Surface.parameter(a)).cross(ea.tangentAt(t))
        db = fb.normalAt(*fb.Surface.parameter(b)).cross(eb.tangentAt(tb))
        if da.Length < 1e-9 or db.Length < 1e-9:
            return None
        da.normalize()
        db.normalize()
        if da.dot(b - a) < 0:
            da = da * -1
        if db.dot(a - b) < 0:
            db = db * -1
        hit = _closest(a, da, b, db)
        if hit is None or hit[1] > 0.2 * width or hit[2] <= 0 or hit[3] <= 0 or max(hit[2], hit[3]) > 6 * width:
            return None
        A.append(a)
        B.append(b)
        P.append(hit[0])
    return A, B, P


def _arc_mid(f, a, b):
    """The point of curved strip f half-way across, between a and b (nearest to the middle of the chord)."""
    m = (a + b) * 0.5
    u, v = f.Surface.parameter(m)
    return f.valueAt(u, v)


def fillet_radius(f, others):
    """Radius of a round strip f whose cross-section is a circular arc tangent to both side faces (a fillet along a
    curved edge: on a spline flank the kernel stores it as a spline surface), or None. others: [(edge, face, ...)]
    longest first."""
    (ea, fa), (eb, fb) = others[0][:2], others[1][:2]
    width = f.Area / max(ea.Length, 1e-12)
    sec = _sections(ea, fa, eb, fb, width, 5)
    if sec is None:
        return None
    radii = []
    for a, b, p in zip(*sec):
        m = _arc_mid(f, a, b)
        chord = (b - a).Length
        if chord < 1e-9 or ((a + b) * 0.5 - m).Length < 0.05 * chord:
            return None                              # flat across: a chamfer, not a round
        la, lb = (a - p).Length, (b - p).Length
        if abs(la - lb) > 0.03 * max(la, lb):
            return None                              # unequal legs: not a constant-radius round
        u, w = a - p, b - p
        u.normalize()
        w.normalize()
        half = 0.5 * math.acos(max(-1.0, min(1.0, u.dot(w))))
        if half < 1e-3 or half > 0.5 * math.pi - 1e-3:
            return None
        r = 0.5 * (la + lb) * math.tan(half)        # tangent at a and b: centre on the bisector
        bis = u + w
        bis.normalize()
        c = p + bis * (0.5 * (la + lb) / math.cos(half))
        if abs((m - c).Length - r) > 0.03 * r:
            return None                              # middle not on that circle
        radii.append(r)
    if max(radii) - min(radii) > 0.02 * max(radii):
        return None
    return sum(radii) / len(radii)


def curved_fillets(shape, faces=None, adj=None):
    """{face number: radius} of the round strips along curved edges (see fillet_radius). Analytic rounds (cylinders,
    tori) are found elsewhere (machined.find_blends)."""
    faces = faces if faces is not None else shape.Faces
    out = {}
    cand = [i for i, f in enumerate(faces) if type(f.Surface).__name__ in ("BSplineSurface", "BezierSurface",
            "SurfaceOfExtrusion", "SurfaceOfRevolution", "OffsetSurface") and 4 <= len(f.Edges) <= 8]
    if not cand:
        return out
    adj = adj if adj is not None else _adjacent(faces)
    # edge -> faces once: comparing every edge with every edge of a big neighbour (the end face of a gear with
    # hundreds of edges) made this grow with the square of the size
    emap = {}
    for j in {j for i in cand for j in adj[i]} | set(cand):
        for e in faces[j].Edges:
            emap.setdefault(e.hashCode(), []).append((j, e))
    for i in cand:
        try:
            others = _strip_others(i, faces, adj, set(), any_surface=True, emap=emap)
            if others is None:
                continue
            r = fillet_radius(faces[i], others)
            if r is not None:
                out[i] = r
        except Exception:
            continue
    return out


def _edge_map(faces, adj, ids):
    """edge hash -> [(face, edge)] for faces ids and their neighbours (see curved_fillets)."""
    emap = {}
    for j in {j for i in ids for j in adj[i]} | set(ids):
        for e in faces[j].Edges:
            emap.setdefault(e.hashCode(), []).append((j, e))
    return emap


def _strip_others(i, faces, adj, picked, any_surface=False, emap=None):
    """[(edge, neighbour face, its number)] for strip i, longest edges first, or None if it is not a simple strip."""
    f = faces[i]
    if not any_surface and type(f.Surface).__name__ in ("Cylinder", "Toroid", "Sphere", "Cone"):
        return None
    if not 4 <= len(f.Edges) <= 8:
        return None
    others = []
    for e in f.Edges:
        if emap is not None:
            hit = sorted({j for j, x in emap.get(e.hashCode(), ()) if j != i and j in adj[i] and e.isSame(x)})
        else:
            hit = [j for j in adj[i] if any(e.isSame(x) for x in faces[j].Edges)]
        if len(hit) != 1:
            return None
        others.append((e, faces[hit[0]], hit[0]))
    others.sort(key=lambda x: -x[0].Length)
    if others[2][0].Length > 0.5 * others[1][0].Length or any(j in picked for _, _, j in others[2:]):
        return None
    return others


def strip_plan(shape, face_ids, faces=None):
    """What is needed to cut each chamfer strip again at any size: [{"face", "a", "b", "p" (points along the two long
    edges and the corner line), "ends" (planes that stop it), "convex", "leg", "width", "slack", "probe"}]. Plain
    numbers only, so it can be stored in the document."""
    faces = faces if faces is not None else shape.Faces
    picked = set(int(i) for i in face_ids)
    key = (len(faces), round(shape.Volume, 6), round(shape.Area, 6), tuple(sorted(picked)))
    if key in _PLANS:
        return [dict(d) for d in _PLANS[key]]
    adj = _adjacent(faces)
    emap = _edge_map(faces, adj, picked)
    out = []

    def r6(v):
        return [round(v.x, 7), round(v.y, 7), round(v.z, 7)]

    for i in sorted(picked):
        try:
            f = faces[i]
            if type(f.Surface).__name__ == "Toroid":
                d = _rev_plan(i, faces, adj, emap, shape)
                if d is not None:
                    out.append(d)
                continue
            others = _strip_others(i, faces, adj, picked, any_surface=True, emap=emap)
            if others is None:
                continue
            if type(f.Surface).__name__ in ("Cylinder", "Toroid", "Sphere", "Cone"):
                continue                            # analytic rounds: PartDesign's fillet takes those
            (ea, fa, _), (eb, fb, _) = others[0], others[1]
            width = f.Area / max(ea.Length, 1e-12)
            straight = type(ea.Curve).__name__ == "Line" and type(eb.Curve).__name__ == "Line"
            n = 3 if straight else 13
            radius = fillet_radius(f, others)
            sec = _sections(ea, fa, eb, fb, width, n)
            if sec is None:
                continue
            A, B, P = sec
            M = [_arc_mid(f, a, b) for a, b in zip(A, B)] if radius else None
            ends = []
            c = f.CenterOfMass
            for e, g, _ in others[2:]:
                pe = _plane_of(g)
                if pe is None:
                    m = e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)
                    ne = g.normalAt(*g.Surface.parameter(m))
                    pe = (ne, ne.dot(m))
                nn, dd = pe
                if nn.dot(c) - dd < 0:
                    nn, dd = nn * -1, -dd
                ends.append([nn.x, nn.y, nn.z, dd])
            mid = len(A) // 2
            # a point of what the strip took away (or added): for a round between its arc and the corner
            probe = (M[mid] + P[mid]) * 0.5 if radius else (A[mid] + B[mid] + P[mid]) * (1.0 / 3.0)
            leg = sum((A[k] - P[k]).Length + (B[k] - P[k]).Length for k in range(n)) / (2.0 * n)
            if radius is None and any(((A[k] + B[k]) * 0.5 - _arc_mid(f, A[k], B[k])).Length > 0.05 * (B[k] - A[k]).Length
                                      for k in (0, n // 2, n - 1)):
                continue                            # round but not a circle of constant radius: not handled
            overlap = sum((A[k + 1] - A[k]).Length for k in range(n - 1))
            out.append({"face": i, "a": [r6(v) for v in A], "b": [r6(v) for v in B], "p": [r6(v) for v in P],
                        "ends": [[round(x, 9) for x in e] for e in ends], "convex": not shape.isInside(probe, 1e-7, False),
                        "leg": leg, "width": width, "slack": max(ea.Length - overlap, 0.0), "probe": r6(probe),
                        "straight": straight, "reach": f.BoundBox.DiagonalLength,
                        "kind": "fillet" if radius else "chamfer", "size": radius if radius else leg,
                        **({"m": [r6(v) for v in M]} if radius else {})})
        except Exception:
            continue
    if len(_PLANS) > 16:
        _PLANS.clear()
    _PLANS[key] = [dict(d) for d in out]
    return out

_PLANS = {}


# ------------------------------------------------- a round that runs round an axis but stops at other faces (torus piece)
# e.g. the corner between the floor (cone) and the end wall (plane) of a tooth slot that stops part-way, ended by the
# two tooth flanks. Its section is the same in every half-plane through the axis, so the corner piece is that section
# turned about the axis; PartDesign's fillet cannot put it back on the short arcs between the flanks.
def _is_coaxial_circle(e, C, z):
    c = e.Curve
    if type(c).__name__ != "Circle":
        return False
    ax = App.Vector(c.Axis)
    if ax.cross(z).Length > 1e-6:
        return False
    w = App.Vector(c.Center) - C
    return (w - z * w.dot(z)).Length < 1e-5


def _rev_plan(i, faces, adj, emap, shape):
    """strip_plan for a torus piece i (see above), or None. Its two long edges are circles about the torus' axis
    (where it meets the two faces it rounds); the others end it, and at least one of those is not flat."""
    f = faces[i]
    srf = f.Surface
    C, z = App.Vector(srf.Center), App.Vector(srf.Axis)
    z.normalize()
    R, r = srf.MajorRadius, srf.MinorRadius
    side, ends = [], []
    for e in f.Edges:
        hit = sorted({j for j, x in emap.get(e.hashCode(), ()) if j != i and j in adj[i] and e.isSame(x)})
        if len(hit) != 1:
            return None
        (side if _is_coaxial_circle(e, C, z) else ends).append((e, hit[0]))
    if len(side) != 2 or side[0][1] == side[1][1] or len(ends) < 2:
        return None
    if all(_plane_of(faces[j]) is not None for _, j in ends):
        return None                                 # flat ends: PartDesign's fillet takes those
    w = f.CenterOfMass - C
    e1 = w - z * w.dot(z)
    if e1.Length < 1e-9:
        return None
    e1.normalize()
    e2 = z.cross(e1)
    angs = []
    for e in f.Edges:
        for q in e.discretize(16):
            v = q - C
            angs.append(math.atan2(v.dot(e2), v.dot(e1)))
    a0, a1 = min(angs), max(angs)
    if a1 - a0 > math.pi or a1 - a0 < 1e-6:
        return None
    V = App.Vector

    def rz(q):
        v = q - C
        h = v.dot(z)
        return V((v - z * h).Length, h, 0)

    ta, tb = (rz(e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)) for e, _ in side)
    cc = V(R, 0, 0)
    if abs((ta - cc).Length - r) > 0.01 * r or abs((tb - cc).Length - r) > 0.01 * r:
        return None
    da, db = ta - cc, tb - cc
    da, db = V(-da.y, da.x, 0), V(-db.y, db.x, 0)      # the faces' lines in the section: square to the radius
    den = da.x * db.y - da.y * db.x
    if abs(den) < 1e-9:
        return None                                 # parallel: not a corner
    q = tb - ta
    t = (q.x * db.y - q.y * db.x) / den
    P2 = ta + da * t
    u = P2 - cc
    if u.Length < 1e-9:
        return None
    u.normalize()
    M2 = cc + u * r
    mg = 0.25 * (a1 - a0)                           # the tool runs on a little past the ends (into material / air)

    def at(p2, a):
        return C + (e1 * math.cos(a) + e2 * math.sin(a)) * p2.x + z * p2.y

    def r6(v):
        return [round(v.x, 7), round(v.y, 7), round(v.z, 7)]

    am = 0.5 * (a0 + a1)
    stations = [a0 + (a1 - a0) * k / 4.0 for k in range(5)]
    probe = at((M2 + P2) * 0.5, am)
    leg = 0.5 * ((ta - P2).Length + (tb - P2).Length)
    return {"face": i, "a": [r6(at(ta, a)) for a in stations], "b": [r6(at(tb, a)) for a in stations],
            "p": [r6(at(P2, a)) for a in stations], "m": [r6(at(M2, a)) for a in stations], "ends": [],
            "convex": not shape.isInside(probe, 1e-7, False), "leg": leg, "width": r, "slack": 0.0,
            "probe": r6(probe), "straight": False, "reach": f.BoundBox.DiagonalLength, "kind": "fillet", "size": r,
            "rev": {"c": r6(C), "z": r6(z), "e1": r6(e1), "e2": r6(e2), "a0": a0 - mg, "a1": a1 + mg,
                    "ta": [ta.x, ta.y], "tb": [tb.x, tb.y], "p": [P2.x, P2.y], "m": [M2.x, M2.y],
                    "end_faces": [j for _, j in ends]}}


def _rev_tool(d, k, exact=False):
    """strip_tool for a torus piece: the section (corner - arc, scaled by k about the corner) turned about the axis
    over the piece and a little past its ends. Not exact: widened outwards past the two faces, so no face of the tool
    lies on one of the part (and past the ends it only meets material it fuses with / air it cuts). exact: just the
    corner - arc region (to remove the round, then split by the end faces)."""
    V = App.Vector
    rv = d["rev"]
    C, z, e1, e2 = (V(*rv[q]) for q in ("c", "z", "e1", "e2"))
    p = V(rv["p"][0], rv["p"][1], 0)
    a, b, m = (p + (V(rv[q][0], rv[q][1], 0) - p) * k for q in ("ta", "tb", "m"))
    if exact:
        pts = [a, m, b, p]
        edges2 = [("arc", 0, 1, 2), ("line", 2, 3), ("line", 3, 0)]
    else:
        uu, ww = a - p, b - p
        leg = 0.5 * (uu.Length + ww.Length)
        uu.normalize()
        ww.normalize()
        na = (ww - uu * ww.dot(uu)) * -1
        nb = (uu - ww * uu.dot(ww)) * -1
        na.normalize()
        nb.normalize()
        e = 0.5 * leg + 1e-3
        bis = uu + ww
        bis.normalize()
        pts = [a, m, b, b + nb * e, p - bis * (leg + e), a + na * e]
        edges2 = [("arc", 0, 1, 2), ("line", 2, 3), ("line", 3, 4), ("line", 4, 5), ("line", 5, 0)]
    if min(x.x for x in pts) <= 1e-6:
        return None                                 # would cross the axis
    a0 = rv["a0"]
    rad = e1 * math.cos(a0) + e2 * math.sin(a0)
    P3 = [C + rad * x.x + z * x.y for x in pts]
    es = []
    for kind, *ix in edges2:
        if kind == "arc":
            es.append(Part.Arc(P3[ix[0]], P3[ix[1]], P3[ix[2]]).toShape())
        else:
            es.append(Part.makeLine(P3[ix[0]], P3[ix[1]]))
    try:
        sol = Part.Face(Part.Wire(es)).revolve(C, z, math.degrees(rv["a1"] - rv["a0"]))
        if sol.ShapeType != "Solid":
            sol = Part.Solid(Part.Shell(sol.Faces))
        if sol.Volume < 0:
            sol.reverse()
        return sol if sol.isValid() and sol.Volume > 1e-12 else None
    except Exception:
        return None


def strip_tool(d, k):
    """The solid that makes strip d a chamfer k times its original size when cut from (fused to) the sharp part."""
    V = App.Vector
    A, B, P = ([V(*v) for v in d[q]] for q in ("a", "b", "p"))
    n = len(A)
    grow = max(k, 1.0)
    if d.get("rev"):
        return _rev_tool(d, k)
    if d.get("kind") == "fillet":
        return _round_tool(d, k, A, B, P, [V(*v) for v in d["m"]], grow)
    secs = []
    for i in range(n):
        a, b = P[i] + (A[i] - P[i]) * k, P[i] + (B[i] - P[i]) * k
        m = (a + b) * 0.5
        # a little wider than the strip and reaching past the corner: the tool must hold the whole corner piece even
        # where the side faces bulge, and its sides must lie outside the part
        secs.append((a + (a - b) * 0.25, b + (b - a) * 0.25, P[i] + (P[i] - m)))
    # past both ends, straight on: the faces at the ends stop it
    ext = d["slack"] + 4.0 * d["width"] * grow + 1e-3
    t0, t1 = P[0] - P[1], P[-1] - P[-2]
    t0.normalize()
    t1.normalize()
    first = tuple(x + t0 * ext for x in secs[0])
    last = tuple(x + t1 * ext for x in secs[-1])
    secs = [first, last] if d.get("straight") else [first] + secs + [last]
    wires = [Part.makePolygon([s[0], s[1], s[2], s[0]]) for s in secs]
    tool = Part.makeLoft(wires, True, bool(d.get("straight")))
    if tool.Volume < 0:
        tool.reverse()
    size = d["reach"] + 2 * ext + 8.0 * d["width"] * grow
    c = V(*d["probe"])
    for nx, ny, nz, dd in d["ends"]:
        nn = V(nx, ny, nz)
        inside = c if nn.dot(c) - dd > 1e-9 else c + nn * (1e-6 + dd - nn.dot(c))
        tool = tool.common(_half_space((nn, dd), inside, size))
    if not tool.Solids:
        return None
    return tool.Solids[0] if len(tool.Solids) == 1 else max(tool.Solids, key=lambda s: s.Volume)


def _round_tool(d, k, A, B, P, M, grow):
    """strip_tool for a round: each section is the region between the corner and the arc (scaled by k about the
    corner), widened outwards past the side faces so that no face of the tool lies on a face of the part."""
    V = App.Vector
    wires = []
    ext = d["slack"] + 4.0 * d["width"] * grow + 1e-3
    t0, t1 = P[0] - P[1], P[-1] - P[-2]
    t0.normalize()
    t1.normalize()
    idx = list(range(len(A)))
    stations = [(0, t0 * ext)] + [(i, V()) for i in idx] + [(len(A) - 1, t1 * ext)]
    if d.get("straight"):
        stations = [stations[0], stations[-1]]
    for i, shift in stations:
        p = P[i]
        a, b, m = p + (A[i] - p) * k, p + (B[i] - p) * k, p + (M[i] - p) * k
        u, w = a - p, b - p
        leg = 0.5 * (u.Length + w.Length)
        u.normalize()
        w.normalize()
        na = (w - u * w.dot(u)) * -1
        nb = (u - w * u.dot(w)) * -1
        na.normalize()
        nb.normalize()
        e = 0.5 * leg + 1e-3
        bis = u + w
        bis.normalize()
        a_out, b_out, p_far = a + na * e, b + nb * e, p - bis * (leg + e)
        pts = [x + shift for x in (a, m, b, b_out, p_far, a_out)]
        arc = Part.Arc(pts[0], pts[1], pts[2]).toShape()
        wires.append(Part.Wire([arc, Part.makeLine(pts[2], pts[3]), Part.makeLine(pts[3], pts[4]),
                                Part.makeLine(pts[4], pts[5]), Part.makeLine(pts[5], pts[0])]))
    tool = Part.makeLoft(wires, True, bool(d.get("straight")))
    if tool.Volume < 0:
        tool.reverse()
    size = d["reach"] + 2 * ext + 8.0 * d["width"] * grow
    c = V(*d["probe"])
    for nx, ny, nz, dd in d["ends"]:
        nn = V(nx, ny, nz)
        inside = c if nn.dot(c) - dd > 1e-9 else c + nn * (1e-6 + dd - nn.dot(c))
        tool = tool.common(_half_space((nn, dd), inside, size))
    if not tool.Solids:
        return None
    return tool.Solids[0] if len(tool.Solids) == 1 else max(tool.Solids, key=lambda s: s.Volume)


def apply_strips(shape, plans, k):
    """The sharp `shape` with every strip of `plans` cut as a chamfer k times its original size. None if that fails."""
    if not 0.02 <= k <= 5.0:
        return None
    try:
        cut, add = [], []
        for d in plans:
            t = strip_tool(d, k)
            if t is None or not t.isValid():
                return None
            (cut if d["convex"] else add).append(t)
        out = shape
        if cut:
            out = out.cut(cut, 1e-6)
        if add:
            out = out.fuse(add, 1e-6)
        if not out.Solids or len(out.Solids) != len(shape.Solids):
            return None
        out = out.Solids[0] if len(out.Solids) == 1 else out
        if not out.isValid():
            return None
        try:
            refined = out.copy().removeSplitter()
            if refined.isValid() and abs(refined.Volume - out.Volume) <= 1e-6 * max(out.Volume, 1.0):
                out = refined
        except Exception:
            pass
        return out
    except Exception:
        return None
