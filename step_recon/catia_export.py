"""Replay a reconstructed feature tree in CATIA V5 (written for V5-6R2024 = R34; plain V5 automation).

The reconstruction itself happens in FreeCAD (see agent.py). This module turns the resulting document into a
VBScript that drives a RUNNING CATIA session through its automation interface: for every sketch-based feature it
creates a sketch (lines, arcs, circles) and a Pad or Pocket in a new CATPart, so the result has a real history.

It was written without access to CATIA, so the script trusts as little as possible:
  * sketch position: the script reads back the axis CATIA actually gave the sketch and maps every point into it;
  * extrusion direction / through-all mode: each variant is tried and the body's volume and centre of gravity are
    compared with the values FreeCAD had after the same feature; the best variant is kept;
  * every step is written to a log file, which to_catia.py prints.
Standard library + Windows Script Host only; nothing has to be installed.

Not exported yet (logged as SKIPPED): fillets, chamfers, patterns, mirror, revolution, sweep, loft, shell, draft,
imported freeform regions, and counterbore/countersink shapes of holes (the plain hole is exported)."""
import math

SUPPORTED = ("PartDesign::Pad", "PartDesign::Pocket", "PartDesign::Hole")


def _n(x):
    s = "%.10g" % float(x)
    return "0" if s in ("-0", "0") else s


def _q(s):
    return '"' + str(s).replace('"', '""') + '"'


def _mass(shape):
    """(volume, centre of mass) of a shape that may hold several solids."""
    solids = shape.Solids or [shape]
    vol = sum(s.Volume for s in solids)
    c = [0.0, 0.0, 0.0]
    for s in solids:
        m = s.CenterOfMass
        for i, v in enumerate((m.x, m.y, m.z)):
            c[i] += v * s.Volume / vol
    return vol, c


def _sketch_geometry(sk):
    """Sketch frame (origin, H, V, normal as tuples) and geometry in GLOBAL coordinates:
    ("line", p1, p2) | ("circle", centre, r) | ("arc", centre, r, start, end)  -- arcs counter-clockwise about normal."""
    import FreeCAD as App
    pl = sk.Placement
    o = pl.Base
    h, v, n = (pl.Rotation.multVec(App.Vector(*a)) for a in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))

    def g(p):
        q = pl.multVec(App.Vector(p.x, p.y, 0))
        return (q.x, q.y, q.z)

    out = []
    for geo in sk.Geometry:
        t = type(geo).__name__
        if t == "LineSegment":
            out.append(("line", g(geo.StartPoint), g(geo.EndPoint)))
        elif t == "Circle":
            out.append(("circle", g(geo.Center), geo.Radius))
        elif t == "ArcOfCircle":
            c, r = geo.Center, geo.Radius
            a0, a1 = geo.FirstParameter, geo.LastParameter
            p0 = App.Vector(c.x + r * math.cos(a0), c.y + r * math.sin(a0), 0)
            p1 = App.Vector(c.x + r * math.cos(a1), c.y + r * math.sin(a1), 0)
            out.append(("arc", g(c), r, g(p0), g(p1)))
        elif t == "Point":
            continue
        else:
            raise ValueError(f"sketch {sk.Name} contains a {t}, which is not exported")
    return ((o.x, o.y, o.z), (h.x, h.y, h.z), (v.x, v.y, v.z), (n.x, n.y, n.z)), out


