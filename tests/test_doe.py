"""machined.py also splits off fillets and chamfers; doe.py varies the parameters and writes one STEP per variant."""
import csv
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze, compare, machined, params  # noqa: E402
import doe  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def zrange(e):
    zs = [v.Point.z for v in e.Vertexes]
    return min(zs), max(zs)


def bracket():
    """Block with a round boss: four vertical corners R3, boss root R2 (a torus), one top edge chamfered 1.5,
    a through bore D8 in the boss and two D5 holes in the plate."""
    b = Part.makeBox(40, 30, 10).fuse(Part.makeCylinder(8, 12, V(20, 15, 10))).removeSplitter().Solids[0]
    b = b.makeFillet(3, [e for e in b.Edges if type(e.Curve).__name__ == "Line" and zrange(e) == (0.0, 10.0)])
    b = b.makeFillet(2, [e for e in b.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Curve.Radius - 8) < 1e-6
                         and zrange(e) == (10.0, 10.0)])
    top = [e for e in b.Edges if type(e.Curve).__name__ == "Line" and zrange(e) == (10.0, 10.0)
           and abs(e.Vertexes[0].Point.y) < 1e-6 and abs(e.Vertexes[-1].Point.y) < 1e-6]
    b = b.makeChamfer(1.5, top)
    for c in (Part.makeCylinder(4, 22, V(20, 15, 0)), Part.makeCylinder(2.5, 10, V(6, 6, 0)),
              Part.makeCylinder(2.5, 10, V(34, 24, 0))):
        b = b.cut(c)
    return b


tmp = tempfile.mkdtemp(prefix="steprecon_doe_")
path = os.path.join(tmp, "bracket.step")
bracket().exportStep(path)
ref = analyze.load_shape(path)

blends = machined.find_blends(ref)
report("fillet and chamfer faces recognised", ("fillet", 3.0) in blends and ("fillet", 2.0) in blends
       and ("chamfer",) in blends, str({k: len(v) for k, v in blends.items()}))

sess, rep = machined.build(ref)
c = sess.compare()
labels = [o.Label for o in sess.body.Group if o.TypeId.startswith("PartDesign::") and o.TypeId != "PartDesign::FeatureBase"]
report("result identical to the reference", c["converged"] and c["xor_pct"] < 1e-3, f"xor={c['xor_pct']} {labels} skipped={rep['skipped']}")
report("holes, fillets and the chamfer are separate features", rep["holes_made"] == 3 and rep["fillets_made"] >= 5
       and rep["chamfers_made"] >= 1 and any(x.startswith("Fillet_R3") for x in labels)
       and any(x.startswith("Fillet_R2") for x in labels) and any(x.startswith("Chamfer_1.5") for x in labels), str(rep))
blank = sess.body.BaseFeature.Shape
report("blank is sharp-edged and without holes", not any(type(f.Surface).__name__ == "Toroid" for f in blank.Faces)
       and len(blank.Faces) < len(ref.Faces))
model = os.path.join(tmp, "bracket.FCStd")
sess.save(model)
App.closeDocument(sess.doc.Name)

# parameters
doc = App.openDocument(model)
plist = params.collect(doc)
names = [p["name"] for p in plist]
App.closeDocument(doc.Name)
fr3 = next(n for n in names if n.startswith("Fillet_R3") and n.endswith(".Radius"))
fr2 = next(n for n in names if n.startswith("Fillet_R2") and n.endswith(".Radius"))
ch = next(n for n in names if n.startswith("Chamfer_1_5") and n.endswith(".Size"))
bore = next(n for n in names if n.startswith("Hole_D8") and n.endswith(".Diameter1"))
vals = {p["name"]: p["value"] for p in plist}
report("parameters carry the model's values", abs(vals[fr3] - 3) < 1e-9 and abs(vals[fr2] - 2) < 1e-9
       and abs(vals[ch] - 1.5) < 1e-9 and abs(vals[bore] - 8) < 1e-9, f"{len(names)} parameters")
report("hole positions are parameters too", any(n.startswith("Hole_D5_x2.DistanceX") for n in names)
       and any(n.startswith("Hole_D5_x2.DistanceY") for n in names))

