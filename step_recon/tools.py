"""What an AI assistant may do with the part that is open in STEP Studio.

The assistant never makes geometry itself. It calls these tools; the engine (studio.Studio) rebuilds the solid,
checks it, and takes a change back if the solid cannot be built. The same list serves the MCP server (mcp.py) and
the chat panel of the page.

    TOOLS                       tool definitions ({name, description, input_schema})
    call(studio, name, args)    -> (text for the model, is_error)

Texts for the model are English; the lines written to the activity list of the page (studio.note) are Korean like
the rest of the page."""
import difflib
import os
import random
import re
import time

from . import params as P

MAX_RUNS = 2000


class ToolError(Exception):
    """Something the model can correct: the message goes back to it as the tool's result."""


def _num(schema_desc):
    return {"type": "number", "description": schema_desc}


TOOLS = [
    {"name": "open_part",
     "description": "Open a STEP or IGES file and list what could become a dimension (candidates: holes by "
                    "diameter, fillets by radius, chamfers by size, movable flat faces). Quick; nothing is built yet: "
                    "call make_dimensions with what the user wants to vary. Replaces the part that was open.",
     "input_schema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "Full path of a .stp/.step/.igs/.iges file on this computer"}}}},
    {"name": "make_dimensions",
     "description": "Turn the chosen candidates into features with named parameters; only those are computed, the "
                    "rest of the part stays as it is in the file (always checked to equal the file). Choose what the "
                    "user wants to vary - a group of equal fillets is one choice. Replaces earlier dimensions and "
                    "their changes. A few seconds to a minute per group; everything=true can take very long.",
     "input_schema": {"type": "object", "properties": {
         "groups": {"type": "array", "items": {"type": "string"},
                    "description": "Group ids from the candidate list, e.g. [\"g2\", \"g5\"] (all members)"},
         "items": {"type": "array", "items": {"type": "string"},
                   "description": "Single candidates: a hole (h3), one fillet/chamfer face (b17), a flat face (p20)"},
         "everything": {"type": "boolean", "description": "All candidates, flat faces chosen automatically (slow)"}}}},
    {"name": "get_part",
     "description": "Describe the open part: size, volume, and every feature with its number, place and parameters "
                    "(exact parameter names and current values). Call this before changing anything.",
     "input_schema": {"type": "object", "properties": {
         "category": {"type": "string", "enum": ["hole", "fillet", "chamfer", "face", "other"],
                      "description": "Only features of this kind"},
         "feature": {"type": "integer", "description": "Only the feature with this number"},
         "density_g_cm3": _num("If given, the mass is reported too (steel 7.85, aluminium 2.70)"),
         "candidates": {"type": "boolean", "description": "List the candidates (what could become a dimension) "
                                                          "instead of the made dimensions"}}}},
    {"name": "set_values",
     "description": "Change parameters and rebuild the solid. Lengths in mm, angles in degrees. Earlier changes "
                    "stay unless replace=true. If the solid cannot be built with the new values, nothing is changed "
                    "and the reason is returned.",
     "input_schema": {"type": "object", "required": ["values"], "properties": {
         "values": {"type": "object", "additionalProperties": {"type": "number"},
                    "description": 'Parameter name -> new value, e.g. {"Hole_D8_x4.Diameter1": 10}'},
         "replace": {"type": "boolean", "description": "true: all other parameters go back to the original values"}}}},
    {"name": "reset_values",
     "description": "Put every parameter back to the value of the original file.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "find_limits",
     "description": "Find how far one parameter can go down and up from its present value before the solid can no "
                    "longer be built (other parameters stay as they are now). Use it before proposing a range for a "
                    "DOE, or to explain why a value failed.",
     "input_schema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "Exact parameter name"},
         "low": _num("Lowest value worth trying (default: far below the present value)"),
         "high": _num("Highest value worth trying (default: far above the present value)")}}},
    {"name": "plan_doe",
     "description": "Prepare a design of experiments: which parameters vary over which values. Nothing is built yet; "
                    "the table of runs is returned so that the user can confirm it. Parameters that are changed now "
                    "and are not factors keep their changed value in every run.",
     "input_schema": {"type": "object", "required": ["factors"], "properties": {
         "factors": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["name"], "properties": {
             "name": {"type": "string", "description": "Exact parameter name"},
             "values": {"type": "array", "items": {"type": "number"}, "description": "The levels, or instead:"},
             "min": {"type": "number"}, "max": {"type": "number"},
             "levels": {"type": "integer", "description": "How many evenly spaced values from min to max (default 3)"}}}},
         "design": {"type": "string", "enum": ["full", "one_at_a_time", "latin"],
                    "description": "full = every combination (default); one_at_a_time = the present part plus each "
                                   "factor changed alone; latin = `runs` space-filling points between min and max"},
         "runs": {"type": "integer", "description": "Number of runs for design=latin"}}}},
    {"name": "run_doe",
     "description": "Build the variants of the plan made with plan_doe: one STEP file per run plus results.csv. "
                    "Call it only after the user agreed to the plan. Works for a limited time per call; if it "
                    "reports that runs remain, call it again to continue.",
     "input_schema": {"type": "object", "properties": {
         "max_seconds": _num("Time budget of this call (default 45)")}}},
    {"name": "doe_results",
     "description": "Results of the last DOE: which runs were built, volume per run, and how strongly each factor "
                    "moved the volume.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "show_feature",
     "description": "Point at a feature in the user's 3D view (it is highlighted and scrolled into view). 0 clears it.",
     "input_schema": {"type": "object", "required": ["feature"], "properties": {
         "feature": {"type": "integer", "description": "Feature number from get_part"}}}},
    {"name": "label_features",
     "description": "Give features a short meaningful name in the user's language (e.g. what the hole pattern is "
                    "for). Shown in the feature list; parameter names do not change.",
     "input_schema": {"type": "object", "required": ["labels"], "properties": {
         "labels": {"type": "array", "items": {"type": "object", "required": ["feature", "name"], "properties": {
             "feature": {"type": "integer"}, "name": {"type": "string", "description": "Empty removes the name"}}}}}}},
    {"name": "export_step",
     "description": "Save the solid as it is now (with the current values) as a STEP file in the work folder.",
     "input_schema": {"type": "object", "properties": {
         "file_name": {"type": "string", "description": "Name without folder (default: <part>_edit.step)"}}}},
]
NAMES = [t["name"] for t in TOOLS]


