"""CATIA V5 replay macro (to_catia.py). CATIA itself is not available to the tests, so this checks what can be
checked without it: the features and expected values collected from a reconstruction, the generated macro text, and
- on Windows - that the macro compiles and stops cleanly when CATIA is not running."""
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import catia_export, fc_tools  # noqa: E402
import to_catia  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


tmp = tempfile.mkdtemp(prefix="steprecon_catia_")

# a tilted plate with a rounded outline, a through hole and a blind pocket, rebuilt with the usual tools
ref = Part.makeBox(40, 30, 6)
ref = ref.makeFillet(5, [e for e in ref.Edges if abs(e.valueAt(e.FirstParameter).z - e.valueAt(e.LastParameter).z) > 5])
ref = ref.cut(Part.makeCylinder(4, 6, V(10, 10, 0))).cut(Part.makeBox(10, 8, 2, V(22, 12, 4)))
ref.Placement = App.Placement(V(5, -3, 20), App.Rotation(V(1, 2, 0), 25))
path = os.path.join(tmp, "plate.step")
ref.exportStep(path)
from step_recon import analyze  # noqa: E402
ref = analyze.load_shape(path)
n = App.Rotation(V(1, 2, 0), 25).multVec(V(0, 0, 1))


def plane_faces(normal):
    out = []
    for i, f in enumerate(ref.Faces):
        if type(f.Surface).__name__ == "Plane":
            pt, u, v = analyze._face_point(f)
            if f.normalAt(u, v).getAngle(normal) < 1e-6:
                out.append((f.Area, i))
    return sorted(out)


tops = plane_faces(n)
s = fc_tools.Session(ref)
s.pad(face=tops[-1][1], length=6)                       # outline with four arcs
s.pocket(face=plane_faces(n * -1)[-1][1], through_all=True)     # the hole (only inner loop of the bottom face)
s.pocket(face=tops[0][1], loops="outer", depth=2)       # blind pocket from its floor
assert s.state()["converged"], s.state()
fcstd = os.path.join(tmp, "plate.FCStd")
s.save(fcstd)

doc = App.openDocument(fcstd)
feats, skipped = catia_export.collect(doc)
App.closeDocument(doc.Name)
report("three features collected, none skipped", [f["kind"] for f in feats] == ["pad", "pocket", "pocket"] and not skipped)
pad, hole, pocket = feats
kinds = [g[0] for g in pad["geometry"]]
report("pad outline: 4 lines + 4 arcs in global coordinates", kinds.count("line") == 4 and kinds.count("arc") == 4)
on_plane = all(abs(sum(a * b for a, b in zip(p, (n.x, n.y, n.z))) - tops[-1][0] * 0 - pad["frame"][0][0] * n.x
                   - pad["frame"][0][1] * n.y - pad["frame"][0][2] * n.z) < 1e-6
               for g in pad["geometry"] if g[0] == "line" for p in (g[1], g[2]))
report("outline points lie in the sketch plane", on_plane)
report("expected volumes decrease feature by feature", pad["volume"] > hole["volume"] > pocket["volume"]
       and abs(pocket["volume"] - ref.Volume) < 1e-3 * ref.Volume)
report("through / blind flags", hole["through"] and not pocket["through"] and abs(pocket["length"] - 2) < 1e-9)

script, info = to_catia.export(fcstd)
with io.open(script, encoding="utf-16", newline="") as f:
    text = f.read()
report("macro text", text.count("Set sk = NewSketch(") == 3 and text.count("sf.AddNewPad(") == 1
       and text.count("sf.AddNewPocket(") == 2 and text.count("AddArc f2,") == 4 and "\r\n" in text
       and info["features"] == 3 and info["part"].endswith("plate.CATPart"))
report("no decimal commas or non-ASCII in the macro body", all(ord(c) < 128 for c in text))

# unsupported features are reported, not silently dropped
s2 = fc_tools.Session(Part.makeBox(20, 10, 5))
s2.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [20, 10]}], 5)
s2.fillet([1], 1.0)
p2 = os.path.join(tmp, "f.FCStd")
s2.save(p2)
doc = App.openDocument(p2)
feats2, skipped2 = catia_export.collect(doc)
App.closeDocument(doc.Name)
report("fillet is listed as skipped", len(feats2) == 1 and len(skipped2) == 1 and "Fillet" in skipped2[0][1])