# movable faces: the plate's underside (thickness 10) and the top of the boss (height 22)
thick = sorted((p["value"], p["name"]) for p in plist if p["name"].endswith(".Thickness"))
report("flat faces become movable: underside 10, boss top 22, the four sides 30 / 40; chamfer faces are not offered",
       [round(v, 3) for v, _ in thick] == [10.0, 22.0, 30.0, 30.0, 40.0, 40.0] and rep.get("faces_made") == 6, str(thick))
plate_t, boss_h = thick[0][1], thick[1][1]
doc = App.openDocument(model)
kinds = {o.Label: o.SR_Kind for o in doc.Objects if hasattr(o, "SR_Kind")}
normals = {}
for o in doc.Objects:
    if hasattr(o, "SR_Kind"):
        fc = o.Profile[0].Shape.getElement(o.Profile[1][0])
        nv = fc.normalAt(*fc.Surface.parameter(fc.CenterOfMass))
        normals[o.Label] = (round(nv.x), round(nv.y), round(nv.z))
App.closeDocument(doc.Name)
report("sides bounded by fillets and the chamfer are lands, underside and boss top have straight surroundings",
       sorted(kinds.values()) == ["land"] * 4 + ["straight"] * 2, str(kinds))
side = next(lbl for lbl, nv in normals.items() if nv == (0, -1, 0))       # the y = 0 side, which carries the chamfer
rows_s = doe.run(model, [{side + ".Thickness": 30}, {side + ".Thickness": 27}, {side + ".Thickness": 33}],
                 os.path.join(tmp, "doe_side"), verbose=False)
report("moving a land in and out changes the part size by exactly that much",
       [r["ok"] for r in rows_s] == [True] * 3 and [r["size_y"] for r in rows_s] == [30.0, 27.0, 33.0]
       and rows_s[1]["volume"] < rows_s[0]["volume"] < rows_s[2]["volume"],
       str([(r["size_y"], r["volume"]) for r in rows_s]))
cut_shape = analyze.load_shape(os.path.join(tmp, "doe_side", rows_s[1]["file"]))
slab = Part.makeBox(60, 3, 40, V(-10, 0, -10))
report("milling a land lower leaves nothing above the new plane (no rim from the old fillets and chamfer)",
       cut_shape.isValid() and cut_shape.common(slab).Volume < 1e-6 and len(cut_shape.Solids) == 1,
       f"left in the removed zone: {cut_shape.common(slab).Volume:.6f}")
names_now = [p["name"] for p in plist]
report("helper outlines of the lands are not offered as parameters",
       not any("_in." in n for n in names_now) and len(names_now) == 13 + 6, f"{len(names_now)} parameters")
rows_t = doe.run(model, [{plate_t: 10}, {plate_t: 12}, {plate_t: 7}, {boss_h: 30, bore: 10}, {plate_t: 0}],
                 os.path.join(tmp, "doe_thick"), verbose=False)
area_bottom = 40 * 30 - (4 - math.pi) * 9 - math.pi * 16 - 2 * math.pi * 6.25
dv = [float(r["volume"]) - float(rows_t[0]["volume"]) if r["ok"] else None for r in rows_t]
report("plate thickness 10 -> 12 / 7 adds / removes exactly area x change", abs(dv[1] - 2 * area_bottom) < 0.05
       and abs(dv[2] + 3 * area_bottom) < 0.05 and rows_t[1]["size_z"] == 24.0 and rows_t[2]["size_z"] == 19.0,
       f"{dv[1]:.3f} {dv[2]:.3f} expected {2 * area_bottom:.3f} {-3 * area_bottom:.3f}")
ring = math.pi * (64 - 25)
bore_extra = math.pi * (25 - 16) * 22
report("boss height 22 -> 30 together with a larger bore follows the new bore", rows_t[3]["ok"]
       and abs(dv[3] - (8 * ring - bore_extra)) < 0.05 and rows_t[3]["size_z"] == 30.0, f"{dv[3]:.3f} expected {8 * ring - bore_extra:.3f}")