def collect(doc):
    """Feature list of a reconstructed document: [{body, kind, name, frame, geometry, ...expected values}], skipped."""
    feats, skipped = [], []
    bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body"]
    for bi, body in enumerate(bodies):
        for o in body.Group:
            t = o.TypeId
            if t.startswith("Sketcher::") or t.startswith("App::"):
                continue
            if t not in SUPPORTED:
                skipped.append((o.Name, t))
                continue
            sk = o.Profile[0] if isinstance(o.Profile, (tuple, list)) else o.Profile
            if sk is None or sk.TypeId != "Sketcher::SketchObject":
                if not getattr(o, "Suppressed", False):
                    skipped.append((o.Name, "profile taken from a face (movable face), not exported"))
                continue
            try:
                frame, geo = _sketch_geometry(sk)
            except ValueError as e:
                skipped.append((o.Name, str(e)))
                continue
            vol, cog = _mass(o.Shape)
            f = {"body": bi, "name": o.Name, "frame": frame, "geometry": geo, "volume": vol, "cog": cog,
                 "through": False, "symmetric": False, "note": ""}
            if t == "PartDesign::Pad":
                f.update(kind="pad", length=float(o.Length), symmetric=bool(o.Midplane))
            elif t == "PartDesign::Pocket":
                through = str(o.Type) in ("ThroughAll", "1")
                f.update(kind="pocket", through=through, length=float(o.Length))
            else:
                through = str(o.DepthType) == "ThroughAll"
                f.update(kind="pocket", through=through, length=float(o.Depth))
                # the Hole's sketch holds the positions; make sure the circles have the hole diameter
                f["geometry"] = [("circle", g[1], float(o.Diameter) / 2.0) for g in geo if g[0] == "circle"]
                extra = [x for x in (str(getattr(o, "HoleCutType", "None")), str(getattr(o, "DrillPoint", "Flat")))
                         if x not in ("None", "Flat")]
                if extra:
                    f["note"] = "hole exported without its " + "/".join(extra).lower() + " shape"
            feats.append(f)
    return feats, skipped


