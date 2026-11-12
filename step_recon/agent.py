"""LLM tool-use loop that rebuilds a STEP/IGES solid as a FreeCAD PartDesign feature tree.

The model is pluggable (see llm.py): Claude API or any OpenAI-compatible server (Ollama, LM Studio, vLLM...)."""
import json
import os
import time

from . import analyze, fc_tools, llm

SYSTEM = """You are a CAD reverse-engineering agent. You receive a geometry summary of a dumb solid (no history)
and must rebuild the SAME shape in FreeCAD PartDesign using the provided tools, producing a clean, editable
feature tree the way a human designer would (base pad -> pockets/bosses -> holes -> fillets/chamfers last).

Conventions (global coordinates in mm):
- plane XY: sketch (x,y) = global (X,Y); pad goes +Z. offset = Z position of the sketch plane.
- plane XZ: sketch (x,y) = global (X,Z); the sketch normal is -Y, so pad/pocket go toward -Y unless reversed=true.
  offset moves the plane along its normal (i.e. -Y).
- plane YZ: sketch (x,y) = global (Y,Z); normal is +X. offset = X position.
- Pocket cuts opposite to the sketch normal (into material below the sketch plane); the tool auto-flips once if
  nothing was removed. For a pocket from a top face at Z=h, use plane XY with offset=h.
- Geometry items: {"type":"rect","p1":[x,y],"p2":[x,y]}, {"type":"circle","c":[x,y],"r":r},
  {"type":"line","p1":..,"p2":..}, {"type":"arc","c":[x,y],"r":r,"a0":deg,"a1":deg} (counter-clockwise).
  Profiles must be closed; use only dimensions that are plausible design values (snap to 0.5 mm etc).
- Edge indices for fillet/chamfer come from list_edges (1-based) of the CURRENT tip; they change after every feature.
  Fillets and chamfers, last: call list_edges with only_missing=true. It lists the sharp edges of your solid that the
  reference does not have and, for each, "suggest": {"fillet": radius} or {"chamfer": size} measured from the
  reference. Apply exactly those (one call per group of edges with the same suggestion), then list again.

- Outlines from the reference (PREFERRED for any outline that is not a plain rectangle or circle - do not retype
  coordinates): every Plane face lists "outline" (edge kinds of its outer boundary), "holes" (number of inner loops)
  and "max_depth" (how much of the part lies behind it). pad with face=<id> and length extrudes the exact outer
  outline of that reference face into the material; holes=true keeps the face's inner loops as openings. pocket with
  face=<id> cuts the inner loops of that face into the material (loops="inner", default), or with loops="outer" removes
  the face's own outline outward (use the floor face of a recess with its depth). No plane/geometry is needed then.
  Typical plate or flange: pad face=<largest Plane face> length=<thickness>, then pockets, holes, chamfers.

Rotational parts: if cylinders/cones share one axis, build a revolved profile (revolution) instead of stacked pads.
Repeated holes/pockets at regular spacing or on a circle -> build ONE and use linear_pattern / polar_pattern;
symmetric features about a global plane -> mirror. Patterns/mirror reference features by index (see features tool;
sketch-based pad/pocket/hole/revolution each count as one feature, fillets too). Tilted faces -> pass a plane object.
Molded / thin-walled parts: if the part is a hollow shell (large planar faces with a parallel offset face at a constant
wall thickness, open side), build the OUTER solid first and then use shell with the opening face(s). Side walls that are
slightly inclined (about 0.5-5 deg from the pull direction) are drafted: build straight, then apply draft (neutral face =
the parting face); do draft BEFORE shell and fillets. Thin plate-like webs inside a shell are ribs. Order for molded
parts: outer body -> draft -> shell -> ribs/bosses -> holes -> fillets last.
Freeform surfaces: the summary field "freeform" (null if none) lists non-analytic faces (BSplineSurface etc.) with their
bounding box and a "clusters" list (connected groups of such faces, each with its own bounding box). Do NOT try to fake
them with sketches. Call import_freeform FIRST (a frozen base region around those faces),
then rebuild everything else (plate, bosses, holes, fillets on analytic edges) with normal features and use compare to
check. The imported region is not parametric - say so in your final message; keep it as small as possible.
Multiple bodies: if the summary has n_solids > 1, the part consists of separate solids that do not touch. "solids" lists
each one (volume, bounding box, face ids) and every face carries its "solid" index. Use ONE body per solid: build the
first solid in the initial body, then call new_body and build the next one there, and so on. Every tool acts on the
active body only (edge/face indices, pocket, fillet, patterns); select_body switches back to an earlier body. compare
always checks all bodies together against the whole reference, so until every solid is built it reports the unbuilt ones
as missing_regions. Do not let bodies overlap or touch.
Holes: a concave cylinder with a larger coaxial concave cylinder at its entry is a counterbore, with a concave Cone at
its entry a countersink, with a Cone at its blind end a drill point - make each as ONE hole call with those options.
Several equal holes that do not form a regular pattern -> one hole call with positions.
Swept shapes: Toroid faces whose minor radius equals the radius of adjoining cylinders (same tube cross-section running
through straight and bent stretches) are a pipe/handle/bent bar -> sweep the cross-section along the centre line (lines +
tangent arcs; a Toroid's major_radius is the bend radius). Constant-section channels that follow a bent line -> sweep
with cut=true. Sections that change size or shape between two parallel planes (Cone faces that are not chamfers,
tapered sides that are not a constant draft) -> loft with ruled=true.
Workflow: study the summary (planes with normal/offset, cylinders with concave=true are holes, convex cylinders
or Toroid/Cone faces near edges hint at fillets/chamfers). Build step by step. Every tool result that changes the
shape carries "state": xor_pct (difference to the target, 0 = identical), volume vs target_volume, bbox vs target_bbox
([xmin,ymin,zmin,xmax,ymax,zmax]), faces vs target_faces and missing_regions / extra_regions bounding boxes showing
what is still wrong - read it instead of calling compare. First make bbox equal target_bbox (right plane, position and
direction), then fix details. Use undo to retract a wrong feature. The run ends by itself when state.converged is
true; call finish only to give up. Always answer with a tool call, never with prose. Be economical with tool calls."""

