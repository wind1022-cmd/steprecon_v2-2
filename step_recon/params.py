"""The editable numbers of a reconstructed model, by name - the basis for design-of-experiments runs (doe.py).

A parameter is either a feature property (pad length, pocket depth, fillet radius, chamfer size) or a dimension of a
sketch (diameter, radius, distance). Names are built from the feature's label, e.g.
    Hole_D6_x4.Diameter1    Hole_D6_x4.DistanceX1    Pad.Length    Fillet_R3_x4.Radius    Face_1.Thickness
and stay the same as long as the model's tree does."""
import re

import FreeCAD as App

FEATURE_PROPS = {
    "PartDesign::Pad": ("Length",),
    "PartDesign::Pocket": ("Length",),
    "PartDesign::Fillet": ("Radius",),
    "PartDesign::Chamfer": ("Size", "Size2"),
    "PartDesign::Hole": ("Depth",),
    "PartDesign::Revolution": ("Angle",),
    "PartDesign::Groove": ("Angle",),
}
DIMENSIONS = ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle")


_cache = {}
_rim_error = [""]


def _clean(label):
    return re.sub(r"[^A-Za-z0-9_]+", "_", label).strip("_") or "Feature"


def _profile(obj):
    p = getattr(obj, "Profile", None)
    if isinstance(p, (tuple, list)):
        p = p[0] if p else None
    return p


def collect(doc):
    """[{"name", "value", "kind", ...}] in tree order. kind: length | angle."""
    out, seen = [], set()

    def add(name, value, kind, **how):
        base, n = name, 2
        while name in seen:
            name = f"{base}_{n}"
            n += 1
        seen.add(name)
        out.append(dict(name=name, value=float(value), kind=kind, **how))

    owner = {}
    for o in doc.Objects:
        sk = _profile(o)
        if sk is not None and sk.TypeId == "Sketcher::SketchObject":
            owner.setdefault(sk.Name, o)
    movers = {o.SR_Pair for o in doc.Objects if hasattr(o, "SR_Pair")}
    for o in doc.Objects:
        t = o.TypeId
        if hasattr(o, "SR_Pair"):
            # a movable face: one number (the thickness behind it, or the offset if that is unknown) drives a
            # pad for moving out and a pocket for moving in
            cut = doc.getObject(o.SR_Pair)
            rim = hasattr(o, "SR_RimPlane")
            off = 0.0
            if rim and o.SR_Rim:
                off = float(o.SR_Offset)
            elif not o.Suppressed:
                off = float(o.Length)
            elif cut is not None and not cut.Suppressed:
                off = -float(cut.Length)
            t0 = float(o.SR_Thickness)
            label = _clean(o.Label)
            add(f"{label}." + ("Thickness" if t0 > 0 else "Offset"), t0 + off, "length",
                obj=o.Name, pair=o.SR_Pair, t0=t0)
            if rim:     # 1: the chamfers / fillets around the face move with it, 0: they stay where they are
                add(f"{label}.WithRim", 1.0 if o.SR_Rim else 0.0, "flag", obj=o.Name, flag=True)
            continue
        if o.Name in movers:
            continue
        if hasattr(o, "SR_StripData"):      # chamfer strips cut by a tool (sharpen.apply_strips): one size for all
            prop = "Radius" if getattr(o, "SR_Kind", "") == "fillet" else "Size"
            add(f"{_clean(o.Label)}.{prop}", float(o.SR_Size), "length", obj=o.Name, prop="SR_Size")
            continue
        if t in FEATURE_PROPS:
            label = _clean(o.Label)
            for prop in FEATURE_PROPS[t]:
                if not hasattr(o, prop):
                    continue
                if prop == "Size2" and str(getattr(o, "ChamferType", "")) != "Two distances":
                    continue
                if t == "PartDesign::Pocket" and str(o.Type) not in ("Length", "0"):
                    continue        # through-all pockets have no depth to vary
                if t == "PartDesign::Hole" and str(o.DepthType) != "Dimension":
                    continue
                if o.getExpression(prop) if hasattr(o, "getExpression") else None:
                    continue        # driven by something else (e.g. a hole's diameter by its sketch)
                q = getattr(o, prop)
                add(f"{label}.{prop}", q.Value if hasattr(q, "Value") else q,
                    "angle" if prop == "Angle" else "length", obj=o.Name, prop=prop)
        elif t == "Sketcher::SketchObject":
            if o.Name in owner and owner[o.Name].Name in movers:
                continue        # outline of a movable face's pocket: driven by the face parameter only
            label = _clean(owner[o.Name].Label) if o.Name in owner else _clean(o.Label)
            count = {}
            for i, c in enumerate(o.Constraints):
                if c.Type not in DIMENSIONS or not c.Driving:
                    continue
                count[c.Type] = count.get(c.Type, 0) + 1
                value = c.Value
                kind = "length"
                if c.Type == "Angle":
                    import math
                    value, kind = math.degrees(c.Value), "angle"
                add(f"{label}.{c.Type}{count[c.Type]}", value, kind, obj=o.Name, constraint=i)
    return out