def g(v):
    return f"{float(v):.6g}"


def _need(st, dims=True):
    if st.ref is None and st.doc is None:
        raise ToolError("No part is open. Call open_part with the path of a STEP file, or ask the user to open one "
                        "in the STEP Studio window.")
    if dims and st.doc is None:
        raise ToolError("No dimensions have been made yet. Call get_part to see the candidates, then make_dimensions "
                        "with the groups or items the user wants to vary.")


KIND_EN = {"hole": "hole", "fillet": "fillet", "chamfer": "chamfer", "face": "flat face"}


def _candidates(st):
    c, chosen = st.cands, set(st.selection)
    out = [f"Candidates of {st.info['name']} ({len(st.ref.Faces)} faces). Groups (equal size together):"]
    for g in c["groups"]:
        n = len(g["items"])
        size = f"D{g['size']:g}" if g["kind"] == "hole" else f"R{g['size']:g}" if g["kind"] == "fillet" else \
            (f"{g['size']:g} x {g['size2']:g} mm (two distances)" if g.get("size2") else f"{g['size']:g} mm")
        made = sum(1 for i in g["items"] if i in chosen)
        out.append(f"  {g['id']}: {g['kind']} {size}, {n} {'holes' if g['kind'] == 'hole' else 'faces'}"
                   + (f"  [dimension made for {made}]" if made else "")
                   + (f"  [WARNING: {g['rim_unknown']} unrecognised rim face(s) will not follow the diameter]"
                      if g.get("rim_unknown") else "") + "   items " + ", ".join(g["items"][:8])
                   + (" ..." if n > 8 else ""))
    planes = [it for it in c["items"] if it["kind"] == "face"]
    if planes:
        out.append(f"Movable flat faces ({len(planes)}; one item each, largest first):")
        for it in sorted(planes, key=lambda x: -x["size"])[:25]:
            out.append(f"  {it['id']}: facing {it['side'] or 'oblique'}, area {it['size']:g} mm2, centre "
                       f"({', '.join(f'{v:g}' for v in it['at'])})" + ("  [dimension made]" if it["id"] in chosen else ""))
        if len(planes) > 25:
            out.append(f"  ... {len(planes) - 25} smaller ones")
    if not c["items"]:
        out.append("  none: no round holes, constant fillets / chamfers or movable flat faces were found")
    return "\n".join(out)