# Short prompt for small local models: drop the advanced sections (revolve/pattern/mold/freeform guidance).
SYSTEM_BASIC = SYSTEM[:SYSTEM.index("Rotational parts:")] + SYSTEM[SYSTEM.index("Workflow:"):]

# Tool subsets. "basic" keeps small models focused (fewer, simpler tools); "full" exposes everything.
BASIC_TOOLS = ("pad", "pocket", "hole", "fillet", "chamfer", "list_edges", "compare", "undo", "finish")
PROFILES = {"full": None, "basic": BASIC_TOOLS}
# tools that do not change the shape (no progress report is attached to their results)
STATE_BUDGET_S = 30     # see state() in run()
READ_ONLY = ("compare", "list_edges", "list_faces", "features", "select_body", "new_body", "finish")

PLANE = {"description": "XY / XZ / YZ, or an object {origin:[x,y,z], normal:[x,y,z], x_dir:[x,y,z]} "
         "for an arbitrary plane in global coordinates (sketch x axis = x_dir)",
         "anyOf": [{"type": "string", "enum": ["XY", "XZ", "YZ"]}, {"type": "object"}]}
FEAT = {"type": "integer", "description": "0-based index from the features tool"}

FACES = {"type": "array", "items": {"type": "integer"}, "description": "1-based face indices from list_faces"}

