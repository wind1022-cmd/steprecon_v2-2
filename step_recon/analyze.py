"""STEP/IGES -> structured geometry summary (runs inside freecadcmd)."""
import math
import Part


def _orient(solid):
    """Some exporters write solids with inward-pointing faces (negative volume); flip them."""
    if solid.Volume < 0:
        solid = solid.copy()
        solid.reverse()
    return solid


def _merge_solids(solids):
    """Fuse overlapping/touching solids into one; truly disjoint bodies stay separate (compound).
    Falls back to a plain compound if the boolean looks wrong (see the B-spline note in compare.py)."""
    solids = [_orient(s) for s in solids]
    vols = [s.Volume for s in solids]       # measured before any boolean: failed OCC ops can corrupt inputs in place
    try:
        copies = [s.copy() for s in solids]
        fused = copies[0].fuse(copies[1:])
        try:
            # merge coplanar faces; optional: OCC can fail or return a broken shape, then keep the unrefined fuse
            refined = fused.copy().removeSplitter()
            # (volumes of curved solids are integrated numerically: a good refine still moves them by ~1e-5)
            if refined.isValid() and len(refined.Solids) == len(fused.Solids) \
                    and abs(refined.Volume - fused.Volume) <= 2e-4 * max(1.0, abs(fused.Volume)):
                fused = refined
        except Exception:
            pass
        parts = [_orient(p) for p in fused.Solids]
        total = sum(p.Volume for p in parts)
        if parts and max(vols) * 0.999 <= total <= sum(vols) * 1.001 and fused.isValid():
            return parts[0] if len(parts) == 1 else Part.makeCompound(parts)
    except Exception:
        pass
    return Part.makeCompound(solids)


merge_solids = _merge_solids    # public name (fc_tools merges the bodies of a reconstruction the same way)


def solid_table(shape):
    """For a shape with several solids: (per-solid info list, {face index in shape.Faces: solid index}).
    Returns ([], {}) for a single solid."""
    solids = shape.Solids
    if len(solids) < 2:
        return [], {}
    faces = shape.Faces
    owner, k, contiguous = {}, 0, True
    for si, s in enumerate(solids):         # a compound normally lists its faces solid by solid
        sf = s.Faces
        if k + len(sf) > len(faces) or not faces[k].isSame(sf[0]):
            contiguous = False
            break
        for _ in sf:
            owner[k] = si
            k += 1
    if not contiguous or k != len(faces):   # unusual ordering: match every face explicitly
        owner = {}
        for si, s in enumerate(solids):
            for f in s.Faces:
                for i, g in enumerate(faces):
                    if i not in owner and f.isSame(g):
                        owner[i] = si
                        break
    info = []
    for si, s in enumerate(solids):
        bb = s.BoundBox
        info.append({"solid": si, "volume": _r(s.Volume), "n_faces": len(s.Faces),
                     "face_ids": sorted(i for i, o in owner.items() if o == si),
                     "bbox_min": [_r(bb.XMin), _r(bb.YMin), _r(bb.ZMin)],
                     "bbox_max": [_r(bb.XMax), _r(bb.YMax), _r(bb.ZMax)]})
    return info, owner


def load_shape_info(path):
    """Return (shape, raw_solid_count). Multi-solid files are merged where bodies overlap or touch."""
    shape = Part.read(path)
    raw = len(shape.Solids)
    if shape.ShapeType == "Solid":
        shape = _orient(shape)
    else:
        solids = shape.Solids
        if solids:
            shape = _orient(solids[0]) if len(solids) == 1 else _merge_solids(solids)
        elif shape.Shells:
            shape = Part.Solid(shape.Shells[0])
    return shape, raw


def load_shape(path):
    return load_shape_info(path)[0]


# Surface class names as FreeCAD reports them (a torus is "Toroid"); everything else counts as freeform.
ANALYTIC = ("Plane", "Cylinder", "Cone", "Sphere", "Toroid")
FREEFORM_EXCLUDE = ANALYTIC


def freeform_clusters(shape):
    """Group non-analytic faces into connected clusters (shared edge). Returns a list of dicts with
    face_ids (indices into shape.Faces), area and a bounding box."""
    ids = [i for i, f in enumerate(shape.Faces) if type(f.Surface).__name__ not in FREEFORM_EXCLUDE]
    parent = {i: i for i in ids}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    faces = shape.Faces
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            if any(e1.isSame(e2) for e1 in faces[ids[a]].Edges for e2 in faces[ids[b]].Edges):
                parent[find(ids[b])] = find(ids[a])
    groups = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    out = []
    for g in groups.values():
        bb = faces[g[0]].BoundBox
        for i in g[1:]:
            bb.add(faces[i].BoundBox)
        out.append({"face_ids": sorted(g), "area": _r(sum(faces[i].Area for i in g)),
                    "bbox_min": [_r(bb.XMin), _r(bb.YMin), _r(bb.ZMin)],
                    "bbox_max": [_r(bb.XMax), _r(bb.YMax), _r(bb.ZMax)]})
    out.sort(key=lambda c: -c["area"])
    return out


def _r(x, n=3):
    return round(float(x), n)


def _vec(v):
    return [_r(v.x), _r(v.y), _r(v.z)]