def _name(st, key):
    """The exact parameter name for what the model wrote."""
    if key in st.base_values:
        return key
    low = {k.lower(): k for k in st.base_values}
    if str(key).lower() in low:
        return low[str(key).lower()]
    near = difflib.get_close_matches(str(key), list(st.base_values), n=4, cutoff=0.5)
    raise ToolError(f"There is no parameter {key!r}. " + ("Did you mean: " + ", ".join(near) + "?" if near else
                                                         "Call get_part for the exact names."))


def _unit(st, name):
    return "deg" if next(p for p in st.plist if p["name"] == name)["kind"] == "angle" else "mm"


def _current(st):
    return {p["name"]: p["value"] for p in P.collect(st.doc)}


def _measure(st):
    shape = P.result_shape(st.doc)
    return st._measure(shape) if shape is not None else None


def _vol_line(st, m):
    dv = (m["volume"] - st.ref_volume) / st.ref_volume * 100 if st.ref_volume else 0.0
    return (f"volume {g(m['volume'])} mm3 ({dv:+.2f}% against the original {g(st.ref_volume)}), "
            f"size {g(m['size_x'])} x {g(m['size_y'])} x {g(m['size_z'])} mm, surface {g(m['area'])} mm2")


def _apply(st, values):
    """Set exactly these changes (everything else at the original value). Returns the problems ([] = built)."""
    try:
        return st.set_values(values)
    except Exception as e:
        return [f"{type(e).__name__}: {e}"]


# ------------------------------------------------------------------ tools
def open_part(st, path):
    path = os.path.expanduser(str(path).strip().strip('"'))
    if not os.path.isfile(path):
        raise ToolError(f"File not found: {path}")
    if os.path.splitext(path)[1].lower() not in (".stp", ".step", ".igs", ".iges"):
        raise ToolError("Only STEP (.stp, .step) and IGES (.igs, .iges) files can be opened.")
    st.worker.busy = "open"
    st.open(path)
    i = st.info
    st.note(f"파일 열기: {i['name']} (후보 {len(st.cands['items'])}개, {i['scan_seconds']}초)")
    return _candidates(st) + "\nNothing is built yet. Ask the user what to vary if unclear, then call make_dimensions."


def make_dimensions(st, groups=None, items=None, everything=False):
    _need(st, dims=False)
    by_g = {g["id"]: g for g in st.cands["groups"]}
    known = {it["id"] for it in st.cands["items"]}
    pick = []
    for g in groups or []:
        if g not in by_g:
            raise ToolError(f"There is no group {g!r}; groups are " + ", ".join(by_g) + ".")
        pick += by_g[g]["items"]
    for i in items or []:
        if i not in known:
            raise ToolError(f"There is no candidate {i!r}. Call get_part with candidates=true for the ids.")
        pick.append(i)
    if not pick and not everything:
        raise ToolError("Choose groups and/or items (or everything=true).")
    st.worker.busy = "make"
    st.make(pick, bool(everything))
    i = st.info
    st.note(f"치수 만들기: {'전부 자동' if everything else f'후보 {len(set(pick))}개'} → 피처 {len(st.features)}개 ({i['seconds']}초)")
    extra = f"\nAlso made, because they sit on the rim of a chosen hole: {i['auto_added']} fillet/chamfer faces." \
        if i.get("auto_added") else ""
    for w in i.get("warnings", []):
        extra += "\nWARNING (tell the user): " + w["text"]
    return get_part(st) + extra


