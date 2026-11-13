"""Moving a flat end face together with the chamfers around its rim (switch "WithRim"): geometry, parameters, DOE."""
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import machined, params as P, studio  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def lines(edges_of, z):
    return [e for e in edges_of if type(e.Curve).__name__ == "Line" and all(abs(v.Point.z - z) < 1e-6 for v in e.Vertexes)]


# plate 60 x 40 x 10: outline of the top chamfered 1, D10 bore with a 0.5 rim chamfer on top
plate = Part.makeBox(60, 40, 10).cut(Part.makeCylinder(5, 10, V(30, 20, 0)))
plate = plate.makeChamfer(1, lines(plate.Edges, 10))
plate = plate.makeChamfer(0.5, [e for e in plate.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Vertexes[0].Point.z - 10) < 1e-6])
A_SECTION = 60 * 40 - math.pi * 25
A_TOP = 58 * 38 - math.pi * 5.5 ** 2


def slanted_z(shape):
    """(lowest, highest) z of the chamfer faces: the planar / conical ones that are neither flat nor vertical."""
    zs = []
    for f in shape.Faces:
        u0, u1, v0, v1 = f.ParameterRange
        nz = abs(f.normalAt((u0 + u1) / 2, (v0 + v1) / 2).z)
        if 0.05 < nz < 0.95:
            zs += [f.BoundBox.ZMin, f.BoundBox.ZMax]
    return round(min(zs), 3), round(max(zs), 3)


sc = machined.scan(plate)
top = next(it for it in sc["items"] if it["kind"] == "face" and it["side"] == "+Z")
sess, rep = machined.build_selected(plate, [], [], [top["faces"][0]])
tmp = tempfile.mkdtemp(prefix="steprecon_rim_")
fcstd = os.path.join(tmp, "plate.FCStd")
sess.save(fcstd)
App.closeDocument(sess.doc.Name)
doc = App.openDocument(fcstd)
plist = P.collect(doc)
names = {p["name"]: p for p in plist}
report("the movable top face offers the 'with rim' switch, on by default",
       rep["faces_made"] == 1 and set(names) == {"Face_1.Thickness", "Face_1.WithRim"} and names["Face_1.WithRim"]["kind"] == "flag"
       and names["Face_1.WithRim"]["value"] == 1.0 and names["Face_1.Thickness"]["value"] == 10.0, str(sorted(names)))
base_v = P.result_shape(doc).Volume


def at(thick, rim=1):
    P.apply(doc, plist, {"Face_1.WithRim": rim, "Face_1.Thickness": thick})
    doc.recompute()
    s = P.result_shape(doc)
    return s, P.problems(doc)


s, bad = at(13)
report("+3 with rim: volume = section x 3, chamfers moved up, still one valid solid", not bad and s.isValid() and len(s.Solids) == 1
       and abs((s.Volume - base_v) - A_SECTION * 3) < 1e-3 and slanted_z(s) == (12.0, 13.0) and abs(s.BoundBox.ZMax - 13) < 1e-6
       and len(s.Faces) == len(plate.Faces), f"{s.Volume - base_v:.2f} {slanted_z(s)} {len(s.Faces)} faces")
s, bad = at(8)
report("-2 with rim: chamfers come down with the face", not bad and abs((s.Volume - base_v) + A_SECTION * 2) < 1e-3
       and slanted_z(s) == (7.0, 8.0), f"{s.Volume - base_v:.2f} {slanted_z(s)}")
s, bad = at(13, 0)
report("+3 without rim: only the face outline is pushed out, the chamfer stays at 9..10", not bad
       and abs((s.Volume - base_v) - A_TOP * 3) < 1e-3 and slanted_z(s) == (9.0, 10.0), f"{s.Volume - base_v:.2f} {slanted_z(s)}")