def _face_point(f):
    u0, u1, v0, v1 = f.ParameterRange
    u, v = (u0 + u1) / 2, (v0 + v1) / 2
    return f.valueAt(u, v), u, v


def face_info(idx, f):
    s = f.Surface
    pt, u, v = _face_point(f)
    t = type(s).__name__
    d = {"id": idx, "type": t, "area": _r(f.Area), "edges": len(f.Edges)}
    bb = f.BoundBox
    d["bbox"] = [_r(bb.XMin), _r(bb.YMin), _r(bb.ZMin), _r(bb.XMax), _r(bb.YMax), _r(bb.ZMax)]
    if t == "Plane":
        n = f.normalAt(u, v)
        d["normal"] = _vec(n)
        d["offset"] = _r(n.dot(pt))
        # what a designer would sketch: the boundary of the face (usable directly via pad/pocket face=<id>)
        try:
            outer = f.OuterWire
            kinds = {}
            for e in outer.Edges:
                k = type(e.Curve).__name__
                k = {"Line": "lines", "LineSegment": "lines", "Circle": "arcs"}.get(k, "other")
                if k == "arcs" and e.isClosed():
                    k = "circles"
                kinds[k] = kinds.get(k, 0) + 1
            d["outline"] = kinds
            d["holes"] = len([w for w in f.Wires if not w.isSame(outer)])
        except Exception:
            pass
    elif t == "Cylinder":
        d["radius"] = _r(s.Radius)
        d["axis"] = _vec(s.Axis)
        d["center"] = _vec(s.Center)
        n = f.normalAt(u, v)
        # concave (hole) if normal points toward the axis
        to_axis = s.Center - pt
        to_axis = to_axis - s.Axis * to_axis.dot(s.Axis)
        d["concave"] = bool(n.dot(to_axis) > 0)
    elif t == "Cone":
        d["radius"] = _r(s.Radius)
        d["axis"] = _vec(s.Axis)
        d["half_angle_deg"] = _r(math.degrees(s.SemiAngle))
    elif t == "Toroid":
        d["major_radius"] = _r(s.MajorRadius)
        d["minor_radius"] = _r(s.MinorRadius)
        d["axis"] = _vec(s.Axis)
        d["center"] = _vec(s.Center)
    elif t == "Sphere":
        d["radius"] = _r(s.Radius)
    return d


def adjacency(shape):
    """face index -> sorted neighbour face indices (shared edges)."""
    edge_faces = {}
    for i, f in enumerate(shape.Faces):
        for e in f.Edges:
            for j, e2 in enumerate(shape.Edges):
                if e.isSame(e2):
                    edge_faces.setdefault(j, set()).add(i)
                    break
    adj = {i: set() for i in range(len(shape.Faces))}
    for fs in edge_faces.values():
        for a in fs:
            adj[a] |= fs - {a}
    return {i: sorted(v) for i, v in adj.items()}


def analyze(path, with_adjacency=True):
    shape, raw_solids = load_shape_info(path)
    bb = shape.BoundBox
    faces = [face_info(i, f) for i, f in enumerate(shape.Faces)]
    hist = {}
    for f in faces:
        hist[f["type"]] = hist.get(f["type"], 0) + 1
    free = [f for f in faces if f["type"] not in ANALYTIC]
    freeform = None
    if free:
        freeform = {"n_faces": len(free), "face_ids": [f["id"] for f in free],
                    "area": _r(sum(f["area"] for f in free)),
                    "bbox_min": [_r(min(f["bbox"][i] for f in free)) for i in range(3)],
                    "bbox_max": [_r(max(f["bbox"][i + 3] for f in free)) for i in range(3)],
                    "clusters": freeform_clusters(shape)}
    corners = [(x, y, z) for x in (bb.XMin, bb.XMax) for y in (bb.YMin, bb.YMax) for z in (bb.ZMin, bb.ZMax)]
    for f in faces:
        if f["type"] == "Plane":    # how much of the part lies behind the face (upper bound for a pad from it)
            n = f["normal"]
            f["max_depth"] = _r(f["offset"] - min(n[0] * c[0] + n[1] * c[1] + n[2] * c[2] for c in corners))
    solids, owner = solid_table(shape)
    for i, si in owner.items():
        faces[i]["solid"] = si
    out = {"solids": solids} if solids else {}      # only for multi-body parts: one entry per separate solid
    out.update({
        "n_solids": len(shape.Solids), "n_solids_raw": raw_solids,
        "freeform": freeform,
        "file": path,
        "volume": _r(shape.Volume),
        "area": _r(shape.Area),
        "center_of_mass": _vec(shape.CenterOfMass if hasattr(shape, "CenterOfMass") else bb.Center),
        "bbox": {"min": [_r(bb.XMin), _r(bb.YMin), _r(bb.ZMin)],
                 "max": [_r(bb.XMax), _r(bb.YMax), _r(bb.ZMax)],
                 "size": [_r(bb.XLength), _r(bb.YLength), _r(bb.ZLength)]},
        "n_faces": len(faces), "n_edges": len(shape.Edges),
        "face_type_histogram": hist,
        "faces": faces,
        "adjacency": adjacency(shape) if with_adjacency else {},
    })
    return out