def get_part(st, category=None, feature=None, density_g_cm3=None, candidates=False):
    _need(st, dims=False)
    if candidates or st.doc is None:
        return _candidates(st) + ("" if st.doc is not None else "\nNo dimensions made yet: call make_dimensions.")
    s = st.state()
    i, m = s["info"], s["measure"]
    out = [f"Part: {i['name']}  (rebuilt solid identical to the file: {'yes' if i['identical'] else 'NO, differs by ' + str(i['xor_pct']) + '%'})"]
    if m:
        out.append(_vol_line(st, m) + f", centre of gravity ({g(m['cog_x'])}, {g(m['cog_y'])}, {g(m['cog_z'])})")
        if density_g_cm3:
            out.append(f"mass {g(m['volume'] / 1000.0 * float(density_g_cm3))} g at {g(density_g_cm3)} g/cm3 "
                       f"(original {g(st.ref_volume / 1000.0 * float(density_g_cm3))} g)")
    if s["problems"]:
        out.append("PROBLEM, the solid is not valid now: " + "; ".join(s["problems"])[:300])
    ch = st.changed()
    out.append("Changed now: " + (", ".join(f"{k} = {g(v)} (original {g(st.base_values[k])})" for k, v in ch.items())
                                  if ch else "nothing, all parameters have the original values"))
    if i["skipped"]:
        out.append(f"{len(i['skipped'])} shapes could not be made into features and stay part of the fixed blank.")
    feats = [f for f in s["features"] if (not category or f["category"] == category)
             and (feature is None or f["n"] == int(feature))]
    counts = {}
    for f in s["features"]:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    out.append(f"Features: {len(s['features'])} (" + ", ".join(f"{v} {k}" for k, v in counts.items()) + ")"
               + (f", {len(feats)} shown" if len(feats) != len(s["features"]) else ""))
    hints = {"Thickness": "distance from this face to the opposite side of the material; larger moves the face outward",
             "Offset": "how far the face is moved outward (negative: inward)"}
    for f in feats:
        what = {"hole": f"{f['count']} hole(s)" if f["count"] > 1 else "hole",
                "fillet": f"fillet on {f['count']} edge(s)", "chamfer": f"chamfer on {f['count']} edge(s)",
                "face": ("machined land" if f["kind"] == "land" else "flat face") + (f" looking {f['side']}" if f["side"] else ""),
                "other": f["type"]}[f["category"]]
        where = "" if not f["position"] else " at (" + ", ".join(g(v) for v in f["position"]) + ")"
        alias = f' named "{f["alias"]}"' if f.get("alias") else ""
        out.append(f"#{f['n']} {f['category']}: {what}{where}{alias}")
        for p in f["params"]:
            line = f"    {p['name']} = {g(p['value'])} {p['unit']}"
            if abs(p["value"] - p["base"]) > 1e-9:
                line += f"  (original {g(p['base'])})"
            hint = hints.get(re.sub(r"\d+$", "", p["short"]))
            out.append(line + (f"  [{hint}]" if hint else ""))
    return "\n".join(out)


def set_values(st, values, replace=False):
    _need(st)
    if not isinstance(values, dict) or not values:
        raise ToolError('values must be an object like {"Hole_D8_x4.Diameter1": 10}.')
    new = {}
    for k, v in values.items():
        try:
            new[_name(st, k)] = float(v)
        except (TypeError, ValueError):
            raise ToolError(f"The value of {k} must be a number, got {v!r}.")
    before = st.changed()
    m0 = _measure(st)
    want = dict(new) if replace else {**before, **new}
    want = {k: v for k, v in want.items() if abs(v - st.base_values[k]) > 1e-9}
    bad = _apply(st, want)
    told = ", ".join(f"{k} {g(before.get(k, st.base_values[k]))} → {g(v)}" for k, v in new.items())
    if bad:
        _apply(st, before)
        st.note(f"치수 변경 실패 (되돌림): {told}", "warn")
        return ("NOT changed. The solid cannot be built with " + ", ".join(f"{k} = {g(v)}" for k, v in new.items())
                + ": " + "; ".join(bad)[:300] + "\nThe part is as it was before. find_limits tells how far a "
                "parameter can go."), True
    m1 = _measure(st)
    st.note(f"치수 변경: {told}")
    out = ["Changed and rebuilt: " + ", ".join(f"{k} {g(before.get(k, st.base_values[k]))} -> {g(v)} {_unit(st, k)}"
                                               for k, v in new.items()), "Now: " + _vol_line(st, m1)]
    if m0 and abs(m1["volume"] - m0["volume"]) < 1e-6 * max(m0["volume"], 1.0) and before != want:
        out.append("Note: the volume did not change at all - check that this value really has an effect.")
    elif m0:
        out.append(f"Volume changed by {g(m1['volume'] - m0['volume'])} mm3 with this step.")
    return "\n".join(out)


