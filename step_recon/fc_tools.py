"""FreeCAD PartDesign tool wrappers used by the agent (headless)."""
import math
import FreeCAD as App
import Part
import Sketcher

from . import compare as cmp_mod

PLANES = {"XY": "XY_Plane", "XZ": "XZ_Plane", "YZ": "YZ_Plane"}


def _v(p):
    return App.Vector(float(p[0]), float(p[1]), 0)


class Session:
    def __init__(self, ref_shape, name="Recon"):
        self.ref = ref_shape
        self.doc = App.newDocument(name)
        self.body = self.doc.addObject("PartDesign::Body", "Body")   # the ACTIVE body: every tool acts on it
        self.bodies = [self.body]   # all bodies, in creation order (index = body number shown to the model)
        self.history = []   # list of lists of created object names (for undo)
        self.hist_body = []  # name of the body each history entry belongs to (parallel to history)
        self.log = []       # human-readable feature log
        self._body_prev = {}  # body name -> name of the body that was active when it was created

    # ---------- helpers ----------
    def _record(self, created, desc):
        self.history.append(created)
        self.hist_body.append(self.body.Name)
        self.log.append(desc)

    def _body_index(self, body=None):
        name = (body or self.body).Name
        return [b.Name for b in self.bodies].index(name)

    @staticmethod
    def _shape_of(body):
        t = body.Tip if body.Tip is not None else body.BaseFeature
        return t.Shape if t is not None and not t.Shape.isNull() else None

    def result_shape(self):
        """Everything built so far: the active body's shape, or with several bodies their solids merged the same
        way the reference was loaded (touching/overlapping bodies fuse, separate ones form a compound)."""
        shapes = [s for s in (self._shape_of(b) for b in self.bodies) if s is not None]
        if not shapes:
            return None
        if len(shapes) == 1:
            return shapes[0]
        from . import analyze
        solids = [x for s in shapes for x in s.Solids]
        if len(solids) < 2:
            return solids[0] if solids else Part.makeCompound(shapes)
        return analyze.merge_solids(solids)

    def _bodies_info(self):
        out = []
        for i, b in enumerate(self.bodies):
            s = self._shape_of(b)
            d = {"body": i, "active": b.Name == self.body.Name, "empty": s is None}
            if s is not None:
                bb = s.BoundBox
                d.update(volume=round(s.Volume, 4), faces=len(s.Faces),
                         bbox=[round(v, 3) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)])
            out.append(d)
        return out

    def _plane(self, name):
        for o in self.body.Origin.OriginFeatures:
            if o.Role == PLANES[name]:
                return o
        raise ValueError("unknown plane " + name)

    def _tip_obj(self):
        """Last feature, or the imported base feature when no feature has been added yet."""
        return self.body.Tip if self.body.Tip is not None else self.body.BaseFeature

    def _tip_shape(self):
        t = self._tip_obj()
        return t.Shape if t is not None else None

    # ---------- hybrid: keep freeform geometry from the reference ----------
    def freeform_boxes(self, margin=0.5, clusters=None):
        """One padded bounding box (min, max) per connected cluster of freeform faces, largest area first.
        `clusters` optionally selects clusters by their index in that order."""
        from . import analyze
        cl = analyze.freeform_clusters(self.ref)
        if clusters is not None:
            cl = [cl[i] for i in clusters if 0 <= i < len(cl)]
        return [([v - margin for v in c["bbox_min"]], [v + margin for v in c["bbox_max"]]) for c in cl]

    def freeform_bbox(self, margin=0.5):
        """Single box around ALL freeform faces (union of the cluster boxes), or None."""
        boxes = self.freeform_boxes(margin)
        if not boxes:
            return None
        return ([min(b[0][i] for b in boxes) for i in range(3)], [max(b[1][i] for b in boxes) for i in range(3)])

    def _import_boxes(self, boxes, desc):
        """Freeze the part of the reference inside the union of axis-aligned boxes as the body's base feature."""
        if self.history or self.body.BaseFeature is not None:
            return {"ok": False, "error": "import must be the first step (undo features first)"}
        solids = [Part.makeBox(mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2], App.Vector(*mn)) for mn, mx in boxes]
        region = solids[0] if len(solids) == 1 else solids[0].fuse(solids[1:])
        piece = self.ref.common(region)
        try:
            piece = piece.copy().removeSplitter()      # optional tidy-up; OCC may fail, never trust in-place changes
        except Exception:
            pass
        if piece.isNull() or piece.Volume < 1e-9:
            return {"ok": False, "error": "box does not intersect the reference"}
        base = self.doc.addObject("Part::Feature", "ImportedRegion")
        base.Shape = piece
        self.body.BaseFeature = base
        self.doc.recompute()
        self._record([base.Name], desc)
        frac = piece.Volume / self.ref.Volume
        out = {"ok": True, "feature": base.Name, "volume": round(piece.Volume, 4), "faces": len(piece.Faces),
               "imported_fraction_pct": round(frac * 100, 1), "solids": len(piece.Solids), "boxes": len(boxes)}
        if frac > 0.9:
            out["warning"] = ("the frozen region is >90% of the part: almost nothing stays parametric. Consider a "
                              "smaller margin, selecting only some clusters, or accept that this part is freeform.")
        return out

    def import_region(self, min, max):
        """Freeze the part of the reference inside one explicit box (min/max [x,y,z]). First step only."""
        return self._import_boxes([(list(min), list(max))], f"import_region {list(min)}..{list(max)}")

    def import_freeform(self, margin=0.5, split=True, clusters=None):
        """Freeze the reference around its freeform (B-spline etc.) faces. split=True uses one box per connected
        cluster of freeform faces (keeps far-apart analytic regions parametric); split=False uses one big box.
        `clusters` picks clusters by index (largest area first, see 'freeform.clusters' in the summary)."""
        boxes = self.freeform_boxes(margin, clusters) if split or clusters is not None else []
        if not split and clusters is None:
            fb = self.freeform_bbox(margin)
            boxes = [fb] if fb else []
        if not boxes:
            return {"ok": False, "error": "reference has no freeform faces (or no such cluster)"}
        r = self._import_boxes(boxes, f"import_freeform margin={margin} split={split} clusters={clusters}")
        if r.get("ok"):
            r["box_list"] = [[[round(v, 2) for v in mn], [round(v, 2) for v in mx]] for mn, mx in boxes]
        return r

    def _make_sketch(self, plane, offset, geometry):
        """Create a sketch; if the geometry is bad, leave no half-built sketch behind."""
        before = {o.Name for o in self.doc.Objects}
        try:
            return self._build_sketch(plane, offset, geometry)
        except Exception:
            for o in list(self.doc.Objects):
                if o.Name not in before:
                    self.doc.removeObject(o.Name)
            raise

    def _make_sketches(self, specs):
        """Several sketches from (plane, offset, geometry) tuples; all or none."""
        made = []
        try:
            for plane, offset, geometry in specs:
                made.append(self._make_sketch(plane, offset, geometry))
        except Exception:
            for sk in made:
                self.doc.removeObject(sk.Name)
            raise
        return made

    @staticmethod
    def _frame(plane):
        """Origin and axes (o, x, y, z) of an arbitrary sketch plane
        {"origin":[x,y,z], "normal":[..], "x_dir":[..]} given in global coordinates."""
        z = App.Vector(*plane["normal"]).normalize()
        xd = App.Vector(*plane.get("x_dir", [1, 0, 0]))
        x = (xd - z * xd.dot(z))
        if x.Length < 1e-9:
            x = z.cross(App.Vector(0, 0, 1)) if abs(z.z) < 0.9 else App.Vector(1, 0, 0)
        x.normalize()
        return App.Vector(*plane["origin"]), x, z.cross(x), z

    def _face_profile(self, face, loops="outer", into_material=True):
        """Sketch plane + geometry reproducing the boundary of a planar face of the REFERENCE.
        loops: outer | inner | all. The sketch normal points into the material (into_material) or out of it."""
        from . import analyze
        faces = self.ref.Faces
        idx = int(face)
        if not 0 <= idx < len(faces):
            raise ValueError(f"face {idx} out of range (0..{len(faces) - 1})")
        f = faces[idx]
        kind = type(f.Surface).__name__
        if kind != "Plane":
            raise ValueError(f"face {idx} is a {kind}; only Plane faces have a flat outline")
        if loops not in ("outer", "inner", "all"):
            raise ValueError("loops must be outer, inner or all")
        pt, u, v = analyze._face_point(f)
        n = f.normalAt(u, v)
        z = n * (-1.0 if into_material else 1.0)
        plane = {"origin": [pt.x, pt.y, pt.z], "normal": [z.x, z.y, z.z]}
        # lay the sketch axes along the outline's longest straight side, with one of its ends as origin: the
        # sides then come out horizontal / vertical and get plain dimensions, also on a tilted part
        straight = [e for e in f.OuterWire.Edges if type(e.Curve).__name__ in ("Line", "LineSegment")]
        if straight:
            e0 = max(straight, key=lambda e: e.Length)
            a, b = e0.valueAt(e0.FirstParameter), e0.valueAt(e0.LastParameter)
            d = b - a
            plane["origin"] = [a.x, a.y, a.z]
            plane["x_dir"] = [d.x, d.y, d.z]
        o, x, y, _ = self._frame(plane)
        plane["x_dir"] = [x.x, x.y, x.z]

        def loc(p):
            d = p - o
            return [d.dot(x), d.dot(y)]

        outer = f.OuterWire
        inner = [w for w in f.Wires if not w.isSame(outer)]
        wires = {"outer": [outer], "inner": inner, "all": [outer] + inner}[loops]
        if not wires:
            raise ValueError(f"face {idx} has no inner loops (holes); use loops=outer")
        geo = []
        for w in wires:
            for e in w.Edges:
                c = e.Curve
                t = type(c).__name__
                a, b = e.FirstParameter, e.LastParameter
                if t in ("Line", "LineSegment"):
                    geo.append({"type": "line", "p1": loc(e.valueAt(a)), "p2": loc(e.valueAt(b))})
                elif t == "Circle":
                    cen = loc(c.Center)
                    if e.isClosed() or abs(abs(b - a) - 2 * math.pi) < 1e-9:
                        geo.append({"type": "circle", "c": cen, "r": c.Radius})
                        continue

                    def ang(p):
                        return math.degrees(math.atan2(p[1] - cen[1], p[0] - cen[0])) % 360.0

                    s, m, en = ang(loc(e.valueAt(a))), ang(loc(e.valueAt((a + b) / 2))), ang(loc(e.valueAt(b)))
                    a0, a1 = (s, en) if (m - s) % 360.0 <= (en - s) % 360.0 else (en, s)    # counter-clockwise
                    if a1 <= a0:
                        a1 += 360.0
                    geo.append({"type": "arc", "c": cen, "r": c.Radius, "a0": a0, "a1": a1})
                else:
                    raise ValueError(f"the outline of face {idx} contains a {t} edge, which sketches here cannot "
                                     f"reproduce; draw the profile with plane + geometry instead")
        return plane, geo

    def _build_sketch(self, plane, offset, geometry):
        sk = self.body.newObject("Sketcher::SketchObject", "Sketch")
        sk.MapMode = "FlatFace"
        if isinstance(plane, dict):
            # arbitrary plane, expressed as an offset placement relative to the XY plane
            o, x, y, z = self._frame(plane)
            m = App.Matrix(x.x, y.x, z.x, o.x, x.y, y.y, z.y, o.y, x.z, y.z, z.z, o.z, 0, 0, 0, 1)
            sk.AttachmentSupport = (self._plane("XY"), [""])
            sk.AttachmentOffset = App.Placement(m)
        else:
            sk.AttachmentSupport = (self._plane(plane), [""])
            sk.AttachmentOffset = App.Placement(App.Vector(0, 0, float(offset)), App.Rotation())
        for g in geometry:
            t = g["type"]
            if t == "line":
                sk.addGeometry(Part.LineSegment(_v(g["p1"]), _v(g["p2"])), False)
            elif t == "circle":
                sk.addGeometry(Part.Circle(_v(g["c"]), App.Vector(0, 0, 1), float(g["r"])), False)
            elif t == "arc":
                circ = Part.Circle(_v(g["c"]), App.Vector(0, 0, 1), float(g["r"]))
                sk.addGeometry(Part.ArcOfCircle(circ, math.radians(g["a0"]), math.radians(g["a1"])), False)
            elif t == "rect":
                (x0, y0), (x1, y1) = g["p1"], g["p2"]
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                for i in range(4):
                    sk.addGeometry(Part.LineSegment(_v(pts[i]), _v(pts[(i + 1) % 4])), False)
            else:
                raise ValueError("unknown geometry type " + t)
        # join coincident endpoints so the wire is closed
        # (endpoints within a tolerance are merged: real data and LLM-given numbers are rounded)
        ends = []   # (geometry index, point id, x, y)
        for i in range(sk.GeometryCount):
            g = sk.Geometry[i]
            if hasattr(g, "StartPoint"):
                for pid, pt in ((1, g.StartPoint), (2, g.EndPoint)):
                    ends.append((i, pid, pt.x, pt.y))
        if ends:
            xs = [e[2] for e in ends]
            ys = [e[3] for e in ends]
            tol = max(1e-3, 3e-4 * max(max(xs) - min(xs), max(ys) - min(ys)))
        parent = list(range(len(ends)))

        def find(k):
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        for a in range(len(ends)):
            for b in range(a + 1, len(ends)):
                if ends[a][0] != ends[b][0] and abs(ends[a][2] - ends[b][2]) < tol \
                        and abs(ends[a][3] - ends[b][3]) < tol:
                    parent[find(b)] = find(a)
        groups = {}
        for k in range(len(ends)):
            groups.setdefault(find(k), []).append(ends[k])
        for lst in groups.values():
            for a, b in zip(lst, lst[1:]):
                sk.addConstraint(Sketcher.Constraint("Coincident", a[0], a[1], b[0], b[1]))
        self.doc.recompute()
        return sk

    def _finish(self, feat, created, desc):
        if hasattr(feat, "Refine"):
            feat.Refine = True     # merge coplanar faces (needed when fusing with an imported region)
        self.doc.recompute()
        if not feat.isValid() or feat.Shape.isNull():
            for n in reversed(created):
                self.doc.removeObject(n)
            self.doc.recompute()
            return {"ok": False, "error": f"{desc}: feature invalid after recompute"}
        self._record(created, desc)
        out = {"ok": True, "feature": feat.Name, "volume": round(feat.Shape.Volume, 4),
               "faces": len(feat.Shape.Faces)}
        if len(self.bodies) > 1:
            out["body"] = self._body_index()
        return out

    def _no_effect(self, created, message):
        """Drop a feature that changed nothing and say where the solid actually is."""
        for n in created:
            self.doc.removeObject(n)
        feats = [o for o in self.body.Group if o.isDerivedFrom("PartDesign::Feature")]
        if feats:
            self.body.Tip = feats[-1]
        self.doc.recompute()
        out = {"ok": False, "error": message}
        t = self._tip_shape()
        if t is not None:
            bb = t.BoundBox
            out["solid_bbox"] = [round(v, 3) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)]
        return out

    def state(self):
        """Short progress report attached to every shape-changing tool result (saves the model a compare call)."""
        s = self.result_shape()
        if s is None:
            return None
        r = cmp_mod.compare(self.ref, s)

        def box(sh):
            bb = sh.BoundBox
            return [round(v, 3) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)]

        out = {"converged": r["converged"], "xor_pct": round(r["xor_pct"], 3),
               "volume": r["new_volume"], "target_volume": r["ref_volume"],
               "bbox": box(s), "target_bbox": box(self.ref),
               "faces": r["new_faces"], "target_faces": r["ref_faces"]}
        for k in ("missing_regions", "extra_regions"):
            if r.get(k):
                out[k] = r[k][:3]
        if not r["converged"]:
            if out["bbox"] != out["target_bbox"] and r["volume_diff_pct"] < 1.0:
                out["hint"] = "volume matches but the solid is in the wrong place or orientation: make bbox equal target_bbox"
            elif r["xor_pct"] < 0.1:
                out["hint"] = "shape matches; only the face count differs (a missing/extra fillet, chamfer or split face)"
        return out

    # ---------- tools ----------
    def new_body(self):
        """Start another, separate body (one per disjoint solid of the reference) and make it active."""
        if self._tip_shape() is None:
            return {"ok": False, "error": "the active body is still empty; build it before starting another body"}
        prev = self.body.Name
        b = self.doc.addObject("PartDesign::Body", "Body")
        self.bodies.append(b)
        self.body = b
        self._body_prev[b.Name] = prev
        self._record([b.Name], f"new_body (body {len(self.bodies) - 1})")
        return {"ok": True, "active_body": len(self.bodies) - 1, "bodies": len(self.bodies)}

    def select_body(self, index):
        """Make an existing body the active one (0-based, creation order)."""
        index = int(index)
        if not 0 <= index < len(self.bodies):
            return {"ok": False, "error": f"body index {index} out of range (0..{len(self.bodies) - 1})"}
        self.body = self.bodies[index]
        return {"ok": True, "active_body": index, "bodies": self._bodies_info()}

    def pad(self, plane=None, geometry=None, length=None, offset=0, reversed=False, midplane=False, face=None,
            holes=False):
        if length is None:
            raise ValueError("length is required")
        where = f"{plane}@{offset}"
        if face is not None:    # outline taken from a planar face of the reference, extruded into the material
            plane, geometry = self._face_profile(face, "all" if holes else "outer", into_material=True)
            offset, where = 0, f"face{int(face)}" + ("+holes" if holes else "")
        elif plane is None or geometry is None:
            raise ValueError("give either face, or plane and geometry")
        before = self._tip_shape()
        sk = self._make_sketch(plane, offset, geometry)
        feat = self.body.newObject("PartDesign::Pad", "Pad")
        feat.Profile = sk
        feat.Length = float(length)
        feat.Reversed = bool(reversed)
        feat.Midplane = bool(midplane)
        self.doc.recompute()
        if before is not None and feat.isValid() and not feat.Shape.isNull() \
                and abs(feat.Shape.Volume - before.Volume) < 1e-6:
            return self._no_effect([feat.Name, sk.Name], "pad added nothing: the profile lies entirely inside the "
                                   "existing solid (check plane, offset and direction)")
        return self._finish(feat, [feat.Name, sk.Name],
                            f"pad {where} len={length} rev={reversed} mid={midplane}")

    def pocket(self, plane=None, geometry=None, depth=None, offset=0, reversed=False, through_all=False, face=None,
               loops="inner"):
        before = self._tip_shape()
        if before is None:
            return {"ok": False, "error": "pocket needs an existing solid; use pad first"}
        if depth is None and not through_all:
            raise ValueError("give depth or through_all")
        where = f"{plane}@{offset}"
        if face is not None:
            # inner loops (openings in that face): cut into the material behind the face;
            # outer loop (the face is the floor of a recess): cut from the floor out toward the open side
            plane, geometry = self._face_profile(face, loops, into_material=(loops == "outer"))
            offset, where = 0, f"face{int(face)}/{loops}"
        elif plane is None or geometry is None:
            raise ValueError("give either face, or plane and geometry")
        v0 = before.Volume
        sk = self._make_sketch(plane, offset, geometry)
        feat = self.body.newObject("PartDesign::Pocket", "Pocket")
        feat.Profile = sk
        if through_all:
            feat.Type = 1
        else:
            feat.Type = 0
            feat.Length = float(depth)
        feat.Reversed = bool(reversed)
        self.doc.recompute()
        if abs(feat.Shape.Volume - v0) < 1e-6 and feat.isValid():   # cut the wrong way -> flip once
            feat.Reversed = not feat.Reversed
            self.doc.recompute()
            if feat.isValid() and not feat.Shape.isNull() and abs(feat.Shape.Volume - v0) < 1e-6:
                return self._no_effect([feat.Name, sk.Name], "pocket removed nothing in either direction: the "
                                       "sketch does not overlap the solid (check plane, offset and position)")
        return self._finish(feat, [feat.Name, sk.Name],
                            f"pocket {where} depth={depth} through={through_all} rev={feat.Reversed}")

    def hole(self, plane, x=None, y=None, diameter=None, depth=None, offset=0, reversed=False, through_all=False,
             positions=None, counterbore_diameter=None, counterbore_depth=None, countersink_diameter=None,
             countersink_angle=90, drill_angle=None):
        """Native PartDesign::Hole: one feature for one or several equal holes at sketch positions, optionally with
        a counterbore, a countersink or a conical (drill point) bottom. `depth` is measured to the shoulder.
        Plain holes fall back to a circular pocket if the Hole feature fails."""
        pts = [[float(p[0]), float(p[1])] for p in (positions or [])]
        if x is not None and y is not None:
            pts.insert(0, [float(x), float(y)])
        if not pts:
            return {"ok": False, "error": "give x and y, or positions=[[x,y],...]"}
        if diameter is None or float(diameter) <= 0:
            return {"ok": False, "error": "diameter is required"}
        if not through_all and depth is None:
            return {"ok": False, "error": "give depth, or through_all=true"}
        if counterbore_diameter is not None and countersink_diameter is not None:
            return {"ok": False, "error": "use either a counterbore or a countersink, not both"}
        if counterbore_diameter is not None and counterbore_depth is None:
            return {"ok": False, "error": "counterbore_depth is required with counterbore_diameter"}
        for big in (counterbore_diameter, countersink_diameter):
            if big is not None and float(big) <= float(diameter):
                return {"ok": False, "error": "counterbore/countersink diameter must be larger than the hole diameter"}
        before = self._tip_shape()
        if before is None:
            return {"ok": False, "error": "hole needs an existing solid; use pad first"}
        v0 = before.Volume
        circles = [{"type": "circle", "c": p, "r": float(diameter) / 2.0} for p in pts]
        plain = counterbore_diameter is None and countersink_diameter is None and drill_angle is None
        desc = f"hole {plane}@{offset} d={diameter} n={len(pts)} " + ("through" if through_all else f"depth={depth}")
        sk = self._make_sketch(plane, offset, circles)
        feat = self.body.newObject("PartDesign::Hole", "Hole")
        feat.Profile = sk
        feat.Threaded = False
        feat.Diameter = float(diameter)
        feat.DepthType = "ThroughAll" if through_all else "Dimension"
        if not through_all:
            feat.Depth = float(depth)
        feat.DrillPoint = "Flat"
        if drill_angle is not None and not through_all:
            feat.DrillPoint = "Angled"
            feat.DrillPointAngle = float(drill_angle)
            feat.DrillForDepth = False          # depth = cylindrical part; the cone tip comes on top of it
            desc += f" drill_angle={drill_angle}"
        if counterbore_diameter is not None:
            feat.HoleCutType = "Counterbore"
            feat.HoleCutDiameter = float(counterbore_diameter)
            feat.HoleCutDepth = float(counterbore_depth)
            desc += f" counterbore d={counterbore_diameter} depth={counterbore_depth}"
        elif countersink_diameter is not None:
            feat.HoleCutType = "Countersink"
            feat.HoleCutDiameter = float(countersink_diameter)
            feat.HoleCutCountersinkAngle = float(countersink_angle)
            desc += f" countersink d={countersink_diameter} angle={countersink_angle}"
        feat.Reversed = bool(reversed)
        self.doc.recompute()
        if feat.isValid() and not feat.Shape.isNull() and abs(feat.Shape.Volume - v0) < 1e-6:   # drilled away from
            feat.Reversed = not feat.Reversed                                                 # the solid: flip once
            self.doc.recompute()
        ok = feat.isValid() and not feat.Shape.isNull() and feat.Shape.Volume < v0 - 1e-9
        if not ok and plain:        # same geometry as a circular pocket
            for n in (feat.Name, sk.Name):
                self.doc.removeObject(n)
            self.doc.recompute()
            return self.pocket(plane, circles, depth=depth, offset=offset, reversed=reversed, through_all=through_all)
        r = self._finish(feat, [feat.Name, sk.Name], desc)
        if r.get("ok"):
            r["holes"] = len(pts)
            if not ok:
                r["warning"] = "the hole removed no material; check plane/offset/direction (undo to retract)"
        return r

    def _two_sketch_feature(self, kind, name, sketches, setup, cut, desc, hint):
        """Shared tail of sweep/loft: `sketches` are already created; on failure everything is removed again."""
        feat = self.body.newObject(f"PartDesign::{'Subtractive' if cut else 'Additive'}{kind}", name)
        try:
            setup(feat)
        except Exception as e:
            self.doc.removeObject(feat.Name)
            for sk in sketches:
                self.doc.removeObject(sk.Name)
            self.doc.recompute()
            return {"ok": False, "error": f"{desc}: {type(e).__name__}: {e}"}
        r = self._finish(feat, [feat.Name] + [sk.Name for sk in sketches], desc)
        if not r.get("ok"):
            r["error"] += ". " + hint
        return r

    def sweep(self, plane, geometry, path_plane, path_geometry, offset=0, path_offset=0, cut=False,
              transition="transformed"):
        """Sweep a closed profile along a planar path of lines/arcs (pipe, handle, bent bar, groove).
        The profile sketch must sit at one end of the path, in a plane perpendicular to the path there."""
        if cut and self._tip_shape() is None:
            return {"ok": False, "error": "a cut sweep needs an existing solid"}
        trans = {"transformed": "Transformed", "right": "Right corner", "round": "Round corner"}.get(str(transition).lower())
        if trans is None:
            return {"ok": False, "error": "transition must be transformed, right or round"}
        prof, path = self._make_sketches([(plane, offset, geometry), (path_plane, path_offset, path_geometry)])

        def setup(f):
            f.Profile = prof
            f.Spine = (path, [])
            f.Mode = "Frenet"
            f.Transition = trans

        return self._two_sketch_feature(
            "Pipe", "Sweep", [prof, path], setup, cut,
            f"{'cut ' if cut else ''}sweep profile {plane}@{offset} along path {path_plane}@{path_offset} "
            f"({len(path_geometry)} segments)",
            "Check that the path is one connected open chain (tangent arcs for bends) and that the profile is at a "
            "path end point, in the plane perpendicular to the path direction there")

    def loft(self, sections, ruled=False, closed=False, cut=False):
        """Loft through 2+ closed section sketches given in order: [{plane, offset, geometry}, ...].
        ruled=True joins neighbouring sections with straight lines (cones / flat sides); otherwise a smooth skin."""
        if not isinstance(sections, (list, tuple)) or len(sections) < 2:
            return {"ok": False, "error": "loft needs at least 2 sections: [{plane, offset, geometry}, ...]"}
        if cut and self._tip_shape() is None:
            return {"ok": False, "error": "a cut loft needs an existing solid"}
        for sec in sections:
            if not isinstance(sec, dict) or "plane" not in sec or "geometry" not in sec:
                return {"ok": False, "error": "every section needs plane and geometry (offset optional)"}
        sks = self._make_sketches([(sec["plane"], sec.get("offset", 0), sec["geometry"]) for sec in sections])

        def setup(f):
            f.Profile = sks[0]
            f.Sections = sks[1:]
            f.Ruled = bool(ruled)
            f.Closed = bool(closed)

        return self._two_sketch_feature(
            "Loft", "Loft", sks, setup, cut,
            f"{'cut ' if cut else ''}loft {len(sks)} sections ruled={bool(ruled)} "
            + " -> ".join(f"{sec['plane']}@{sec.get('offset', 0)}" for sec in sections),
            "Each section must be ONE closed profile, sections must not intersect each other and must be listed in "
            "order along the loft")

    def revolution(self, plane, geometry, axis="V", angle=360, offset=0, cut=False, reversed=False):
        """Revolve (or cut=groove) a closed profile around the sketch's V (vertical) or H (horizontal) axis."""
        if cut and self._tip_shape() is None:
            return {"ok": False, "error": "groove needs an existing solid"}
        sk = self._make_sketch(plane, offset, geometry)
        feat = self.body.newObject("PartDesign::Groove" if cut else "PartDesign::Revolution",
                                   "Groove" if cut else "Revolution")
        feat.Profile = sk
        feat.ReferenceAxis = (sk, ["V_Axis" if axis.upper() == "V" else "H_Axis"])
        feat.Angle = float(angle)
        feat.Reversed = bool(reversed)
        return self._finish(feat, [feat.Name, sk.Name],
                            f"{'groove' if cut else 'revolution'} {plane}@{offset} axis={axis} angle={angle}")

    def _axis(self, name):
        for o in self.body.Origin.OriginFeatures:
            if o.Role == name + "_Axis":
                return o
        raise ValueError("axis must be X, Y or Z")

    def _original(self, index):
        if not -len(self.history) <= index < len(self.history):
            raise ValueError(f"feature_index {index} out of range (0..{len(self.history) - 1})")
        name, owner = self.history[index][0], self.hist_body[index]
        if name == owner:
            raise ValueError(f"feature_index {index} is a new_body step, not a feature")
        if owner != self.body.Name:
            where = [b.Name for b in self.bodies].index(owner)
            raise ValueError(f"feature_index {index} belongs to body {where}, but body {self._body_index()} is active; "
                             f"patterns/mirror stay inside one body (select_body {where} first)")
        return self.doc.getObject(name)

    def _transform(self, kind, index, props, desc):
        orig = self._original(index)
        feat = self.body.newObject("PartDesign::" + kind, kind)
        feat.Originals = [orig]
        for k, v in props(feat).items():
            setattr(feat, k, v)
        self.body.Tip = feat
        return self._finish(feat, [feat.Name], desc)

    def linear_pattern(self, feature_index, direction, length, occurrences):
        """Repeat a feature along global axis X/Y/Z over total `length` (first to last instance)."""
        ax = self._axis(direction.upper())
        return self._transform("LinearPattern", feature_index, lambda f: {
            "Direction": (ax, [""]), "Length": float(length), "Occurrences": int(occurrences)},
            f"linear_pattern feature={feature_index} dir={direction} len={length} n={occurrences}")

    def polar_pattern(self, feature_index, axis, occurrences, angle=360):
        """Repeat a feature around global axis X/Y/Z."""
        ax = self._axis(axis.upper())
        return self._transform("PolarPattern", feature_index, lambda f: {
            "Axis": (ax, [""]), "Angle": float(angle), "Occurrences": int(occurrences)},
            f"polar_pattern feature={feature_index} axis={axis} angle={angle} n={occurrences}")

    def mirror(self, feature_index, plane):
        """Mirror a feature across a global plane XY / XZ / YZ through the origin."""
        pl = self._plane(plane.upper())
        return self._transform("Mirrored", feature_index, lambda f: {"MirrorPlane": (pl, [""])},
                               f"mirror feature={feature_index} plane={plane}")

    def list_faces(self, only_type=None, min_area=0.0):
        """1-based face indices of the current tip (matches 'FaceN'); normals are outward."""
        s = self._tip_shape()
        if s is None:
            return {"ok": False, "error": "no solid yet"}
        out = []
        for i, f in enumerate(s.Faces, 1):
            t = type(f.Surface).__name__
            if (only_type and t.lower() != only_type.lower()) or f.Area < min_area:
                continue
            u0, u1, v0, v1 = f.ParameterRange
            u, v = (u0 + u1) / 2, (v0 + v1) / 2
            p, n = f.valueAt(u, v), f.normalAt(u, v)
            d = {"face": i, "type": t, "area": round(f.Area, 3), "point": [round(p.x, 3), round(p.y, 3), round(p.z, 3)]}
            if t == "Plane":
                d["normal"] = [round(n.x, 4), round(n.y, 4), round(n.z, 4)]
            elif t in ("Cylinder", "Cone"):
                d["radius"] = round(f.Surface.Radius, 3)
            out.append(d)
        return {"ok": True, "faces": out}

    def shell(self, faces, thickness, inward=True, join="arc"):
        """Hollow the solid: remove `faces` (openings) and give the walls `thickness`."""
        base = self._tip_obj()
        if base is None:
            return {"ok": False, "error": "no solid yet"}
        v0 = base.Shape.Volume
        feat = self.body.newObject("PartDesign::Thickness", "Thickness")
        feat.Base = (base, [f"Face{int(i)}" for i in faces])
        feat.Value = float(thickness)
        feat.Mode = 0
        feat.Join = 0 if join == "arc" else 1
        feat.Reversed = bool(inward)
        self.doc.recompute()
        if feat.isValid() and not feat.Shape.isNull():
            shrank = feat.Shape.Volume < v0
            if shrank != bool(inward):      # direction came out opposite to what was asked -> flip once
                feat.Reversed = not feat.Reversed
                self.doc.recompute()
        return self._finish(feat, [feat.Name], f"shell t={thickness} open_faces={list(faces)} inward={inward}")

    def draft(self, faces, angle, neutral_face, pull_axis="Z", reversed=False):
        """Taper `faces` by `angle` degrees about the hinge where they meet `neutral_face`."""
        base = self._tip_obj()
        if base is None:
            return {"ok": False, "error": "no solid yet"}
        feat = self.body.newObject("PartDesign::Draft", "Draft")
        feat.Base = (base, [f"Face{int(i)}" for i in faces])
        feat.NeutralPlane = (base, [f"Face{int(neutral_face)}"])
        feat.PullDirection = (self._axis(pull_axis.upper()), [""])
        feat.Angle = float(angle)
        feat.Reversed = bool(reversed)
        return self._finish(feat, [feat.Name],
                            f"draft {angle}deg faces={list(faces)} neutral=Face{neutral_face} pull={pull_axis}")

    def rib(self, plane, p1, p2, thickness, length, offset=0, reversed=False, midplane=False):
        """Thin straight web: a rectangle of width `thickness` along the line p1->p2 in the sketch plane,
        extruded `length` along the plane normal and fused with the body (MVP of a rib feature)."""
        if self._tip_shape() is None:
            return {"ok": False, "error": "rib needs an existing solid; use pad first"}
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        L = math.hypot(dx, dy)
        if L < 1e-9:
            return {"ok": False, "error": "p1 and p2 coincide"}
        nx, ny = -dy / L * thickness / 2, dx / L * thickness / 2
        pts = [[p1[0] + nx, p1[1] + ny], [p2[0] + nx, p2[1] + ny], [p2[0] - nx, p2[1] - ny], [p1[0] - nx, p1[1] - ny]]
        geom = [{"type": "line", "p1": pts[i], "p2": pts[(i + 1) % 4]} for i in range(4)]
        sk = self._make_sketch(plane, offset, geom)
        feat = self.body.newObject("PartDesign::Pad", "Rib")
        feat.Profile = sk
        feat.Length = float(length)
        feat.Reversed = bool(reversed)
        feat.Midplane = bool(midplane)
        return self._finish(feat, [feat.Name, sk.Name],
                            f"rib {plane}@{offset} t={thickness} len={length} {p1}->{p2}")

    def features(self):
        feats = [{"index": i, "name": h[0], "desc": d} for i, (h, d) in enumerate(zip(self.history, self.log))]
        if len(self.bodies) == 1:
            return {"ok": True, "features": feats}
        names = [b.Name for b in self.bodies]
        for f, owner in zip(feats, self.hist_body):
            f["body"] = names.index(owner)
        return {"ok": True, "features": feats, "bodies": self._bodies_info()}

    def _dress(self, kind, edges, size):
        base = self._tip_obj()
        if base is None:
            return {"ok": False, "error": "no solid yet"}
        feat = self.body.newObject("PartDesign::" + kind, kind)
        feat.Base = (base, [f"Edge{int(i)}" for i in edges])
        if kind == "Fillet":
            feat.Radius = float(size)
        else:
            feat.Size = float(size)
        return self._finish(feat, [feat.Name], f"{kind.lower()} size={size} edges={list(edges)}")

    def fillet(self, edges, radius):
        return self._dress("Fillet", edges, radius)

    def chamfer(self, edges, size):
        return self._dress("Chamfer", edges, size)

    def _dress_hint(self, tip, e, mid):
        """For a sharp edge of the current solid that the reference does not have: is the reference rounded or
        bevelled there, and by how much? Returns {"fillet": r} / {"chamfer": size} or None.
        The corner point lies d away from the reference's surface; with t = angle between the two faces,
        a fillet of radius r gives d = r (1/sin(t/2) - 1), an equal chamfer of size c gives d = c cos(t/2)."""
        try:
            # distance to the reference's SURFACE: at a concave edge the corner point lies inside the reference,
            # where the distance to the solid itself would be zero
            if getattr(self, "_ref_faces", None) is None:
                self._ref_faces = Part.Compound(self.ref.Faces)
            d, _, info = self._ref_faces.distToShape(Part.Vertex(mid))
            if d < 1e-5 or not info or info[0][0] != "Face":
                return None
            kind = type(self.ref.Faces[info[0][1]].Surface).__name__
            faces = tip.ancestorsOfType(e, Part.Face)
            t = math.pi / 2
            if len(faces) == 2:
                n = [f.normalAt(*f.Surface.parameter(mid)) for f in faces]
                t = math.pi - n[0].getAngle(n[1])
            if not 0.2 < t < math.pi - 0.2:
                return None
            if kind in ("Cylinder", "Toroid", "Sphere"):
                return {"fillet": round(d / (1.0 / math.sin(t / 2) - 1.0), 2)}
            if kind in ("Plane", "Cone"):
                return {"chamfer": round(d / math.cos(t / 2), 2)}
        except Exception:
            pass
        return None

    def list_edges(self, only_type=None, min_length=0.0, only_missing=False):
        """1-based edge indices of the current tip shape (matches 'EdgeN' naming).
        Every edge says whether the reference has it too; for those it lacks, `suggest` tells which dress-up
        feature (and size) the reference has in that place. only_missing lists just those edges."""
        s = self._tip_shape()
        if s is None:
            return {"ok": False, "error": "no solid yet"}
        if getattr(self, "_ref_edges", None) is None:
            self._ref_edges = Part.Compound(self.ref.Edges)
        out = []
        for i, e in enumerate(s.Edges, 1):
            t = type(e.Curve).__name__
            if only_type and t.lower() != only_type.lower():
                continue
            if e.Length < min_length:
                continue
            mid = e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)
            try:
                in_ref = self._ref_edges.distToShape(Part.Vertex(mid))[0] < 1e-4
            except Exception:
                in_ref = True
            if only_missing and in_ref:
                continue
            a, b = e.Vertexes[0].Point, e.Vertexes[-1].Point
            d = {"edge": i, "type": t, "length": round(e.Length, 3),
                 "p1": [round(a.x, 3), round(a.y, 3), round(a.z, 3)],
                 "p2": [round(b.x, 3), round(b.y, 3), round(b.z, 3)]}
            if t == "Circle":
                d["radius"] = round(e.Curve.Radius, 3)
            d["in_reference"] = in_ref
            if not in_ref:
                hint = self._dress_hint(s, e, mid)
                if hint:
                    d["suggest"] = hint
            out.append(d)
        res = {"ok": True, "edges": out}
        if only_missing:
            res["note"] = ("edges of your solid that the reference does not have: apply the suggested fillet/chamfer "
                           "(group edges with the same suggestion in one call); an edge without a suggestion "
                           "usually belongs to a wrong or missing feature")
        return res

    def compare(self):
        """Compare ALL bodies together against the reference."""
        s = self.result_shape()
        if s is None:
            return {"ok": False, "error": "no solid yet"}
        r = cmp_mod.compare(self.ref, s)
        r["ok"] = True
        if len(self.bodies) > 1 or r["ref_solids"] > 1:
            r["bodies"] = len(self.bodies)
            if r["new_solids"] < r["ref_solids"] and len(self.bodies) < r["ref_solids"]:
                r["hint"] = (f"the reference has {r['ref_solids']} separate solids; use new_body to build the "
                             f"remaining ones (see missing_regions)")
            elif r["new_solids"] < len([b for b in self.bodies if self._shape_of(b) is not None]):
                r["hint"] = "some bodies touch or overlap each other and were merged for this comparison"
        return r

    def undo(self):
        """Remove the last step (feature, import or empty new body), whichever body it is in."""
        if not self.history:
            return {"ok": False, "error": "nothing to undo"}
        names = self.history.pop()
        self.log.pop()
        owner = self.hist_body.pop()
        body = self.doc.getObject(owner)
        if names[0] == owner:       # a new_body step: by now the body is empty again (later steps were undone first)
            was_active = self.body.Name == owner
            self.bodies = [b for b in self.bodies if b.Name != owner]
            prev = self._body_prev.pop(owner, None)
            if was_active:
                self.body = next((b for b in self.bodies if b.Name == prev), self.bodies[-1])
            body.removeObjectsFromDocument()    # origin planes/axes
            self.doc.removeObject(owner)
            self.doc.recompute()
            return {"ok": True, "remaining_features": len(self.log), "active_body": self._body_index(),
                    "bodies": len(self.bodies)}
        for n in names:
            if body.BaseFeature is not None and body.BaseFeature.Name == n:
                body.BaseFeature = None
                # FreeCAD wraps an imported base in a PartDesign::FeatureBase inside the body; drop it too
                for o in list(body.Group):
                    if o.isDerivedFrom("PartDesign::FeatureBase"):
                        self.doc.removeObject(o.Name)
            self.doc.removeObject(n)
        feats = [o for o in body.Group if o.isDerivedFrom("PartDesign::Feature")]
        if feats:
            body.Tip = feats[-1]
        self.doc.recompute()
        out = {"ok": True, "remaining_features": len(self.log)}
        if len(self.bodies) > 1:
            out["undone_in_body"] = self._body_index(body)
            out["active_body"] = self._body_index()
        return out

    def import_shape(self, shape, desc="import_shape"):
        """Use an arbitrary solid as the frozen (non-parametric) base of the active body. Must be the first step."""
        if self.history or self.body.BaseFeature is not None:
            return {"ok": False, "error": "import must be the first step (undo features first)"}
        base = self.doc.addObject("Part::Feature", "ImportedBlank")
        base.Shape = shape
        self.body.BaseFeature = base
        self.doc.recompute()
        self._record([base.Name], desc)
        return {"ok": True, "feature": base.Name, "volume": round(shape.Volume, 4), "faces": len(shape.Faces)}

    def constrain(self):
        """Fully constrain and dimension every sketch (see constrain.py). The shape must stay the same: if any
        body changes, the document is left for the caller to judge via the returned "shape_changed"."""
        from . import constrain as cons
        self.doc.recompute()
        before = [(b.Name, self._shape_of(b).Volume) for b in self.bodies if self._shape_of(b) is not None]
        info = cons.constrain_document(self.doc)
        # a Hole feature takes its diameter from its own property, not from the sketch circle: tie the two
        # together so that the dimension shown in the sketch is the one that drives the hole
        for o in self.doc.Objects:
            if o.TypeId != "PartDesign::Hole":
                continue
            try:
                sk = o.Profile[0] if isinstance(o.Profile, (tuple, list)) else o.Profile
                i = next(k for k, c in enumerate(sk.Constraints) if c.Type == "Diameter")
                if not sk.Constraints[i].Name:
                    sk.renameConstraint(i, "HoleDiameter")
                o.setExpression("Diameter", f"{sk.Name}.Constraints.{sk.Constraints[i].Name}")
            except Exception:
                pass
        self.doc.recompute()
        after = {b.Name: self._shape_of(b).Volume for b in self.bodies if self._shape_of(b) is not None}
        info["shape_changed"] = any(abs(after.get(n, 0.0) - v) > 1e-6 * max(1.0, abs(v)) for n, v in before)
        return info

    def save(self, path, constrain=True):
        self.doc.recompute()
        out = {"ok": True, "path": path}
        if constrain:
            try:
                c = self.constrain()
                out["sketches"] = {k: c[k] for k in ("sketches", "fully_constrained", "geometric", "dimensions")}
                if c["restored"]:
                    out["sketches"]["left_unconstrained"] = c["restored"]
                if c["shape_changed"]:
                    out["sketches"]["warning"] = "the shape changed while constraining"
            except Exception as e:
                out["sketches"] = {"error": f"{type(e).__name__}: {e}"}
        self.sketch_report = out.get("sketches")
        self.doc.saveAs(path)
        try:
            self._write_gui_visibility(path)
        except Exception:
            pass    # the model itself is saved; only the display defaults are missing
        return out

    def _write_gui_visibility(self, path):
        """A file saved without the GUI has no display data, and FreeCAD then opens it with every Body and
        feature hidden and only the sketches shown ("just lines"). Add the GUI part so each body's result shows."""
        import zipfile
        rows = []
        for o in self.doc.Objects:
            is_body = o.TypeId == "PartDesign::Body"
            vis = is_body or any(b.Tip == o for b in self.bodies)
            rows.append('<ViewProvider name="%s" expanded="%d"><Properties Count="1" TransientCount="0">'
                        '<Property name="Visibility" type="App::PropertyBool" status="1"><Bool value="%s"/>'
                        '</Property></Properties></ViewProvider>' % (o.Name, is_body, "true" if vis else "false"))
        xml = ('<?xml version="1.0" encoding="utf-8"?>\n<Document SchemaVersion="1">\n'
               '<ViewProviderData Count="%d">\n%s\n</ViewProviderData>\n<Camera settings=""/>\n</Document>\n'
               % (len(rows), "\n".join(rows)))
        with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
            if "GuiDocument.xml" not in z.namelist():
                z.writestr("GuiDocument.xml", xml)