TOOLS = [
    {"name": "import_freeform", "description": "HYBRID step for parts with freeform (B-spline etc.) faces that cannot be "
     "sketched: freezes the part of the reference inside a box around all freeform faces as the base feature. Must be the "
     "FIRST step (use undo to retract). Afterwards rebuild the remaining analytic geometry with pad/pocket/etc. and "
     "use compare (missing_regions) to see what is left. By default one box per connected cluster of freeform faces "
     "(split=true) so far-apart analytic regions stay parametric; the result reports imported_fraction_pct and warns "
     "when >90% of the part is frozen. Use `clusters` (indices into freeform.clusters, largest area first) to freeze "
     "only some clusters, e.g. when a small 'freeform' patch is really just a blend you can model with fillets.",
     "input_schema": {"type": "object", "properties": {
         "margin": {"type": "number", "description": "box margin in mm, default 0.5"},
         "split": {"type": "boolean", "description": "one box per cluster (default true) or a single box around all"},
         "clusters": {"type": "array", "items": {"type": "integer"}}}}},
    {"name": "import_region", "description": "Like import_freeform but with an explicit axis-aligned box (min/max [x,y,z]); "
     "use when you want a tighter or larger frozen region. Must be the FIRST step.",
     "input_schema": {"type": "object", "properties": {
         "min": {"type": "array", "items": {"type": "number"}}, "max": {"type": "array", "items": {"type": "number"}}},
         "required": ["min", "max"]}},
    {"name": "list_faces", "description": "List faces (1-based index, type, area, a point on the face, outward normal for planes) "
     "of the current result. Face indices change after every feature.",
     "input_schema": {"type": "object", "properties": {
         "only_type": {"type": "string", "description": "e.g. Plane or Cylinder"}, "min_area": {"type": "number"}}}},
    {"name": "shell", "description": "Hollow out the solid: faces = opening faces to remove, thickness = wall thickness "
     "(inward=true keeps the outer shape, walls grow inward). Typical for molded/injection parts: do it AFTER the outer shape, "
     "before ribs/small fillets.",
     "input_schema": {"type": "object", "properties": {
         "faces": FACES, "thickness": {"type": "number"}, "inward": {"type": "boolean"},
         "join": {"type": "string", "enum": ["arc", "intersection"]}}, "required": ["faces", "thickness"]}},
    {"name": "draft", "description": "Taper faces by an angle (degrees) around the hinge with the neutral face; pull_axis is the "
     "mold pull direction (global X/Y/Z). Use reversed=true if the taper goes the wrong way.",
     "input_schema": {"type": "object", "properties": {
         "faces": FACES, "angle": {"type": "number"}, "neutral_face": {"type": "integer"},
         "pull_axis": {"type": "string", "enum": ["X", "Y", "Z"]}, "reversed": {"type": "boolean"}},
         "required": ["faces", "angle", "neutral_face"]}},
    {"name": "rib", "description": "Thin straight web: rectangle of width `thickness` along the line p1->p2 in the sketch plane, "
     "extruded `length` along the plane normal (midplane/reversed supported), fused with the body. Make p1->p2 span wall to wall. "
     "For a triangular gusset between a floor and a wall use pad instead: closed triangle on the rib's mid plane, "
     "length = rib thickness, midplane=true.",
     "input_schema": {"type": "object", "properties": {
         "plane": PLANE, "offset": {"type": "number"}, "p1": {"type": "array", "items": {"type": "number"}},
         "p2": {"type": "array", "items": {"type": "number"}}, "thickness": {"type": "number"},
         "length": {"type": "number"}, "reversed": {"type": "boolean"}, "midplane": {"type": "boolean"}},
         "required": ["plane", "p1", "p2", "thickness", "length"]}},
    {"name": "revolution", "description": "Revolve a closed profile (cut=true makes a groove) around the sketch's "
     "V (vertical) or H (horizontal) axis. On plane XZ the V axis is global Z.",
     "input_schema": {"type": "object", "properties": {
         "plane": PLANE, "offset": {"type": "number"}, "geometry": {"type": "array", "items": {"type": "object"}},
         "axis": {"type": "string", "enum": ["V", "H"]}, "angle": {"type": "number"},
         "cut": {"type": "boolean"}, "reversed": {"type": "boolean"}},
         "required": ["plane", "geometry"]}},
    {"name": "sweep", "description": "Sweep a closed profile along a planar path made of lines and arcs (pipes, handles, "
     "bent bars; cut=true for grooves/channels). plane/offset/geometry describe the profile exactly like pad; "
     "path_plane/path_offset/path_geometry describe the path as one connected open chain (use tangent arcs for bends). "
     "The profile must sit at an end point of the path, in the plane perpendicular to the path direction there.",
     "input_schema": {"type": "object", "properties": {
         "plane": PLANE, "offset": {"type": "number"}, "geometry": {"type": "array", "items": {"type": "object"}},
         "path_plane": PLANE, "path_offset": {"type": "number"},
         "path_geometry": {"type": "array", "items": {"type": "object"}},
         "cut": {"type": "boolean"},
         "transition": {"type": "string", "enum": ["transformed", "right", "round"],
                        "description": "corner treatment where path segments are not tangent (default transformed)"}},
         "required": ["plane", "geometry", "path_plane", "path_geometry"]}},
    {"name": "loft", "description": "Loft through 2 or more closed section profiles on different planes, listed in order "
     "(tapered or transitional shapes that pad+draft cannot make; cut=true removes material). ruled=true joins "
     "neighbouring sections with straight lines (circle to circle = cone, polygon to polygon = flat sides); "
     "ruled=false makes one smooth skin through all sections (a freeform surface when there are 3+ sections).",
     "input_schema": {"type": "object", "properties": {
         "sections": {"type": "array", "items": {"type": "object", "properties": {
             "plane": PLANE, "offset": {"type": "number"}, "geometry": {"type": "array", "items": {"type": "object"}}},
             "required": ["plane", "geometry"]}},
         "ruled": {"type": "boolean"}, "closed": {"type": "boolean"}, "cut": {"type": "boolean"}},
         "required": ["sections"]}},
    {"name": "linear_pattern", "description": "Repeat a feature along global X/Y/Z; length = distance from first to last instance.",
     "input_schema": {"type": "object", "properties": {
         "feature_index": FEAT, "direction": {"type": "string", "enum": ["X", "Y", "Z"]},
         "length": {"type": "number"}, "occurrences": {"type": "integer"}},
         "required": ["feature_index", "direction", "length", "occurrences"]}},
    {"name": "polar_pattern", "description": "Repeat a feature around global axis X/Y/Z (angle 360 = full circle).",
     "input_schema": {"type": "object", "properties": {
         "feature_index": FEAT, "axis": {"type": "string", "enum": ["X", "Y", "Z"]},
         "occurrences": {"type": "integer"}, "angle": {"type": "number"}},
         "required": ["feature_index", "axis", "occurrences"]}},
    {"name": "mirror", "description": "Mirror a feature across a global plane through the origin.",
     "input_schema": {"type": "object", "properties": {
         "feature_index": FEAT, "plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]}},
         "required": ["feature_index", "plane"]}},
    {"name": "new_body", "description": "Start a new, separate body and make it active. Use it when the reference has "
     "n_solids > 1: one body per disjoint solid. The current body must already contain a solid. All later tools act on "
     "the new body; compare still checks all bodies together. undo removes the new body again while it is empty.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "select_body", "description": "Make an earlier body the active one again (0-based index in creation order; "
     "the features tool lists the bodies). Returns volume/face count/bounding box of every body.",
     "input_schema": {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"]}},
    {"name": "features", "description": "List the feature tree built so far with indices (for patterns/mirror); with "
     "several bodies it also shows which body each feature is in and which body is active.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "pad", "description": "Extrude a closed profile (adds material). Either face=<id of a Plane face of the "
     "reference> to reuse its exact outline (extruded into the material), or plane + geometry to draw it.",
     "input_schema": {"type": "object", "properties": {
         "face": {"type": "integer", "description": "reference Plane face id whose outline is the profile"},
         "holes": {"type": "boolean", "description": "with face: also keep the face's inner loops as openings"},
         "plane": PLANE,
         "offset": {"type": "number", "description": "plane offset along its normal"},
         "geometry": {"type": "array", "items": {"type": "object"}},
         "length": {"type": "number"},
         "reversed": {"type": "boolean"}, "midplane": {"type": "boolean"}},
         "required": ["length"]}},
    {"name": "pocket", "description": "Cut a closed profile into the solid. Either face=<id of a Plane face of the "
     "reference> (its inner loops, or with loops=outer its own outline cut outward), or plane + geometry.",
     "input_schema": {"type": "object", "properties": {
         "face": {"type": "integer", "description": "reference Plane face id"},
         "loops": {"type": "string", "enum": ["inner", "outer", "all"]},
         "plane": PLANE,
         "offset": {"type": "number"},
         "geometry": {"type": "array", "items": {"type": "object"}},
         "depth": {"type": "number"}, "through_all": {"type": "boolean"}, "reversed": {"type": "boolean"}}}},
    {"name": "hole", "description": "Round hole at sketch position (x,y); give depth or through_all. Optional: "
     "positions for several equal holes in one feature, a counterbore (diameter + depth) or countersink (diameter + "
     "angle) at the entry, drill_angle for a conical bottom (depth is then the cylindrical part).",
     "input_schema": {"type": "object", "properties": {
         "plane": PLANE,
         "x": {"type": "number"}, "y": {"type": "number"}, "diameter": {"type": "number"},
         "depth": {"type": "number"}, "offset": {"type": "number"},
         "through_all": {"type": "boolean"}, "reversed": {"type": "boolean"},
         "positions": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                       "description": "[[x,y],...] more holes of the same size (x,y may then be omitted)"},
         "counterbore_diameter": {"type": "number"}, "counterbore_depth": {"type": "number"},
         "countersink_diameter": {"type": "number"},
         "countersink_angle": {"type": "number", "description": "full cone angle in degrees, default 90"},
         "drill_angle": {"type": "number", "description": "drill point angle in degrees, e.g. 118 (blind holes)"}},
         "required": ["plane", "diameter"]}},
    {"name": "fillet", "description": "Fillet edges of the current tip (indices from list_edges).",
     "input_schema": {"type": "object", "properties": {
         "edges": {"type": "array", "items": {"type": "integer"}}, "radius": {"type": "number"}},
         "required": ["edges", "radius"]}},
    {"name": "chamfer", "description": "Chamfer edges of the current tip (indices from list_edges).",
     "input_schema": {"type": "object", "properties": {
         "edges": {"type": "array", "items": {"type": "integer"}}, "size": {"type": "number"}},
         "required": ["edges", "size"]}},
    {"name": "list_edges", "description": "List edges (1-based index, type, length, endpoints) of the current result; "
     "in_reference tells whether the reference has the edge, suggest which fillet/chamfer it has there instead.",
     "input_schema": {"type": "object", "properties": {
         "only_type": {"type": "string", "description": "e.g. Line or Circle"},
         "min_length": {"type": "number"},
         "only_missing": {"type": "boolean", "description": "only edges the reference does not have, each with the "
                          "fillet radius or chamfer size the reference has there"}}}},
    {"name": "compare", "description": "Compare current result with the reference: volume diff, XOR %, "
     "missing/extra regions, face type histograms, converged flag.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "undo", "description": "Remove the last step (feature and its sketch, or an empty new body).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "finish", "description": "Save the FreeCAD document and end. Call when converged or out of ideas.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _compact_summary(a, max_faces=80):
    """Drop adjacency and trim face list so the prompt stays small."""
    s = {k: v for k, v in a.items() if k not in ("adjacency", "faces")}
    faces = a["faces"]
    # when the list must be cut, keep the largest faces (they carry the main outlines), still listed by id
    keep = sorted(sorted(faces, key=lambda f: -f.get("area", 0))[:max_faces], key=lambda f: f["id"])
    s["faces"] = keep
    if len(faces) > max_faces:
        s["faces_truncated"] = len(faces) - max_faces
    return s


def select_tools(profile):
    names = PROFILES[profile]
    return TOOLS if names is None else [t for t in TOOLS if t["name"] in names]


def run(step_path, out_dir, max_turns=40, client=None, backend=None, profile="full", max_faces=80, verbose=True):
    """Reconstruct one file. `backend` is an llm backend instance (default: from STEPRECON_BACKEND env, i.e. Claude);
    `client` is accepted for backwards compatibility (an Anthropic-style client object)."""
    os.makedirs(out_dir, exist_ok=True)
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {sorted(PROFILES)}")
    summary = analyze.analyze(step_path)
    ref = analyze.load_shape(step_path)
    sess = fc_tools.Session(ref)
    if backend is None:
        backend = llm.AnthropicBackend(client=client) if client is not None else llm.make_backend()
    tools = select_tools(profile)
    allowed = {t["name"] for t in tools}
    base = os.path.splitext(os.path.basename(step_path))[0]
    out_path = os.path.join(out_dir, base + ".FCStd")

    system = SYSTEM if profile == "full" else SYSTEM_BASIC
    user = "Rebuild this solid. Geometry summary:\n" + json.dumps(_compact_summary(summary, max_faces))
    backend.start(system, tools, user)
    est_tokens = int((len(system) + len(user) + len(json.dumps(tools))) / 3.2)    # rough: ~3.2 chars per token
    result = {"file": step_path, "turns": 0, "converged": False, "backend": backend.name,
              "model": getattr(backend, "model", ""), "profile": profile, "prompt_tokens_est": est_tokens}
    if verbose and backend.name == "openai":
        print(f"prompt is about {est_tokens} tokens before any tool results; make sure the server's context window is "
              f"larger (Ollama: set OLLAMA_CONTEXT_LENGTH=16384 or more, otherwise the start of the prompt is "
              f"silently cut off)")
    finished = False
    nudges = 0
    finish_refused = False

    slow = []

    def state():
        # comparing after every step is cheap on ordinary parts but can take minutes on huge freeform ones;
        # after one slow comparison stop doing it automatically (the model can still call compare)
        if slow:
            return None
        t0 = time.time()
        try:
            return sess.state()
        except Exception:
            return None
        finally:
            if time.time() - t0 > STATE_BUDGET_S:
                slow.append(1)
                if verbose:
                    print(f"(comparison took {time.time() - t0:.0f}s; automatic state reports are off for this part)")

    for turn in range(max_turns):
        t_model = time.time()
        reply = backend.complete()
        t_model = time.time() - t_model
        result["turns"] = turn + 1
        if verbose and t_model > 60:
            print(f"[{turn}] (model answered after {t_model:.0f}s)")
        if not reply.calls:
            # small models sometimes answer in prose or with nothing at all; nudge them back to the protocol
            # (up to 3 times in a row) before giving up
            if nudges < 3 and turn < max_turns - 1:
                nudges += 1
                st = state()
                backend.add_user("No tool call received. "
                                 + (f"Current state: {json.dumps(st)}. " if st else "Nothing is built yet. ")
                                 + "Answer with exactly one tool call now.")
                if verbose:
                    info = getattr(backend, "last_info", "")
                    print(f"[{turn}] (no tool call; nudging) {reply.text[:120]!r} {info}")
                continue
            break
        nudges = 0
        results = []
        converged = False
        for c in reply.calls:
            try:
                if c.error:
                    out = {"ok": False, "error": c.error}
                elif c.name not in allowed:
                    out = {"ok": False, "error": f"unknown tool {c.name!r}; available: {sorted(allowed)}"}
                elif c.name == "finish":
                    st = state()
                    if st and not st["converged"] and not finish_refused and turn < max_turns - 1:
                        finish_refused = True       # weak models give up early; push back once
                        out = {"ok": False, "error": "not converged yet: read state, undo what is wrong and keep "
                               "fixing. Call finish again only if you really give up.", "state": st}
                    else:
                        out = sess.save(out_path)
                        finished = True
                else:
                    out = getattr(sess, c.name)(**c.args)
                    if c.name not in READ_ONLY and isinstance(out, dict) and out.get("ok"):
                        st = state()
                        if st:
                            out["state"] = st
                            converged = st["converged"]
            except Exception as e:  # report tool errors back so the model can recover
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if verbose:
                print(f"[{turn}] {c.name} -> {json.dumps(out)[:300]}")
            results.append((c, json.dumps(out)))
        backend.add_results(results)
        if converged and not finished:      # nothing left to do: do not spend another (slow) model turn on finish
            sess.save(out_path)
            finished = True
            if verbose:
                print(f"[{turn}] converged -> saved")
        if finished:
            break

    if not finished:
        sess.save(out_path)
    result["sketches"] = getattr(sess, "sketch_report", None)     # constrained and dimensioned on save
    final = sess.compare()
    result.update(converged=bool(final.get("converged")), xor_pct=final.get("xor_pct"),
                  features=sess.log, fcstd=out_path)
    with open(os.path.join(out_dir, base + ".recon.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    try:    # full conversation, handy for debugging weak local models
        with open(os.path.join(out_dir, base + ".transcript.json"), "w", encoding="utf-8") as f:
            json.dump(backend.transcript(), f, indent=1, default=str)
    except Exception:
        pass
    return result