def reset_values(st):
    _need(st)
    n = len(st.changed())
    bad = _apply(st, {})
    st.note("모든 치수를 원래 값으로 되돌림")
    if bad:
        return "Reset, but the solid reports: " + "; ".join(bad)[:300], True
    return f"{n} parameter(s) put back. " + _vol_line(st, _measure(st))


def find_limits(st, name, low=None, high=None):
    _need(st)
    name = _name(st, name)
    keep = st.changed()
    here = _current(st)[name]
    base = st.base_values[name]
    short = name.split(".", 1)[-1]
    positive = not short.startswith(("DistanceX", "DistanceY", "Offset", "Angle"))
    m = _measure(st)
    span = max(abs(here), abs(base), 0.25 * max(m["size_x"], m["size_y"], m["size_z"]) if m else 1.0, 1e-3)
    lo = float(low) if low is not None else (max(here * 0.05, 1e-3) if positive else here - span)
    hi = float(high) if high is not None else (here * 5 if positive and here > 0 else here + span)
    if not lo < here < hi:
        raise ToolError(f"low < present value ({g(here)}) < high is needed; got low {g(lo)}, high {g(hi)}.")
    tries = [0]

    def ok(v):
        tries[0] += 1
        return not _apply(st, {**keep, name: v})

    quiet, st.quiet = st.quiet, True
    try:
        if not ok(here):
            raise ToolError("The solid is not valid at the present values; reset_values first.")
        ends = []
        for end in (lo, hi):
            if ok(end):
                ends.append((end, True))
                continue
            good, bad = here, end
            for _ in range(8):
                mid = (good + bad) / 2
                if ok(mid):
                    good = mid
                else:
                    bad = mid
            ends.append((good, False))
    finally:
        _apply(st, keep)
        st.quiet = quiet
    u = _unit(st, name)
    (a, a_open), (b, b_open) = ends
    st.note(f"가능 범위 확인: {name} {g(a)} ~ {g(b)} {u}")
    out = [f"{name} (now {g(here)} {u}, original {g(base)} {u}) can be built from {g(a)} to {g(b)} {u}.",
           f"  lower end: " + (f"{g(a)} was the lowest value tried and still builds" if a_open
                               else f"fails below about {g(a)}"),
           f"  upper end: " + (f"{g(b)} was the highest value tried and still builds" if b_open
                               else f"fails above about {g(b)}"),
           f"Checked with {tries[0]} rebuilds; other parameters as they are now. \"Can be built\" only means that "
           "a valid solid results - whether the value makes sense for the design is for the user to decide. "
           "The part is unchanged."]
    return "\n".join(out)


def _levels(st, f):
    name = _name(st, f.get("name", ""))
    if f.get("values"):
        vals = [float(v) for v in f["values"]]
    elif f.get("min") is not None and f.get("max") is not None:
        n = max(2, min(50, int(f.get("levels") or 3)))
        a, b = float(f["min"]), float(f["max"])
        vals = [float(f"{a + (b - a) * i / (n - 1):.10g}") for i in range(n)]
    else:
        raise ToolError(f"Factor {name} needs either values, or min and max.")
    seen = []
    for v in vals:
        if not any(abs(v - s) < 1e-12 for s in seen):
            seen.append(v)
    return name, seen


