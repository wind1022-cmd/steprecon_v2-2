"""Give the sketches of a reconstruction geometric constraints and dimensions until they are fully constrained.

The reconstruction tools only tie endpoints together. Opened in FreeCAD such a sketch is white (free): nothing shows
its sizes and dragging deforms it. This pass adds, in a designer-like order,
    horizontal / vertical lines -> tangent joints -> diameters and radii (equal sizes tied together) ->
    positions of the straight sides -> positions of circle centres ->
    line lengths -> positions of the remaining points,
each one only if it removes a degree of freedom without conflict, until the sketch has none left. Values are taken
from the geometry as it is, so the shape does not change; if it does, or the solver objects, the sketch is restored.

No model is involved: this is plain rules, run after the shape has been matched."""
import math

import Sketcher

TOL = 1e-7
ORIGIN = (-1, 1)        # the sketch origin as (GeoId, PosId)
H_AXIS, V_AXIS = -1, -2


def _dof(sk):
    """Degrees of freedom left, or None if the solver reports a problem."""
    return sk.DoF if sk.solve() == 0 else None


def _points(sk):
    """Key points of every element, to detect that a sketch moved."""
    out = []
    for g in sk.Geometry:
        t = type(g).__name__
        if t == "LineSegment":
            out += [g.StartPoint, g.EndPoint]
        elif t == "Circle":
            out += [g.Center, g.Center + g.XAxis * g.Radius]
        elif t == "ArcOfCircle":
            out += [g.Center, g.StartPoint, g.EndPoint]
    return [(p.x, p.y) for p in out]


def _moved(a, b, tol=1e-6):
    return len(a) != len(b) or any(abs(p[0] - q[0]) > tol or abs(p[1] - q[1]) > tol for p, q in zip(a, b))