_HEADER = r'''' Generated by steprecon (step_recon/catia_export.py). Run with CATIA V5 open:
'   cscript //nologo thisfile.catvbs        (or CATIA: Tools > Macro > Macros... > select this file > Run)
Option Explicit
REM_CONSTS
Const TOL = 0.005
Dim CATIA, doc, part, sf, hsf, spa, fso, logf, hb, body
Dim ax(8), nrm(2), lastErr, okCount, badCount, lastScore

Sub Say(s)
    logf.WriteLine s
End Sub

Function Atan2(y, x)
    Const PI = 3.14159265358979
    If Abs(x) < 1E-300 Then
        If y >= 0 Then Atan2 = PI / 2 Else Atan2 = -PI / 2
    ElseIf x > 0 Then
        Atan2 = Atn(y / x)
    ElseIf y >= 0 Then
        Atan2 = Atn(y / x) + PI
    Else
        Atan2 = Atn(y / x) - PI
    End If
End Function

' coordinates of a global point in the axis CATIA actually gave the current sketch
Function SU(x, y, z)
    SU = (x - ax(0)) * ax(3) + (y - ax(1)) * ax(4) + (z - ax(2)) * ax(5)
End Function

Function SV(x, y, z)
    SV = (x - ax(0)) * ax(6) + (y - ax(1)) * ax(7) + (z - ax(2)) * ax(8)
End Function

' +1 if the sketch's own H x V points along the intended normal, -1 if it is mirrored
Function Handed()
    Dim cx, cy, cz
    cx = ax(4) * ax(8) - ax(5) * ax(7)
    cy = ax(5) * ax(6) - ax(3) * ax(8)
    cz = ax(3) * ax(7) - ax(4) * ax(6)
    If cx * nrm(0) + cy * nrm(1) + cz * nrm(2) >= 0 Then Handed = 1 Else Handed = -1
End Function

Function NewSketch(ox, oy, oz, hx, hy, hz, vx, vy, vz, nx, ny, nz)
    Dim p1, p2, p3, pl, sk, want(8), i, got
    Set p1 = hsf.AddNewPointCoord(ox, oy, oz)
    Set p2 = hsf.AddNewPointCoord(ox + 10 * hx, oy + 10 * hy, oz + 10 * hz)
    Set p3 = hsf.AddNewPointCoord(ox + 10 * vx, oy + 10 * vy, oz + 10 * vz)
    hb.AppendHybridShape p1
    hb.AppendHybridShape p2
    hb.AppendHybridShape p3
    Set pl = hsf.AddNewPlane3Points(part.CreateReferenceFromObject(p1), part.CreateReferenceFromObject(p2), _
                                    part.CreateReferenceFromObject(p3))
    hb.AppendHybridShape pl
    part.Update
    Set sk = body.Sketches.Add(part.CreateReferenceFromObject(pl))
    want(0) = ox: want(1) = oy: want(2) = oz
    want(3) = hx: want(4) = hy: want(5) = hz
    want(6) = vx: want(7) = vy: want(8) = vz
    nrm(0) = nx: nrm(1) = ny: nrm(2) = nz
    For i = 0 To 8
        ax(i) = want(i)
    Next
    On Error Resume Next
    sk.SetAbsoluteAxisData want
    Err.Clear
    Dim back(8)
    sk.GetAbsoluteAxisData back
    got = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
    If got Then
        If Abs(back(3)) + Abs(back(4)) + Abs(back(5)) > 0.5 Then
            For i = 0 To 8
                ax(i) = back(i)
            Next
        End If
    Else
        Say "  note: could not read the sketch axis back; assuming the requested one"
    End If
    Set NewSketch = sk
End Function

Sub AddLine(f2, x1, y1, z1, x2, y2, z2)
    f2.CreateLine SU(x1, y1, z1), SV(x1, y1, z1), SU(x2, y2, z2), SV(x2, y2, z2)
End Sub

Sub AddCircle(f2, cx, cy, cz, r)
    f2.CreateClosedCircle SU(cx, cy, cz), SV(cx, cy, cz), r
End Sub

' arc given counter-clockwise about the intended normal, from (sx,sy,sz) to (ex,ey,ez)
Sub AddArc(f2, cx, cy, cz, r, sx, sy, sz, ex, ey, ez)
    Const TWOPI = 6.28318530717959
    Dim u, v, a0, a1, t
    u = SU(cx, cy, cz): v = SV(cx, cy, cz)
    a0 = Atan2(SV(sx, sy, sz) - v, SU(sx, sy, sz) - u)
    a1 = Atan2(SV(ex, ey, ez) - v, SU(ex, ey, ez) - u)
    If Handed() < 0 Then
        t = a0: a0 = a1: a1 = t
    End If
    If a0 < 0 Then a0 = a0 + TWOPI
    If a1 < 0 Then a1 = a1 + TWOPI
    If a1 <= a0 Then a1 = a1 + TWOPI
    f2.CreateCircle u, v, r, a0, a1
End Sub

' how far the active body is from the expected volume (mm3) and centre of gravity (mm); 0 = identical
Function Score(expVol, ex, ey, ez, diag)
    Dim m, v, rv, rc, c(2), k, d, best
    On Error Resume Next
    Err.Clear
    part.Update
    If Err.Number <> 0 Then
        lastErr = Err.Description
        Err.Clear
        Score = 1E+9
        Exit Function
    End If
    Set m = spa.GetMeasurable(part.CreateReferenceFromObject(body))
    v = m.Volume
    If Err.Number <> 0 Then
        lastErr = "cannot measure the body: " & Err.Description
        Err.Clear
        Score = 1E+8
        Exit Function
    End If
    rv = Abs(v - expVol)                                    ' the interface may answer in mm3 or in m3
    If Abs(v * 1E+9 - expVol) < rv Then rv = Abs(v * 1E+9 - expVol)
    rv = rv / expVol
    rc = 0
    m.GetCOG c
    If Err.Number = 0 Then
        best = 1E+30
        For Each k In Array(1, 1000)                        ' ... and in mm or in m
            d = Sqr((c(0) * k - ex) ^ 2 + (c(1) * k - ey) ^ 2 + (c(2) * k - ez) ^ 2)
            If d < best Then best = d
        Next
        rc = best / diag
    End If
    Err.Clear
    lastErr = ""
    Score = rv + rc
End Function

Sub Apply(f, vi, isThrough, big)
    On Error Resume Next
    f.DirectionOrientation = vi Mod 2                  ' 0 regular, 1 inverse
    If isThrough Then
        If vi < 2 Then
            f.FirstLimit.LimitMode = 2                      ' up to last
        Else
            f.FirstLimit.LimitMode = 0                      ' plain depth, long enough to go through
            f.FirstLimit.Dimension.Value = big
        End If
    End If
    Err.Clear
End Sub

' try the variants (direction, through-all mode), keep the one that matches the expected body best
Sub Settle(f, label, isThrough, nVariants, big, expVol, ex, ey, ez, diag)
    Dim i, s, best, bestI, msg
    best = 1E+30: bestI = 0
    For i = 0 To nVariants - 1
        Apply f, i, isThrough, big
        s = Score(expVol, ex, ey, ez, diag)
        If s < best Then
            best = s: bestI = i
        End If
        If s < TOL Then Exit For
    Next
    If bestI <> i And best >= TOL Then
        Apply f, bestI, isThrough, big
        s = Score(expVol, ex, ey, ez, diag)
    End If
    lastScore = best
    msg = label & "  variant=" & bestI & "  mismatch=" & FormatNumber(best * 100, 3) & "%"
    If best < TOL Then
        okCount = okCount + 1
        Say "OK   " & msg
    Else
        badCount = badCount + 1
        If lastErr <> "" Then msg = msg & "  (" & lastErr & ")"
        Say "BAD  " & msg
    End If
End Sub

Sub Main()
    Dim sk, f2, f
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set logf = fso.CreateTextFile(LOGFILE, True, True)
    On Error Resume Next
    Set CATIA = GetObject(, "CATIA.Application")
    If Err.Number <> 0 Or IsEmpty(CATIA) Then
        Err.Clear
        Say "ERROR CATIA is not running (start CATIA V5 first)"
        logf.Close
        WScript.Quit 2
    End If
    Say "START CATIA V5 release " & CATIA.SystemConfiguration.Release
    Err.Clear
    On Error GoTo 0
    CATIA.DisplayFileAlerts = False
    Set doc = CATIA.Documents.Add("Part")
    Set part = doc.Part
    Set sf = part.ShapeFactory
    Set hsf = part.HybridShapeFactory
    Set spa = doc.GetWorkbench("SPAWorkbench")
    Set hb = part.HybridBodies.Add()
    hb.Name = "SketchPlanes"
    Set body = part.MainBody
    okCount = 0: badCount = 0
'''