def plan_doe(st, factors, design="full", runs=None):
    _need(st)
    if not isinstance(factors, list) or not factors:
        raise ToolError("factors must be a list with at least one {name, values} or {name, min, max, levels}.")
    fs = [_levels(st, f) for f in factors]
    names = [n for n, _ in fs]
    if len(set(names)) != len(names):
        raise ToolError("Each parameter can be a factor only once.")
    design = design or "full"
    here = _current(st)
    if design == "full":
        rows = [[]]
        for _, vals in fs:
            rows = [r + [v] for r in rows for v in vals]
            if len(rows) > MAX_RUNS:
                raise ToolError(f"More than {MAX_RUNS} runs. Use fewer levels, fewer factors, or design=latin.")
    elif design == "one_at_a_time":
        centre = [here[n] for n in names]
        rows = [centre]
        for k, (_, vals) in enumerate(fs):
            rows += [centre[:k] + [v] + centre[k + 1:] for v in vals if abs(v - centre[k]) > 1e-12]
    elif design == "latin":
        n = int(runs or 0)
        if not 2 <= n <= MAX_RUNS:
            raise ToolError(f"design=latin needs runs between 2 and {MAX_RUNS}.")
        rnd = random.Random(1)          # the same plan every time
        cols = []
        for _, vals in fs:
            a, b = min(vals), max(vals)
            order = list(range(n))
            rnd.shuffle(order)
            cols.append([float(f"{a + (b - a) * (i + 0.5) / n:.5g}") for i in order])
        rows = [list(r) for r in zip(*cols)]
    else:
        raise ToolError("design must be full, one_at_a_time or latin.")
    fixed = {k: v for k, v in st.changed().items() if k not in names}
    st.plan = {"names": names, "rows": rows, "fixed": fixed, "design": design, "next": 0, "started": False,
               "finished": False, "summary": None}
    st.note(f"실험 계획: 변수 {len(names)}개, {len(rows)}개 조합 ({design}) · 아직 실행 전")
    out = [f"DOE plan ({design}): {len(rows)} runs, {len(names)} factor(s). Nothing has been built yet."]
    for n, vals in fs:
        out.append(f"  {n} (now {g(here[n])} {_unit(st, n)}): " + (", ".join(g(v) for v in vals) if design != "latin"
                                                                    else f"{g(min(vals))} ... {g(max(vals))}"))
    if fixed:
        out.append("Kept in every run: " + ", ".join(f"{k} = {g(v)}" for k, v in fixed.items()))
    out.append("run | " + " | ".join(names))
    for i, r in enumerate(rows[:40], 1):
        out.append(f"{i} | " + " | ".join(g(v) for v in r))
    if len(rows) > 40:
        out.append(f"... and {len(rows) - 40} more")
    out.append("Show this plan to the user and call run_doe when they agree.")
    return "\n".join(out)


def _summary(st):
    plan, rows = st.plan, st.rows
    good = [r for r in rows if r["ok"]]
    out = [f"DOE: {len(good)} of {len(rows)} variants built" + ("" if plan["finished"] else
                                                                 f" so far ({len(plan['rows']) - plan['next']} runs remain)") + ".",
           f"Folder: {st.run_dir}  (run_NNN.step per variant, results.csv" + (f", {plan['zip']}" if plan.get("zip") else "") + ")"]
    for r in [r for r in rows if not r["ok"]][:10]:
        out.append(f"  run {r['run']} failed (" + ", ".join(f"{n} = {g(r[n])}" for n in plan["names"]) + f"): {r['error'][:120]}")
    if good:
        lo, hi = min(good, key=lambda r: r["volume"]), max(good, key=lambda r: r["volume"])
        out.append(f"Volume: smallest {g(lo['volume'])} mm3 (run {lo['run']}), largest {g(hi['volume'])} mm3 "
                   f"(run {hi['run']}), original part {g(st.ref_volume)} mm3.")
        effects = []
        for n in plan["names"]:
            by = {}
            for r in good:
                by.setdefault(round(r[n], 9), []).append(r["volume"])
            if len(by) < 2 or len(by) > 12:
                continue
            means = {k: sum(v) / len(v) for k, v in sorted(by.items())}
            effects.append((max(means.values()) - min(means.values()), n, means))
        if effects:
            out.append("Effect of each factor on the volume (mean volume of the built runs at each level, strongest first):")
            for rng, n, means in sorted(effects, reverse=True):
                out.append(f"  {n}: " + ", ".join(f"{g(k)} -> {g(v)}" for k, v in means.items()) + f"   (spread {g(rng)} mm3)")
        if len(good) <= 40:
            out.append("run | " + " | ".join(plan["names"]) + " | volume mm3 | surface mm2")
            for r in good:
                out.append(f"{r['run']} | " + " | ".join(g(r[n]) for n in plan["names"]) + f" | {g(r['volume'])} | {g(r['area'])}")
    return "\n".join(out)