class _Pass:
    def __init__(self, sk):
        self.sk = sk
        self.added = {"geometric": 0, "dimensions": 0}

    def attempt(self, kind, *args):
        """Add one constraint; keep it only if it removes freedom and the solver stays happy."""
        sk = self.sk
        before = _dof(sk)
        if not before:          # None (broken) or 0 (done)
            return False
        idx = sk.addConstraint(Sketcher.Constraint(*args))
        after = _dof(sk)
        if after is None or after >= before:
            sk.delConstraint(idx)
            sk.solve()
            return False
        self.added[kind] += 1
        return True

    def coordinate(self, geo, pos, value, axis):
        """Pin one coordinate of a point: on the axis if it is zero, otherwise a distance from the origin."""
        if abs(value) < 1e-9:
            return self.attempt("geometric", "PointOnObject", geo, pos, V_AXIS if axis == "x" else H_AXIS)
        name = "DistanceX" if axis == "x" else "DistanceY"
        if value > 0:
            return self.attempt("dimensions", name, ORIGIN[0], ORIGIN[1], geo, pos, value)
        return self.attempt("dimensions", name, geo, pos, ORIGIN[0], ORIGIN[1], -value)

    def tangent_joints(self):
        """Where a line runs smoothly into an arc (or arc into arc), turn the plain joint into a tangent one."""
        sk = self.sk
        geo = sk.Geometry

        def direction(i, pos):
            g = geo[i]
            if type(g).__name__ == "LineSegment":
                d = g.EndPoint - g.StartPoint
            elif type(g).__name__ == "ArcOfCircle":
                p = g.StartPoint if pos == 1 else g.EndPoint
                r = p - g.Center
                d = type(r)(-r.y, r.x, 0)           # tangent is square to the radius
            else:
                return None
            return d.normalize() if d.Length > 1e-12 else None

        joints = [(c.First, c.FirstPos, c.Second, c.SecondPos) for c in sk.Constraints
                  if c.Type == "Coincident" and c.First >= 0 and c.Second >= 0
                  and "ArcOfCircle" in (type(geo[c.First]).__name__, type(geo[c.Second]).__name__)]
        for j in joints:
            d1, d2 = direction(j[0], j[1]), direction(j[2], j[3])
            if d1 is None or d2 is None or abs(d1.x * d2.y - d1.y * d2.x) > 1e-6:
                continue
            before = _dof(sk)
            if not before:
                return
            idx = next((k for k, c in enumerate(sk.Constraints) if c.Type == "Coincident"
                        and (c.First, c.FirstPos, c.Second, c.SecondPos) == j), None)
            if idx is None:
                continue
            sk.delConstraint(idx)
            new = sk.addConstraint(Sketcher.Constraint("Tangent", *j))
            after = _dof(sk)
            if after is None or after >= before:        # no gain: put the plain joint back
                sk.delConstraint(new)
                sk.addConstraint(Sketcher.Constraint("Coincident", *j))
                sk.solve()
            else:
                self.added["geometric"] += 1

    def run(self):
        sk = self.sk
        geo = sk.Geometry
        kinds = [type(g).__name__ for g in geo]
        lines = [i for i, k in enumerate(kinds) if k == "LineSegment"]
        circles = [i for i, k in enumerate(kinds) if k == "Circle"]
        arcs = [i for i, k in enumerate(kinds) if k == "ArcOfCircle"]
        for i in lines:
            d = geo[i].EndPoint - geo[i].StartPoint
            if abs(d.y) < TOL < abs(d.x):
                self.attempt("geometric", "Horizontal", i)
            elif abs(d.x) < TOL < abs(d.y):
                self.attempt("geometric", "Vertical", i)
        self.tangent_joints()
        # one dimension per size: further circles / arcs of the same size are tied to the first with "equal",
        # so changing that one dimension changes them all
        for group, name, factor in ((circles, "Diameter", 2.0), (arcs, "Radius", 1.0)):
            first = {}
            for i in group:
                key = round(geo[i].Radius, 6)
                if key in first and self.attempt("geometric", "Equal", first[key], i):
                    continue
                if self.attempt("dimensions", name, i, factor * geo[i].Radius):
                    first.setdefault(key, i)
        for i in lines:             # where the straight sides are: this is what a designer dimensions
            d = geo[i].EndPoint - geo[i].StartPoint
            if abs(d.y) < TOL < abs(d.x):
                self.coordinate(i, 1, geo[i].StartPoint.y, "y")
            elif abs(d.x) < TOL < abs(d.y):
                self.coordinate(i, 1, geo[i].StartPoint.x, "x")
        for i in circles + arcs:
            self.coordinate(i, 3, geo[i].Center.x, "x")
            self.coordinate(i, 3, geo[i].Center.y, "y")
        for i in lines:
            self.attempt("dimensions", "Distance", i, (geo[i].EndPoint - geo[i].StartPoint).Length)
        for i in lines + arcs:      # whatever is still loose: pin the end points
            for pos, p in ((1, geo[i].StartPoint), (2, geo[i].EndPoint)):
                self.coordinate(i, pos, p.x, "x")
                self.coordinate(i, pos, p.y, "y")


def constrain_sketch(sk):
    """Constrain one sketch. Returns {"dof_before", "dof_after", "geometric", "dimensions"}; on trouble the sketch is
    left exactly as it was and "restored" says why."""
    info = {"sketch": sk.Name, "dof_before": None, "dof_after": None, "geometric": 0, "dimensions": 0}
    start = _dof(sk)
    info["dof_before"] = info["dof_after"] = start
    if not start:
        return info
    geometry0, constraints0, points0 = sk.Geometry, sk.Constraints, _points(sk)
    job = _Pass(sk)
    problem = ""
    try:
        job.run()
        end = _dof(sk)
        if end is None:
            problem = "solver error"
        elif _moved(points0, _points(sk)):
            problem = "geometry moved"
    except Exception as e:      # never let the dressing-up break a finished reconstruction
        problem = f"{type(e).__name__}: {e}"
    if problem:
        sk.deleteAllConstraints()
        sk.deleteAllGeometry()
        sk.addGeometry(geometry0, False)
        sk.addConstraint(constraints0)
        sk.solve()
        info["restored"] = problem
        return info
    info.update(dof_after=end, **job.added)
    return info


def constrain_document(doc):
    """Constrain every sketch of the document. Returns a summary for reports."""
    per = [constrain_sketch(o) for o in doc.Objects if o.TypeId == "Sketcher::SketchObject"]
    return {"sketches": len(per), "fully_constrained": sum(1 for p in per if p["dof_after"] == 0),
            "geometric": sum(p["geometric"] for p in per), "dimensions": sum(p["dimensions"] for p in per),
            "restored": [p["sketch"] for p in per if "restored" in p], "detail": per}