_FOOTER = r'''
    On Error Resume Next
    part.Update
    Err.Clear
    doc.SaveAs PARTFILE
    If Err.Number <> 0 Then
        Say "note: could not save the CATPart: " & Err.Description
        Err.Clear
    Else
        Say "SAVED " & PARTFILE
    End If
    doc.ExportData STEPFILE, "stp"
    If Err.Number <> 0 Then
        Say "note: could not export STEP (licence?): " & Err.Description
        Err.Clear
    Else
        Say "EXPORTED " & STEPFILE
    End If
    Say "DONE ok=" & okCount & " bad=" & badCount
    logf.Close
End Sub

Main
'''


def build_script(feats, skipped, log_path, part_path, step_path, diag):
    """VBScript text that rebuilds `feats` (from collect) in CATIA."""
    consts = "\n".join((f"Const LOGFILE = {_q(log_path)}", f"Const PARTFILE = {_q(part_path)}",
                        f"Const STEPFILE = {_q(step_path)}"))
    L = [_HEADER.replace("REM_CONSTS", consts)]     # Option Explicit has to stay the first statement

    def a(s):
        L.append("    " + s)

    for name, why in skipped:
        a(f"Say {_q('SKIPPED ' + name + ': ' + why)}")
    cur_body = 0
    big = _n(diag * 4)
    for i, f in enumerate(feats):
        if f["body"] != cur_body:
            cur_body = f["body"]
            a("Set body = part.Bodies.Add()")
            a("part.InWorkObject = body")
        (o, h, v, n) = f["frame"]
        a(f"' ---- {f['name']} ({f['kind']})")
        a("Set sk = NewSketch(" + ", ".join(_n(x) for x in (*o, *h, *v, *n)) + ")")
        a("Set f2 = sk.OpenEdition()")
        for g in f["geometry"]:
            if g[0] == "line":
                a("AddLine f2, " + ", ".join(_n(x) for x in (*g[1], *g[2])))
            elif g[0] == "circle":
                a("AddCircle f2, " + ", ".join(_n(x) for x in (*g[1], g[2])))
            else:
                a("AddArc f2, " + ", ".join(_n(x) for x in (*g[1], g[2], *g[3], *g[4])))
        a("sk.CloseEdition")
        a("part.InWorkObject = sk")
        a("part.Update")
        length = f["length"] / 2.0 if f["symmetric"] else f["length"]
        if f["kind"] == "pad":
            a(f"Set f = sf.AddNewPad(sk, {_n(length)})")
            if f["symmetric"]:
                a("f.IsSymmetric = True")
        else:
            a(f"Set f = sf.AddNewPocket(sk, {_n(length if not f['through'] else diag)})")
        a(f"f.Name = {_q(f['name'])}")
        variants = 1 if f["symmetric"] else (4 if f["through"] else 2)
        a(f"Settle f, {_q(f['name'])}, {'True' if f['through'] else 'False'}, {variants}, {big}, "
          + ", ".join(_n(x) for x in (f["volume"], *f["cog"], diag)))
        if f["note"]:
            a(f"Say {_q('  note: ' + f['note'])}")
    L.append(_FOOTER)
    return "\n".join(L).replace("\n", "\r\n")