# Windows: the macro must compile; without CATIA it logs that and exits with code 2
if os.name == "nt" and shutil.which("cscript"):
    r = subprocess.run(["cscript", "//nologo", "//E:vbscript", script], capture_output=True, text=True)
    log = io.open(info["log"], encoding="utf-16").read() if os.path.exists(info["log"]) else ""
    if r.returncode == 2:
        report("macro compiles and stops cleanly without CATIA", "CATIA is not running" in log, log.strip()[:60])
    else:       # a compile error ends with code 1 and a message; code 0 means CATIA really was there
        report("macro compiles", "compilation error" not in (r.stdout + r.stderr) and r.returncode == 0,
               (r.stdout + r.stderr).strip()[:200])
else:
    print("SKIP  cscript not available")

# ---- run the macro against a stand-in for CATIA (VBScript classes): checks that the macro's own code runs through
# and that sketch points are mapped correctly into whatever axis the sketch really has (here: shifted and mirrored)
MOCK = r"""
Dim mockf
Class Fake
    Private want(8)
    Public Default Property Get Value
        Value = 0
    End Property
    Private Sub W(s)
        If IsEmpty(mockf) Then Set mockf = CreateObject("Scripting.FileSystemObject").CreateTextFile(MOCKLOG, True, False)
        mockf.WriteLine s
    End Sub
    Public Property Let DisplayFileAlerts(x): End Property
    Public Property Get SystemConfiguration: Set SystemConfiguration = New Fake: End Property
    Public Property Get Release: Release = 34: End Property
    Public Property Get Documents: Set Documents = New FakeAdd1: End Property
    Public Function Add(): Set Add = New Fake: End Function
    Public Property Get Part: Set Part = New Fake: End Property
    Public Function GetWorkbench(x): Set GetWorkbench = New Fake: End Function
    Public Sub SaveAs(p): End Sub
    Public Sub ExportData(p, t): End Sub
    Public Property Get ShapeFactory: Set ShapeFactory = New Fake: End Property
    Public Property Get HybridShapeFactory: Set HybridShapeFactory = New Fake: End Property
    Public Property Get HybridBodies: Set HybridBodies = New Fake: End Property
    Public Property Get MainBody: Set MainBody = New Fake: End Property
    Public Property Get Bodies: Set Bodies = New Fake: End Property
    Public Property Get Sketches: Set Sketches = New FakeAdd1: End Property
    Public Property Let InWorkObject(x): End Property
    Public Property Let Name(x): End Property
    Public Property Let IsSymmetric(x): End Property
    Public Property Let DirectionOrientation(x): W "ORIENT " & x: End Property
    Public Property Let LimitMode(x): End Property
    Public Property Get FirstLimit: Set FirstLimit = New Fake: End Property
    Public Property Get Dimension: Set Dimension = New Fake: End Property
    Public Sub Update(): End Sub
    Public Function CreateReferenceFromObject(o): Set CreateReferenceFromObject = New Fake: End Function
    Public Sub AppendHybridShape(o): End Sub
    Public Function AddNewPointCoord(x, y, z): Set AddNewPointCoord = New Fake: End Function
    Public Function AddNewPlane3Points(a, b, c): Set AddNewPlane3Points = New Fake: End Function
    Public Sub SetAbsoluteAxisData(a)
        Dim i
        For i = 0 To 8: want(i) = a(i): Next
    End Sub
    Public Sub GetAbsoluteAxisData(ByRef b)
        Dim i
        For i = 0 To 2
            b(i) = want(i) + 3 * want(3 + i) + 2 * want(6 + i)      ' origin moved
            b(3 + i) = want(6 + i)                                  ' H and V swapped: mirrored axis
            b(6 + i) = want(3 + i)
        Next
        W "AXIS " & b(0) & " " & b(1) & " " & b(2) & " " & b(3) & " " & b(4) & " " & b(5) & " " & b(6) & " " & b(7) & " " & b(8)
    End Sub
    Public Function OpenEdition(): Set OpenEdition = New Fake: End Function
    Public Sub CloseEdition(): End Sub
    Public Function CreateLine(a, b, c, d): W "LINE " & a & " " & b & " " & c & " " & d: End Function
    Public Function CreateClosedCircle(a, b, r): W "CIRCLE " & a & " " & b & " " & r: End Function
    Public Function CreateCircle(a, b, r, s, e): W "ARC " & a & " " & b & " " & r & " " & s & " " & e: End Function
    Public Function AddNewPad(s, l): W "PAD " & l: Set AddNewPad = New Fake: End Function
    Public Function AddNewPocket(s, l): W "POCKET " & l: Set AddNewPocket = New Fake: End Function
    Public Function GetMeasurable(r): Set GetMeasurable = New Fake: End Function
    Public Property Get Volume: Volume = 1: End Property
    Public Sub GetCOG(ByRef c)
        c(0) = 0: c(1) = 0: c(2) = 0
    End Sub
End Class
Class FakeAdd1
    Public Function Add(x): Set Add = New Fake: End Function
End Class
"""
if os.name == "nt" and shutil.which("cscript"):
    import math
    mock_log = os.path.join(tmp, "mock.log")
    mocked = text.replace('Set CATIA = GetObject(, "CATIA.Application")', "Set CATIA = New Fake")
    mocked = mocked.replace("Option Explicit", 'Option Explicit\r\nConst MOCKLOG = "' + mock_log + '"', 1) + MOCK
    mscript = os.path.join(tmp, "mock.vbs")
    with io.open(mscript, "w", encoding="utf-16", newline="") as f:
        f.write(mocked)
    r = subprocess.run(["cscript", "//nologo", mscript], capture_output=True, text=True)
    log = io.open(info["log"], encoding="utf-16").read()
    report("macro runs to the end against a stand-in", r.returncode == 0 and "DONE ok=0 bad=3" in log,
           (r.stdout + r.stderr).strip()[:200] or log.strip()[-40:])
    rows = [ln.split() for ln in io.open(mock_log).read().splitlines()]
    sketches, cur = [], None
    for row in rows:
        if row[0] == "AXIS":
            cur = {"ax": [float(x.replace(",", ".")) for x in row[1:]], "items": []}
            sketches.append(cur)
        elif row[0] in ("LINE", "CIRCLE", "ARC"):
            cur["items"].append((row[0], [float(x.replace(",", ".")) for x in row[1:]]))

    def glob(ax, u, v):
        return tuple(ax[i] + u * ax[3 + i] + v * ax[6 + i] for i in range(3))

    def close(p, q):
        return max(abs(a - b) for a, b in zip(p, q)) < 1e-6

    ok_lines = ok_arcs = True
    for skt, feat in zip(sketches, feats):
        for (kind, vals), g in zip(skt["items"], feat["geometry"]):
            ax = skt["ax"]
            if kind == "LINE":
                ok_lines &= close(glob(ax, vals[0], vals[1]), g[1]) and close(glob(ax, vals[2], vals[3]), g[2])
            elif kind == "CIRCLE":
                ok_lines &= close(glob(ax, vals[0], vals[1]), g[1]) and abs(vals[2] - g[2]) < 1e-9
            else:
                cu, cv, rad, a0, a1 = vals
                ends = {0: glob(ax, cu + rad * math.cos(a0), cv + rad * math.sin(a0)),
                        1: glob(ax, cu + rad * math.cos(a1), cv + rad * math.sin(a1))}
                mid = glob(ax, cu + rad * math.cos((a0 + a1) / 2), cv + rad * math.sin((a0 + a1) / 2))
                # expected middle of the arc: bisector of start and end as seen from the centre, on the short side
                # (every arc of this outline is a 90 degree fillet)
                c, s0, s1 = V(*g[1]), V(*g[3]), V(*g[4])
                want_mid = c + ((s0 - c) + (s1 - c)).normalize() * rad
                ok_arcs &= a1 > a0 and close(glob(ax, cu, cv), g[1]) and close(mid, (want_mid.x, want_mid.y, want_mid.z)) \
                    and {tuple(round(x, 5) for x in e) for e in ends.values()} == \
                    {tuple(round(x, 5) for x in g[3]), tuple(round(x, 5) for x in g[4])}
    n_items = sum(len(k["items"]) for k in sketches)
    report("all sketch entities drawn", len(sketches) == 3 and n_items == sum(len(f["geometry"]) for f in feats),
           f"{n_items} entities")
    report("lines and circles land on the right global points in a shifted, mirrored sketch axis", ok_lines)
    report("arcs keep their side in a mirrored sketch axis", ok_arcs)
    orient = [row[1] for row in rows if row[0] == "ORIENT"]
    report("both directions and both through-all modes are tried", len(orient) >= 2 + 4 + 2, f"{len(orient)} tries")

sys.exit(1 if fails else 0)