report("thickness 0 is refused, not built", not rows_t[4]["ok"] and "larger than 0" in rows_t[4]["error"])
check = compare.compare(ref, analyze.load_shape(os.path.join(tmp, "doe_thick", rows_t[0]["file"])))
report("unmoved faces leave the part identical to the reference", check["xor_pct"] < 1e-3)

# a full factorial run through the command line
out = os.path.join(tmp, "doe_out")
sys.argv = ["doe.py", model, "--vary", f"{fr3}=2:4:3", "--vary", f"{bore}=6,10", "--out", out]
doe.main()
with open(os.path.join(out, "results.csv"), encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))
report("3 x 2 variants, all built, one STEP each", len(rows) == 6 and all(r["ok"] == "True" for r in rows)
       and all(os.path.exists(os.path.join(out, r["file"])) for r in rows))
same = next(r for r in rows if float(r[fr3]) == 3 and float(r[bore]) == 6)
big = next(r for r in rows if float(r[fr3]) == 3 and float(r[bore]) == 10)
report("bore D6 -> D10 removes exactly the ring", abs((float(same["volume"]) - float(big["volume"])) - math.pi * (25 - 9) * 22) < 0.05,
       f"{float(same['volume']) - float(big['volume']):.3f}")
v = sorted((float(r[fr3]), float(r["volume"])) for r in rows if float(r[bore]) == 6)
report("larger corner radius -> less material", v[0][1] > v[1][1] > v[2][1])
shape = analyze.load_shape(os.path.join(out, same["file"]))
report("exported STEP is a valid solid of the reported volume", shape.isValid() and abs(shape.Volume - float(same["volume"])) < 0.05)

# the model's own values give the reference back; impossible values are reported, the run goes on
table = os.path.join(tmp, "runs.csv")
with open(table, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["run", fr3, ch])
    w.writerow([1, 3, 1.5])
    w.writerow([2, 50, 1.5])       # a 50 mm fillet does not fit
    w.writerow([3, 1, 0.5])
rows = doe.run(model, doe.read_table(table), os.path.join(tmp, "doe_tab"), verbose=False)
r1 = compare.compare(ref, analyze.load_shape(os.path.join(tmp, "doe_tab", rows[0]["file"])))
report("original values reproduce the reference", rows[0]["ok"] and r1["xor_pct"] < 1e-3, f"xor={r1['xor_pct']}")
report("an impossible variant fails cleanly and the next one still builds", not rows[1]["ok"] and rows[1]["error"]
       and rows[2]["ok"], rows[1]["error"][:60])

# --list
sys.argv = ["doe.py", model, "--list"]
doe.main()
report("--list writes the parameter table", os.path.exists(os.path.join(tmp, "bracket.params.csv")))
with open(os.path.join(tmp, "bracket.params.csv"), encoding="utf-8-sig") as f:
    prow = list(csv.DictReader(f))
bore_row = next(r for r in prow if r["name"] == bore)
report("table gives feature number and position", bore_row["no"] == "1" and (float(bore_row["x"]), float(bore_row["y"])) == (20.0, 15.0),
       str(bore_row))
page = open(os.path.join(tmp, "bracket.params.html"), encoding="utf-8").read()
nfeat = len({r["no"] for r in prow})
report("parameter map: four views, every feature drawn and listed, self-contained",
       page.count("<svg") == 4 and all(f'data-f="{k}"' in page for k in range(1, nfeat + 1))
       and page.count("<tr data-f=") == nfeat and all(n in page for n in (fr3, fr2, ch, bore))
       and "http://" not in page and "https://" not in page and page.count('class="hl"') >= 4 * nfeat - 2,
       f"{len(page)} bytes, {nfeat} features")
for bad, what in (("nope=1,2", "unknown parameter"), (f"{fr3}=1:2", "bad range")):
    try:
        sys.argv = ["doe.py", model, "--vary", bad, "--out", os.path.join(tmp, "x")]
        doe.main()
        report(what + " is refused", False)
    except SystemExit as e:
        report(what + " is refused", bool(str(e)), str(e)[:50])

sys.exit(1 if fails else 0)