s, bad = at(8, 0)
report("-2 without rim: milled, no chamfer left", not bad and abs(s.Volume - base_v) > 1 and abs(s.BoundBox.ZMax - 8) < 1e-6)
s, bad = at(10, 1)
report("back to the original value: the original part", not bad and abs(s.Volume - base_v) < 1e-6 and slanted_z(s) == (9.0, 10.0))
s, bad = at(1, 1)
report("a value the part cannot take is reported with the reason, not built", bad and "thick enough" in " ".join(bad), " ".join(bad)[:90])

# a part whose walls change below the rim (wider foot): shrinking across the step is refused
foot = Part.makeBox(80, 60, 4, V(-10, -10, 0)).fuse(Part.makeBox(60, 40, 10, V(0, 0, 4))).removeSplitter()
foot = foot.makeChamfer(1, lines(foot.Edges, 14))
sc2 = machined.scan(foot)
tf = next(it for it in sc2["items"] if it["kind"] == "face" and it["side"] == "+Z" and it["at"][2] > 13)
sess2, rep2 = machined.build_selected(foot, [], [], [tf["faces"][0]])
f2 = os.path.join(tmp, "foot.FCStd")
sess2.save(f2)
App.closeDocument(sess2.doc.Name)
d2 = App.openDocument(f2)
pl2 = P.collect(d2)
tn = next(p["name"] for p in pl2 if p["name"].endswith("Thickness"))
P.apply(d2, pl2, {tn: 14 - 2})
d2.recompute()
ok_small = not P.problems(d2)
P.apply(d2, pl2, {tn: 14 - 9})
d2.recompute()
bad2 = P.problems(d2)
report("shrinking across a step of the walls is refused with a reason", ok_small and bad2 and "straight" in " ".join(bad2), " ".join(bad2)[:80])

# no rim, no switch
bare = Part.makeBox(60, 40, 10)
sc3 = machined.scan(bare)
tb = next(it for it in sc3["items"] if it["kind"] == "face" and it["side"] == "+Z")
sess3, rep3 = machined.build_selected(bare, [], [], [tb["faces"][0]])
names3 = [p["name"] for p in P.collect(sess3.doc)]
report("a face without a rim has no switch", names3 == ["Face_1.Thickness"], str(names3))
App.closeDocument(sess3.doc.Name)

# ------------------------------------------------------------ a gear with spline flanks and a chamfered top
Z, ro, rr, w = 10, 40.0, 35.0, 12.0
dd = 2 * math.pi / Z
pt = lambda r, a: V(r * math.cos(a), r * math.sin(a), 0)  # noqa: E731
edges = []
for i in range(Z):
    a = i * dd
    edges.append(Part.ArcOfCircle(pt(rr, a), pt(rr, a + 0.1 * dd), pt(rr, a + 0.2 * dd)).toShape())
    s1 = Part.BSplineCurve()
    s1.interpolate([pt(rr, a + 0.2 * dd), pt(rr + 1.8, a + 0.27 * dd), pt(rr + 3.6, a + 0.33 * dd), pt(ro, a + 0.4 * dd)])
    edges.append(s1.toShape())
    edges.append(Part.ArcOfCircle(pt(ro, a + 0.4 * dd), pt(ro, a + 0.5 * dd), pt(ro, a + 0.6 * dd)).toShape())
    s2 = Part.BSplineCurve()
    s2.interpolate([pt(ro, a + 0.6 * dd), pt(rr + 3.6, a + 0.67 * dd), pt(rr + 1.8, a + 0.73 * dd), pt(rr, a + 0.8 * dd)])
    edges.append(s2.toShape())
    edges.append(Part.ArcOfCircle(pt(rr, a + 0.8 * dd), pt(rr, a + 0.9 * dd), pt(rr, a + dd)).toShape())
gear = Part.Face(Part.Wire(edges)).extrude(V(0, 0, w)).cut(Part.makeCylinder(12, w))
gear = gear.makeChamfer(0.5, [e for e in gear.Edges if all(abs(v.Point.z - w) < 1e-6 for v in e.Vertexes) and e.BoundBox.ZLength < 1e-6
                              and math.hypot(e.CenterOfMass.x, e.CenterOfMass.y) > 20])