def apply(doc, params, values):
    """Set parameters by name ({name: number}; lengths in mm, angles in degrees). Unknown names raise KeyError."""
    by_name = {p["name"]: p for p in params}
    # the "with rim" switches first: how a face is moved depends on them
    for name, value in sorted(values.items(), key=lambda kv: 0 if by_name.get(kv[0], {}).get("flag") else 1):
        if name not in by_name:
            raise KeyError(f"unknown parameter {name!r}")
        p = by_name[name]
        obj = doc.getObject(p["obj"])
        unit = "deg" if p["kind"] == "angle" else "mm"
        if p.get("flag"):
            obj.SR_Rim = float(value) > 0.5
        elif "pair" in p:
            cut = doc.getObject(p["pair"])
            if p["t0"] > 0 and float(value) <= 0:
                raise ValueError(f"{name} must be larger than 0")
            off = float(value) - p["t0"]
            if hasattr(obj, "SR_RimPlane") and obj.SR_Rim:
                obj.Suppressed, cut.Suppressed = True, True
                obj.SR_Offset = off if abs(off) > 1e-9 else 0.0
                continue
            if hasattr(obj, "SR_RimPlane"):
                obj.SR_Offset = 0.0
            obj.Suppressed, cut.Suppressed = not off > 1e-9, not off < -1e-9
            if off > 1e-9:
                obj.Length = off
            elif off < -1e-9:
                cut.Length = -off
        elif "constraint" in p:
            obj.setDatum(p["constraint"], App.Units.Quantity(f"{float(value)!r} {unit}"))
        else:
            setattr(obj, p["prop"], float(value))


def _with_rim(shape, moves):
    """The tip shape with the faces that carry their rim along moved (stretch.py). None if one cannot be moved."""
    from . import stretch
    key = (shape.hashCode(), round(shape.Volume, 6), tuple(
        (o.Name, round(float(o.SR_Offset), 9), round(float(o.SR_RimPlane), 9)) for o in moves))
    if key in _cache:
        out, _rim_error[0] = _cache[key]
        return out
    out = shape
    _rim_error[0] = ""
    for o in moves:
        nrm = o.SR_RimNormal
        out, info = stretch.stretch(out, nrm, float(o.SR_RimPlane), float(o.SR_Offset))
        if out is None:
            _rim_error[0] = f"{_clean(o.Label)}: {info['error']}"
            break
    if len(_cache) > 8:
        _cache.clear()
    _cache[key] = (out, _rim_error[0])
    return out


_plans = {}


def with_strips(doc, shape, body=None):
    """`shape` (the tip of a body) with the chamfer strips cut that are kept as tool cuts, at their current size.
    None (and the reason in problems()) if one cannot be cut at that size."""
    objs = [o for o in doc.Objects if hasattr(o, "SR_StripData") and (body is None or o.SR_Body == body)]
    if not objs:
        return shape
    import json
    from . import sharpen
    key = ("strips", doc.Name, shape.hashCode(), round(shape.Volume, 6),
           tuple((o.Name, round(float(o.SR_Size), 9)) for o in objs))
    if key in _cache:
        out, _rim_error[0] = _cache[key]
        return out
    out = shape
    for o in objs:
        tag = (doc.Name, o.Name)
        if _plans.get(tag, (None,))[0] != hash(o.SR_StripData):
            _plans[tag] = (hash(o.SR_StripData), json.loads(o.SR_StripData))
        size, size0 = float(o.SR_Size), float(o.SR_Size0)
        new = sharpen.apply_strips(out, _plans[tag][1], size / size0) if size > 0 and size0 > 0 else None
        if new is None:
            _rim_error[0] = f"{_clean(o.Label)}: a chamfer of {size:g} cannot be cut here"
            out = None
            break
        out = new
    if len(_cache) > 8:
        _cache.clear()
    _cache[key] = (out, _rim_error[0])
    return out


def result_shape(doc):
    """The solid(s) the document describes: the tip of every body, with the chamfer strips cut that are kept as tool
    cuts and with the faces moved whose rim goes along."""
    import Part
    shapes = []
    _rim_error[0] = ""
    for b in doc.Objects:
        if b.TypeId == "PartDesign::Body":
            t = b.Tip if b.Tip is not None else b.BaseFeature
            if t is not None and not t.Shape.isNull():
                shape = with_strips(doc, t.Shape, b.Name)
                if shape is None:
                    return None
                inside = {x.Name for x in b.Group}
                moves = [o for o in doc.Objects if hasattr(o, "SR_RimPlane") and o.Name in inside and o.SR_Rim
                         and abs(float(o.SR_Offset)) > 1e-9]
                if moves:
                    shape = _with_rim(shape, moves)
                    if shape is None:
                        return None
                shapes.append(shape)
    if not shapes:
        return None
    return shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)


def problems(doc):
    """Why the document is not a good model right now ([] = fine)."""
    bad = []
    for o in doc.Objects:
        if o.TypeId.startswith("PartDesign::") or o.TypeId == "Sketcher::SketchObject":
            if not o.isValid():
                bad.append(f"{o.Label}: {getattr(o, 'getStatusString', lambda: 'invalid')()}")
    s = result_shape(doc)
    if _rim_error[0]:
        bad.append(_rim_error[0])
    elif s is None or s.isNull():
        bad.append("no solid")
    elif not s.isValid() or s.Volume <= 0:
        bad.append("the solid is not valid")
    return bad
