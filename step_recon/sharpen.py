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
        u0, u1, v0, v1 = g.ParameterRange
        du, dv = (u1 - u0), (v1 - v0)
        wide = None
        for grow in (0.5, 0.2, 0.05):           # the surface usually reaches past the trimmed face: use more of it
            try:
                b0, b1, c0, c1 = srf.bounds()
                wide = srf.toShape(max(u0 - grow * du, b0), min(u1 + grow * du, b1),
                                   max(v0 - grow * dv, c0), min(v1 + grow * dv, c1))
                if wide is not None and not wide.isNull():
                    break
            except Exception:
                wide = None
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
    for i in sorted(picked):
        f = faces[i]
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
        if not out.isValid() or abs(out.Volume - want) > 1e-5 * max(abs(want), 1.0) + 1e-3:
            return None, []
        refined = out.copy().removeSplitter()
        if refined.isValid() and abs(refined.Volume - out.Volume) <= 1e-6 * max(out.Volume, 1.0):
            out = refined
        # curved neighbours are not merged with the piece that continues them (a seam line stays along the old chamfer),
        # and a tangent-plane end leaves a facet: allow for those, but not for anything else
        if len(out.Faces) > len(faces) - len(done) + len(rings) + 2 * _wedge.facets + 3 * _wedge.curved:
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


def _strip_others(i, faces, adj, picked):
    """[(edge, neighbour face, its number)] for strip i, longest edges first, or None if it is not a simple strip."""
    f = faces[i]
    if type(f.Surface).__name__ in ("Cylinder", "Toroid", "Sphere", "Cone") or not 4 <= len(f.Edges) <= 8:
        return None
    others = []
    for e in f.Edges:
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
    adj = _adjacent(faces)
    out = []

    def r6(v):
        return [round(v.x, 7), round(v.y, 7), round(v.z, 7)]

    for i in sorted(picked):
        try:
            others = _strip_others(i, faces, adj, picked)
            if others is None:
                continue
            f = faces[i]
            (ea, fa, _), (eb, fb, _) = others[0], others[1]
            width = f.Area / max(ea.Length, 1e-12)
            # the stretch of ea that has eb opposite
            t0, t1 = ea.FirstParameter, ea.LastParameter
            ts = sorted(min(max(ea.Curve.parameter(v.Point), t0), t1) for v in eb.Vertexes)
            if len(ts) != 2 or ts[1] - ts[0] < 0.3 * (t1 - t0):
                continue
            straight = type(ea.Curve).__name__ == "Line" and type(eb.Curve).__name__ == "Line"
            n = 3 if straight else 13
            A, B, P = [], [], []
            ok = True
            for k in range(n):
                t = ts[0] + (ts[1] - ts[0]) * k / (n - 1.0)
                a = ea.valueAt(t)
                tb = min(max(eb.Curve.parameter(a), eb.FirstParameter), eb.LastParameter)
                b = eb.valueAt(tb)
                da = fa.normalAt(*fa.Surface.parameter(a)).cross(ea.tangentAt(t))
                db = fb.normalAt(*fb.Surface.parameter(b)).cross(eb.tangentAt(tb))
                if da.Length < 1e-9 or db.Length < 1e-9:
                    ok = False
                    break
                da.normalize()
                db.normalize()
                if da.dot(b - a) < 0:
                    da = da * -1
                if db.dot(a - b) < 0:
                    db = db * -1
                hit = _closest(a, da, b, db)
                if hit is None or hit[1] > 0.2 * width or hit[2] <= 0 or hit[3] <= 0 or max(hit[2], hit[3]) > 6 * width:
                    ok = False
                    break
                A.append(a)
                B.append(b)
                P.append(hit[0])
            if not ok:
                continue
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
            probe = (A[mid] + B[mid] + P[mid]) * (1.0 / 3.0)
            leg = sum((A[k] - P[k]).Length + (B[k] - P[k]).Length for k in range(n)) / (2.0 * n)
            overlap = sum((A[k + 1] - A[k]).Length for k in range(n - 1))
            out.append({"face": i, "a": [r6(v) for v in A], "b": [r6(v) for v in B], "p": [r6(v) for v in P],
                        "ends": [[round(x, 9) for x in e] for e in ends], "convex": not shape.isInside(probe, 1e-7, False),
                        "leg": leg, "width": width, "slack": max(ea.Length - overlap, 0.0), "probe": r6(probe),
                        "straight": straight, "reach": f.BoundBox.DiagonalLength})
        except Exception:
            continue
    return out


def strip_tool(d, k):
    """The solid that makes strip d a chamfer k times its original size when cut from (fused to) the sharp part."""
    V = App.Vector
    A, B, P = ([V(*v) for v in d[q]] for q in ("a", "b", "p"))
    n = len(A)
    grow = max(k, 1.0)
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