sc4 = machined.scan(gear)
tg = max((it for it in sc4["items"] if it["kind"] == "face" and it["side"] == "+Z"), key=lambda it: it["size"])
t0 = time.time()
sess4, rep4 = machined.build_selected(gear, [], [], [tg["faces"][0]])
f4 = os.path.join(tmp, "gear.FCStd")
sess4.save(f4)
App.closeDocument(sess4.doc.Name)
d4 = App.openDocument(f4)
pl4 = P.collect(d4)
n4 = [p["name"] for p in pl4]
report("gear with spline flanks: top face with rim switch", rep4["faces_made"] == 1 and "Face_1.WithRim" in n4,
       f"{n4} {time.time() - t0:.1f}s")
if "Face_1.WithRim" in n4:
    v4 = P.result_shape(d4).Volume
    out = []
    for th, rim in ((14, 1), (9, 1), (14, 0)):
        P.apply(d4, pl4, {"Face_1.WithRim": rim, "Face_1.Thickness": th})
        d4.recompute()
        s = P.result_shape(d4)
        out.append((th, rim, not P.problems(d4) and s is not None and s.isValid(), round(s.BoundBox.ZMax, 3) if s is not None else None))
    report("gear: grow, shrink with the rim and grow without it all give valid parts", all(o[2] for o in out)
           and [o[3] for o in out] == [14.0, 9.0, 14.0], str(out))

# ------------------------------------------------------------ the app: the switch as an edit and as a DOE factor
step = os.path.join(tmp, "plate.stp")
plate.exportStep(step)
server, st = studio.serve(os.path.join(tmp, "ws"))
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, obj=None, data=None):
    if obj is not None:
        data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


with open(step, "rb") as f:
    code, s = call("/api/open?name=plate.stp", data=f.read())
pid = next(it["id"] for it in s["items"] if it["kind"] == "face" and it["side"] == "+Z")
code, m = call("/api/make", {"items": [pid]})
face = next(f for f in m["features"] if f["category"] == "face")
flag = next(p for p in face["params"] if p["short"] == "WithRim")
thick = next(p for p in face["params"] if p["short"] == "Thickness")
report("state: the switch is a flag without unit", flag["flag"] and flag["unit"] == "" and flag["base"] == 1 and not thick.get("flag"), str(flag))
v0 = m["measure"]["volume"]
code, e1 = call("/api/set", {"values": {thick["name"]: 13}})
code, e2 = call("/api/set", {"values": {thick["name"]: 13, flag["name"]: 0}})
report("edit: with and without the rim differ as expected", abs((e1["measure"]["volume"] - v0) - A_SECTION * 3) < 1e-2
       and abs((e2["measure"]["volume"] - v0) - A_TOP * 3) < 1e-2 and not e1["problems"] and not e2["problems"])
code, e3 = call("/api/set", {"values": {thick["name"]: 1}})
report("an impossible value is reported and sends no mesh", e3["problems"] and "mesh" not in e3, str(e3["problems"])[:70])
variants = []
k = 0
for th in (12, 13):
    for rim in (1, 0):
        k += 1
        variants.append({"index": k, "values": {thick["name"]: th, flag["name"]: rim}})
call("/api/doe/run", {"names": [thick["name"], flag["name"]], "variants": variants})
while call("/api/doe/status?from=0")[1]["running"]:
    time.sleep(0.1)
rows = {r["run"]: r for r in call("/api/doe/status?from=0")[1]["rows"]}
call("/api/doe/finish", {})
exp = {1: A_SECTION * 2, 2: A_TOP * 2, 3: A_SECTION * 3, 4: A_TOP * 3}
report("parallel DOE with the switch as a factor", all(rows[i]["ok"] and abs((rows[i]["volume"] - v0) - exp[i]) < 1e-2 for i in exp),
       str({i: round(rows[i]["volume"] - v0, 1) for i in rows}))
server.shutdown()
sys.exit(1 if fails else 0)