def run_doe(st, max_seconds=45):
    _need(st)
    plan = st.plan
    if not plan:
        raise ToolError("There is no plan. Call plan_doe first and show the plan to the user.")
    if plan["finished"]:
        return "This plan has already been run.\n" + _summary(st)
    t0 = time.time()
    budget = max(5.0, float(max_seconds or 45))
    if not plan["started"]:
        plan["before"] = st.changed()     # also the present values of the factors themselves
        st.doe_start(plan["names"])
        plan["started"] = True
        st.note(f"DOE 실행 시작: {len(plan['rows'])}개 조합")
    total = len(plan["rows"])
    while plan["next"] < total and time.time() - t0 < budget:
        k = plan["next"]
        st.worker.busy = f"DOE {k + 1}/{total}"
        st.doe_variant(k + 1, {**plan["fixed"], **dict(zip(plan["names"], plan["rows"][k]))})
        plan["next"] = k + 1
    if plan["next"] < total:
        st.note(f"DOE 진행: {plan['next']} / {total}")
        return (f"{plan['next']} of {total} runs done in this call's time; {total - plan['next']} remain. "
                "Call run_doe again to continue.")
    fin = st.doe_finish()           # leaves the part at its original values
    plan["finished"], plan["zip"] = True, fin["zip"]
    bad = _apply(st, plan["before"])
    st.note(f"DOE 완료: {fin['ok']} / {fin['total']}개 변형의 STEP 파일 생성")
    return _summary(st) + ("\nThe part on screen is back at the values it had before the DOE." if not bad else "")


def doe_results(st):
    _need(st)
    if not st.plan or not st.plan["started"]:
        raise ToolError("No DOE has been run yet (plan_doe, then run_doe).")
    return _summary(st)


def show_feature(st, feature):
    _need(st)
    n = int(feature)
    f = next((f for f in st.features if f["n"] == n), None)
    if n and f is None:
        raise ToolError(f"There is no feature #{n}; the part has #1 to #{len(st.features)}.")
    st.highlight = n
    if n:
        st.note(f"화면에 표시: {n}번 {f['label']}")
    return f"Feature #{n} ({f['label']}) is highlighted in the user's 3D view." if n else "Highlight cleared."


def label_features(st, labels):
    _need(st)
    if not isinstance(labels, list) or not labels:
        raise ToolError('labels must be a list like [{"feature": 1, "name": "..."}].')
    known = {f["n"] for f in st.features}
    done = []
    for item in labels:
        n = int(item.get("feature", 0))
        if n not in known:
            raise ToolError(f"There is no feature #{n}.")
        name = " ".join(re.sub(r"[<>&\"]+", " ", str(item.get("name", ""))).split())[:60]
        if name:
            st.aliases[n] = name
        else:
            st.aliases.pop(n, None)
        done.append(f"#{n} = {name or '(removed)'}")
    st.note(f"피처 {len(done)}개에 이름을 붙임")
    return "Named: " + ", ".join(done)


def export_step(st, file_name=None):
    _need(st)
    bad = P.problems(st.doc)
    if bad:
        raise ToolError("The solid is not valid now: " + "; ".join(bad)[:200])
    name = os.path.basename(str(file_name or f"{st.base}_edit.step"))
    name = re.sub(r"[^A-Za-z0-9_.\-가-힣]+", "_", name)
    if not name.lower().endswith((".step", ".stp")):
        name += ".step"
    path = os.path.join(st.dir, name)
    P.result_shape(st.doc).exportStep(path)
    st.note(f"STEP 저장: {name}")
    ch = st.changed()
    return f"Saved {path}\nwith " + (", ".join(f"{k} = {g(v)}" for k, v in ch.items()) if ch else "the original values") + "."


_FUNCS = {f.__name__: f for f in (open_part, make_dimensions, get_part, set_values, reset_values, find_limits, plan_doe, run_doe,
                                  doe_results, show_feature, label_features, export_step)}


def call(studio, name, args):
    """Run one tool. Returns (text, is_error); never raises for something the model did wrong."""
    fn = _FUNCS.get(name)
    if fn is None:
        return f"Unknown tool {name!r}. Tools: {', '.join(NAMES)}", True
    args = args if isinstance(args, dict) else {}
    props = next(t for t in TOOLS if t["name"] == name)["input_schema"].get("properties", {})
    extra = [k for k in args if k not in props]
    if extra:
        return f"{name} has no argument {', '.join(extra)}. Arguments: {', '.join(props) or 'none'}", True
    try:
        out = fn(studio, **args)
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        return f"Wrong arguments for {name}: {e}", True
    except Exception as e:
        return f"{name} failed: {type(e).__name__}: {e}", True
    finally:
        studio.worker.busy = ""
    return out if isinstance(out, tuple) else (out, False)
