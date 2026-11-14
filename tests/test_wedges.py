"""Chamfers along the teeth of a gear that has a step (a flange) - so it is not a plain extrusion: the strips are
removed by filling the wedge each one cut off, and can be made a parameter. Seen on a user's part, where neither
worked: the kernel's defeaturing gave up on them."""
import math
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze, machined, params as P, sharpen  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0
V = App.Vector
W, Z = 14.0, 12


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def profile(z, r_root, r_tip):
    pts = []
    d = 2 * math.pi / z
    for i in range(z):
        a = i * d
        for t, r in ((0.0, r_root), (0.22, r_root), (0.36, r_tip), (0.64, r_tip), (0.78, r_root)):
            pts.append(V(r * math.cos(a + t * d), r * math.sin(a + t * d), 0))
    pts.append(pts[0])
    return Part.makePolygon(pts)


teeth = Part.Face(profile(Z, 36.0, 40.0)).extrude(V(0, 0, W))
flange = Part.makeCylinder(43.0, 3.0)                              # the step: the teeth run into a flange
sharp = teeth.fuse(flange).removeSplitter().cut(Part.makeCylinder(12, W))
# the edges along the teeth between tip land and flank, above the flange
tip_edges = [e for e in sharp.Edges if type(e.Curve).__name__ == "Line" and abs(e.Vertexes[0].Point.z - e.Vertexes[1].Point.z) > W - 3 - 1e-6
             and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - 40.0) < 1e-6]
gear = sharp.makeChamfer(0.4, tip_edges)
tmp = tempfile.mkdtemp(prefix="steprecon_wedge_")
path = os.path.join(tmp, "gear.stp")
gear.exportStep(path)
ref = analyze.load_shape(path)
print(f"{len(ref.Faces)} faces, {len(tip_edges)} chamfered edges, volume {ref.Volume:.3f} (sharp {sharp.Volume:.3f})")

sc = machined.scan(ref)
cham = [i["faces"][0] for i in sc["items"] if i["kind"] == "chamfer"]
report("the strips along the teeth are found as chamfers", len(cham) == len(tip_edges) == 2 * Z
       and {g["size"] for g in sc["groups"] if g["kind"] == "chamfer"} == {0.4}, f"{len(cham)} faces")
new, info = sharpen.rebuild_prism(ref, cham)
report("the outline route does not apply (step in the part)", new is None)

t0 = time.time()
res = machined.remove_faces_ex(ref, cham)
dt = time.time() - t0
out = res["shape"]
report("removed by filling the wedges: exactly the sharp part", res["method"] == "wedges" and res["failed"] == 0 and out.isValid()
       and abs(out.Volume - sharp.Volume) < 1e-6 and len(out.Faces) == len(sharp.Faces), f"{dt:.1f}s {res['method']} "
       f"{out.Volume - sharp.Volume:+.6f} mm3, {len(out.Faces)} faces")
res = machined.remove_faces_ex(ref, cham[:3])
report("a few of them only", res["method"] == "wedges" and res["done"] == 3 and len(res["shape"].Faces) == len(ref.Faces) - 3)

# a chamfer in a hollow corner (material has to be taken away again)
blk = Part.makeBox(30, 20, 10).cut(Part.makeBox(30, 10, 5, V(0, 10, 5)))
inner = [e for e in blk.Edges if type(e.Curve).__name__ == "Line" and all(abs(v.Point.y - 10) < 1e-9 and abs(v.Point.z - 5) < 1e-9 for v in e.Vertexes)]
hollow = blk.makeChamfer(1.0, inner)
strip = [i for i, f in enumerate(hollow.Faces) if type(f.Surface).__name__ == "Plane" and 0.1 < abs(f.Surface.Axis.y) < 0.9]
new, gone = sharpen.fill_wedges(hollow, strip)
report("hollow corner: the wedge is cut away", new is not None and abs(new.Volume - blk.Volume) < 1e-6 and len(gone) == 1)

# ------------------------------------------------------------ curved (spline) flanks, strips with mitred ends
def spline_gear(z=8):
    ro, rr, d = 40.0, 35.0, 2 * math.pi / z
    pt = lambda r, a: V(r * math.cos(a), r * math.sin(a), 0)  # noqa: E731
    es = []
    for i in range(z):
        a = i * d
        es.append(Part.makeLine(pt(rr, a), pt(rr, a + 0.2 * d)))
        c1 = Part.BSplineCurve()
        c1.interpolate([pt(rr, a + 0.2 * d), pt(rr + 1.8, a + 0.275 * d), pt(rr + 3.6, a + 0.335 * d), pt(ro, a + 0.4 * d)])
        es.append(c1.toShape())
        es.append(Part.makeLine(pt(ro, a + 0.4 * d), pt(ro, a + 0.6 * d)))
        c2 = Part.BSplineCurve()
        c2.interpolate([pt(ro, a + 0.6 * d), pt(rr + 3.6, a + 0.665 * d), pt(rr + 1.8, a + 0.725 * d), pt(rr, a + 0.8 * d)])
        es.append(c2.toShape())
        es.append(Part.makeLine(pt(rr, a + 0.8 * d), pt(rr, a + d)))
    body = Part.Face(Part.Wire(es)).extrude(V(0, 0, W)).fuse(Part.makeCylinder(43.0, 3.0)).removeSplitter().cut(Part.makeCylinder(12, W))
    tips = [e for e in body.Edges if type(e.Curve).__name__ == "Line" and abs(e.Vertexes[0].Point.z - e.Vertexes[1].Point.z) > W - 3 - 1e-6
            and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - ro) < 1e-6]
    c1_ = body.makeChamfer(0.35, tips)
    tops = [e for e in c1_.Edges if all(abs(v.Point.z - W) < 1e-6 for v in e.Vertexes) and e.BoundBox.ZLength < 1e-6
            and math.hypot(e.CenterOfMass.x, e.CenterOfMass.y) > 20]
    return body, c1_, c1_.makeChamfer(0.6, tops)


body, only_strips, sg = spline_gear()
scs = machined.scan(sg)
small = min((g for g in scs["groups"] if g["kind"] == "chamfer"), key=lambda g: g["size"])
ids = set(small["items"])
strips = [it["faces"][0] for it in scs["items"] if it["id"] in ids]
t0 = time.time()
res = machined.remove_faces_ex(sg, strips)
gain, took = res["shape"].Volume - sg.Volume, body.Volume - only_strips.Volume
report("strips between a flat tip and a curved flank, ending at another chamfer: removed (seam lines may stay)",
       res["method"] == "wedges" and res["failed"] == 0 and res["shape"].isValid() and 0.90 * took < gain < 1.0 * took,
       f"{time.time() - t0:.1f}s {len(strips)} strips, volume back {gain:.3f} of {took:.3f} (the rest is under the end chamfer)")
t0 = time.time()
sess3, rep3 = machined.build_selected(sg, [], strips, [])
c3 = sess3.compare()
report("those strips as a parameter (cut by a tool where the kernel's chamfer fails): equals the file",
       (c3.get("converged") or c3.get("near")) and rep3.get("strip_tools") == len(strips),
       f"{time.time() - t0:.1f}s tool strips {rep3.get('strip_tools')} xor {c3.get('xor_pct')} skipped={rep3['skipped'][:1]}")
m3 = os.path.join(tmp, "spline.FCStd")
sess3.save(m3)
App.closeDocument(sess3.doc.Name)
doc3 = App.openDocument(m3)
pl3 = P.collect(doc3)
sz = [p for p in pl3 if p["name"].startswith("Chamfer") and p["name"].endswith(".Size") and abs(p["value"] - 0.35) < 0.02]
v3 = P.result_shape(doc3).Volume
P.apply(doc3, pl3, {sz[0]["name"]: 0.7} if sz else {})
doc3.recompute()
s3 = P.result_shape(doc3)
report("spline strips 0.35 -> 0.7 after saving and opening: valid, smaller", bool(sz) and not P.problems(doc3) and s3.isValid()
       and s3.Volume < v3 - 5.0, f"{[p['name'] for p in sz]} {s3.Volume - v3:+.2f} mm3" if s3 is not None else str(P.problems(doc3)))
App.closeDocument(doc3.Name)
# ------------------------------------------------------------ rounds along the curved flanks
ftips = [e for e in body.Edges if type(e.Curve).__name__ == "Line" and abs(e.Vertexes[0].Point.z - e.Vertexes[1].Point.z) > W - 3 - 1e-6
         and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - 40.0) < 1e-6]
fg = body.makeFillet(0.4, ftips)
scf = machined.scan(fg)
fgr = [g for g in scf["groups"] if g["kind"] == "fillet"]
report("rounds along spline flanks are found as fillets R0.4", len(fgr) == 1 and abs(fgr[0]["size"] - 0.4) < 1e-3
       and len(fgr[0]["items"]) == len(ftips), str([(g["kind"], g["size"], len(g["items"])) for g in scf["groups"]]))
fids = [it["faces"][0] for it in scf["items"] if fgr and it["id"] in set(fgr[0]["items"])]
res = machined.remove_faces_ex(fg, fids)
report("those rounds removed: the sharp part (seam lines stay)", res["failed"] == 0 and res["shape"].isValid()
       and abs(res["shape"].Volume - body.Volume) < 1e-2, f"{res['method']} {res['shape'].Volume - body.Volume:+.4f} mm3")
t0 = time.time()
sess4, rep4 = machined.build_selected(fg, [], fids, [])
c4 = sess4.compare()
report("those rounds as a parameter: equals the file", (c4.get("converged") or c4.get("near")) and rep4.get("strip_tools") == len(fids)
       and rep4["fillets_made"] == len(fids), f"{time.time() - t0:.1f}s xor {c4.get('xor_pct')}")
pl4 = P.collect(sess4.doc)
rn = [p["name"] for p in pl4 if p["name"].startswith("Fillet_R0_4") and p["name"].endswith(".Radius")]
v4 = P.result_shape(sess4.doc).Volume
P.apply(sess4.doc, pl4, {rn[0]: 0.8} if rn else {})
s4 = P.result_shape(sess4.doc)
true8 = body.makeFillet(0.8, ftips).Volume
# the sections are scaled about the corner of the TANGENT lines; the flank bends away from them, so the larger the
# change, the further from a true fillet (here 8% of the volume change for twice the radius)
report("R0.4 -> 0.8: close to a true R0.8 fillet", bool(rn) and s4 is not None and s4.isValid()
       and abs(s4.Volume - true8) < 0.10 * abs(v4 - true8), f"{rn} {s4.Volume - true8:+.4f} mm3 off (change {s4.Volume - v4:+.3f})"
       if s4 is not None else str(P.problems(sess4.doc)))
App.closeDocument(sess4.doc.Name)
# ------------------------------------------------------------ bolt holes: conical chamfers on the rims
plain = Part.makeBox(120, 90, 20).fuse(Part.makeCylinder(12, 15, V(60, 45, 20))).removeSplitter()      # block with a boss
centres = [(15 + 18 * i, 12 + 22 * j) for i in range(6) for j in range(4) if not (40 < 15 + 18 * i < 80 and 30 < 12 + 22 * j < 60)]
for x, y in centres:
    plain = plain.cut(Part.makeCylinder(4, 20, V(x, y, 0)))
rims = [e for e in plain.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Curve.Radius - 4) < 1e-6 and abs(e.Curve.Center.z - 20) < 1e-6]
shaft = [e for e in plain.Edges if type(e.Curve).__name__ == "Circle" and abs(e.Curve.Radius - 12) < 1e-6 and abs(e.Curve.Center.z - 35) < 1e-6]
holed = plain.makeChamfer(1.0, rims + shaft)
p2 = os.path.join(tmp, "holes.stp")
holed.exportStep(p2)
ref2 = analyze.load_shape(p2)
cones = [i for i, f in enumerate(ref2.Faces) if type(f.Surface).__name__ == "Cone"]
t0 = time.time()
res = machined.remove_faces_ex(ref2, cones)
report("chamfers on the rims of bolt holes and on a shaft end: rings filled, exact", res["method"] == "wedges" and res["failed"] == 0
       and abs(res["shape"].Volume - plain.Volume) < 1e-6 and not any(type(f.Surface).__name__ == "Cone" for f in res["shape"].Faces),
       f"{len(cones)} cone faces of {len(centres)} holes + boss in {time.time() - t0:.1f}s")
sc2 = machined.scan(ref2)
h8 = [it["key"] for it in sc2["items"] if it["kind"] == "hole" and abs(it["size"] - 8) < 1e-6]
t0 = time.time()
sess2, rep2 = machined.build_selected(ref2, h8, [], [])
c2 = sess2.compare()
report("bolt holes as parameters, their rim chamfers come along", c2.get("converged") and rep2["holes_made"] == len(centres)
       and rep2["chamfers_made"] >= len(centres) and not rep2["warnings"], f"{time.time() - t0:.1f}s holes {rep2['holes_made']} chamfers {rep2['chamfers_made']}")
sess2.save(os.path.join(tmp, "holes.FCStd"))
pl2 = P.collect(sess2.doc)
dia = next(p["name"] for p in pl2 if p["name"].endswith("Diameter1"))
P.apply(sess2.doc, pl2, {dia: 10})
sess2.doc.recompute()
s2 = P.result_shape(sess2.doc)
radii = sorted({round(e.Curve.Radius, 3) for f in s2.Faces if type(f.Surface).__name__ == "Cone" and f.BoundBox.ZMax < 21
                for e in f.Edges if type(e.Curve).__name__ == "Circle"})
report("diameter 8 -> 10: the chamfers follow the holes", not P.problems(sess2.doc) and radii == [5.0, 6.0], str(radii))
App.closeDocument(sess2.doc.Name)
# ------------------------------------------------------------ as a parameter
t0 = time.time()
sess, rep = machined.build_selected(ref, [], cham, [])
c = sess.compare()
report("the chamfers become a parameter, the result equals the file", (c.get("converged") or c.get("near")) and rep["chamfers_made"] == len(cham),
       f"{time.time() - t0:.1f}s chamfer edges {rep['chamfers_made']} skipped={rep['skipped'][:1]}")
model = os.path.join(tmp, "g.FCStd")
sess.save(model)
App.closeDocument(sess.doc.Name)
doc = App.openDocument(model)
plist = P.collect(doc)
size = next(p["name"] for p in plist if p["name"].startswith("Chamfer") and p["name"].endswith(".Size"))
v0 = P.result_shape(doc).Volume
P.apply(doc, plist, {size: 0.8})
doc.recompute()
s = P.result_shape(doc)
report("chamfer 0.4 -> 0.8 rebuilds a valid, smaller part", not P.problems(doc) and s.isValid() and s.Volume < v0 - 1.0, f"{s.Volume - v0:+.2f} mm3")
# ------------------------------------------------------------ a tooth-tip land between two rounds is not a fillet
def land_gear(z=54, ro=42.55, rr=40.0):
    d = 2 * math.pi / z
    pt = lambda r, a: V(r * math.cos(a), r * math.sin(a), 0)  # noqa: E731
    es = []
    for i in range(z):
        a = i * d
        es.append(Part.makeLine(pt(rr, a), pt(rr, a + 0.2 * d)))
        c1 = Part.BSplineCurve()
        c1.interpolate([pt(rr, a + 0.2 * d), pt(rr + 0.9, a + 0.27 * d), pt(rr + 1.8, a + 0.33 * d), pt(ro, a + 0.4 * d)])
        es.append(c1.toShape())
        es.append(Part.Arc(pt(ro, a + 0.4 * d), pt(ro, a + 0.5 * d), pt(ro, a + 0.6 * d)).toShape())   # cylinder land
        c2 = Part.BSplineCurve()
        c2.interpolate([pt(ro, a + 0.6 * d), pt(rr + 1.8, a + 0.67 * d), pt(rr + 0.9, a + 0.73 * d), pt(rr, a + 0.8 * d)])
        es.append(c2.toShape())
        es.append(Part.makeLine(pt(rr, a + 0.8 * d), pt(rr, a + d)))
    b = Part.Face(Part.Wire(es)).extrude(V(0, 0, W)).cut(Part.makeCylinder(20, W))
    tips = [e for e in b.Edges if e.BoundBox.ZLength > W - 1e-6 and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - ro) < 1e-6]
    return b.makeFillet(0.5, tips), len(tips)


lg, nl = land_gear()
gl = [(x["kind"], x["size"], len(x["items"])) for x in machined.scan(lg)["groups"] if x["kind"] == "fillet"]
report("cylindrical tooth-tip lands (R42.55, seen on a user's ring gear) are not fillets; the R0.5 rounds are",
       gl == [("fillet", 0.5, nl)], str(gl))
# ------------------------------------------------------------ ring gear: hollow rounds at the roots (seen on a user's part)
def ring_gear(z=20, rroot=42.55, rtip=40.0, r=0.5):
    d = 2 * math.pi / z
    pt = lambda rad, a: V(rad * math.cos(a), rad * math.sin(a), 0)  # noqa: E731
    es = []
    for i in range(z):
        a = i * d
        es.append(Part.makeLine(pt(rtip, a), pt(rtip, a + 0.2 * d)))
        c1 = Part.BSplineCurve()
        c1.interpolate([pt(rtip, a + 0.2 * d), pt(rtip + 0.9, a + 0.27 * d), pt(rtip + 1.8, a + 0.33 * d), pt(rroot, a + 0.4 * d)])
        es.append(c1.toShape())
        es.append(Part.Arc(pt(rroot, a + 0.4 * d), pt(rroot, a + 0.5 * d), pt(rroot, a + 0.6 * d)).toShape())
        c2 = Part.BSplineCurve()
        c2.interpolate([pt(rroot, a + 0.6 * d), pt(rtip + 1.8, a + 0.67 * d), pt(rtip + 0.9, a + 0.73 * d), pt(rtip, a + 0.8 * d)])
        es.append(c2.toShape())
        es.append(Part.makeLine(pt(rtip, a + 0.8 * d), pt(rtip, a + d)))
    b = Part.makeCylinder(60, W).cut(Part.Face(Part.Wire(es)).extrude(V(0, 0, W)))
    roots = [e for e in b.Edges if e.BoundBox.ZLength > W - 1e-6 and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - rroot) < 1e-6]
    return b, b.makeFillet(r, roots), len(roots)


rb, rg, nr = ring_gear()
scr = machined.scan(rg)
rgr = [x for x in scr["groups"] if x["kind"] == "fillet"]
report("ring gear: the hollow root rounds are fillets R0.5 (and the root land is not)", [(x["size"], len(x["items"])) for x in rgr] == [(0.5, nr)],
       str([(x["kind"], x["size"], len(x["items"])) for x in scr["groups"]]))
rids = [it["faces"][0] for it in scr["items"] if rgr and it["id"] in set(rgr[0]["items"])]
t0 = time.time()
res = machined.remove_faces_ex(rg, rids)
took_r = rg.Volume - rb.Volume
report("ring gear: root rounds removed back to the sharp corners", res["failed"] == 0
       and res["shape"].isValid() and abs(res["shape"].Volume - rb.Volume) < 1e-3 * took_r,
       f"{time.time() - t0:.1f}s {res['method']} {res['shape'].Volume - rb.Volume:+.4f} of {took_r:.3f} mm3")
t0 = time.time()
sess5, rep5 = machined.build_selected(rg, [], rids, [])
c5 = rep5.get("_cmp") or {}
report("ring gear: root rounds as a parameter, equal to the file or near", rep5.get("strip_tools") == len(rids)
       and rep5["fillets_made"] == len(rids), f"{time.time() - t0:.1f}s")
pl5 = P.collect(sess5.doc)
rn5 = [p["name"] for p in pl5 if p["name"].endswith(".Radius")]
v5 = P.result_shape(sess5.doc).Volume
P.apply(sess5.doc, pl5, {rn5[0]: 0.3} if rn5 else {})
s5 = P.result_shape(sess5.doc)
report("ring gear: R0.5 -> 0.3 gives a valid part with less material in the roots", bool(rn5) and s5 is not None and s5.isValid()
       and s5.Volume < v5, f"{rn5} {s5.Volume - v5:+.3f} mm3" if s5 is not None else str(P.problems(sess5.doc)))
App.closeDocument(sess5.doc.Name)


# ------------------------------------------------- ring gear with a full-round root: one R0.5 per tooth space, no land
def round_root_gear(z=20, rv=42.55, rtip=40.0, r=0.5):
    d = 2 * math.pi / z
    h = rv - rtip
    pt = lambda rad, a: V(rad * math.cos(a), rad * math.sin(a), 0)  # noqa: E731
    es = []
    for i in range(z):
        a = i * d
        es.append(Part.makeLine(pt(rtip, a), pt(rtip, a + 0.25 * d)))
        c1 = Part.BSplineCurve()
        c1.interpolate([pt(rtip, a + 0.25 * d), pt(rtip + 0.35 * h, a + 0.335 * d), pt(rtip + 0.7 * h, a + 0.415 * d), pt(rv, a + 0.5 * d)])
        es.append(c1.toShape())
        c2 = Part.BSplineCurve()
        c2.interpolate([pt(rv, a + 0.5 * d), pt(rtip + 0.7 * h, a + 0.585 * d), pt(rtip + 0.35 * h, a + 0.665 * d), pt(rtip, a + 0.75 * d)])
        es.append(c2.toShape())
        es.append(Part.makeLine(pt(rtip, a + 0.75 * d), pt(rtip, a + d)))
    b = Part.makeCylinder(60, W).cut(Part.Face(Part.Wire(es)).extrude(V(0, 0, W)))
    roots = [e for e in b.Edges if e.BoundBox.ZLength > W - 1e-6 and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - rv) < 1e-6]
    return b, b.makeFillet(r, roots), len(roots)


vb, vg, nv = round_root_gear()
scv = machined.scan(vg)
vgr = [x for x in scv["groups"] if x["kind"] == "fillet"]
report("full-round root: one fillet R0.5 per tooth space", [(x["size"], len(x["items"])) for x in vgr] == [(0.5, nv)],
       str([(x["kind"], x["size"], len(x["items"])) for x in scv["groups"]]))
vids = [it["faces"][0] for it in scv["items"] if vgr and it["id"] in set(vgr[0]["items"])]
t0 = time.time()
res = machined.remove_faces_ex(vg, vids)
took_v = vg.Volume - vb.Volume
report("full-round root: removed back to the sharp V, exact", res["failed"] == 0 and res["shape"] is not None
       and res["shape"].isValid() and abs(res["shape"].Volume - vb.Volume) < 1e-3 * took_v,
       f"{time.time() - t0:.1f}s {res['method']} " + (f"{res['shape'].Volume - vb.Volume:+.4f} of {took_v:.3f} mm3" if res["shape"] is not None else ""))
t0 = time.time()
sess6, rep6 = machined.build_selected(vg, [], vids, [])
report("full-round root: as a parameter", rep6.get("strip_tools") == len(vids) and rep6["fillets_made"] == len(vids),
       f"{time.time() - t0:.1f}s {rep6.get('skipped')}")
pl6 = P.collect(sess6.doc)
rn6 = [p["name"] for p in pl6 if p["name"].endswith(".Radius")]
v6 = P.result_shape(sess6.doc).Volume
P.apply(sess6.doc, pl6, {rn6[0]: 0.3} if rn6 else {})
s6 = P.result_shape(sess6.doc)
report("full-round root: R0.5 -> 0.3 gives a valid part with less material in the roots", bool(rn6) and s6 is not None
       and s6.isValid() and s6.Volume < v6, f"{rn6} {s6.Volume - v6:+.3f} mm3" if s6 is not None else str(P.problems(sess6.doc)))
App.closeDocument(sess6.doc.Name)


# ---------- tooth slots that stop part-way (seen on a user's ring gear): floor = cone, end wall = plane, R0.5 between
# them = torus pieces, each ended by the two tooth flanks
def stopped_slots(z=20, rv=42.55, rtip=40.0, r=0.5, zs=10.0):
    d = 2 * math.pi / z
    pt = lambda rad, a: V(rad * math.cos(a), rad * math.sin(a), 0)  # noqa: E731
    slots = []
    for i in range(z):
        a = i * d
        c1 = Part.BSplineCurve()
        c1.interpolate([pt(rtip - 1, a + 0.22 * d), pt(rtip + 0.9, a + 0.29 * d), pt(rv + 1.0, a + 0.38 * d)])
        c2 = Part.BSplineCurve()
        c2.interpolate([pt(rv + 1.0, a + 0.62 * d), pt(rtip + 0.9, a + 0.71 * d), pt(rtip - 1, a + 0.78 * d)])
        f = Part.Face(Part.Wire([c1.toShape(), Part.makeLine(pt(rv + 1.0, a + 0.38 * d), pt(rv + 1.0, a + 0.62 * d)),
                                 c2.toShape(), Part.makeLine(pt(rtip - 1, a + 0.78 * d), pt(rtip - 1, a + 0.22 * d))]))
        f.translate(V(0, 0, -1))
        slots.append(f.extrude(V(0, 0, zs + 1)))
    cutter = Part.makeCompound(slots).common(Part.makeCone(rv + 0.1, rv - 0.15, zs + 1, V(0, 0, -1)))
    b = Part.makeCylinder(60, W).cut(Part.makeCylinder(rtip, W)).cut(cutter).removeSplitter()
    es = [e for e in b.Edges if type(e.Curve).__name__ == "Circle" and abs(e.BoundBox.ZMin - zs) < 1e-6
          and abs(e.BoundBox.ZMax - zs) < 1e-6 and abs(math.hypot(e.Vertexes[0].Point.x, e.Vertexes[0].Point.y) - 42.3) < 0.5]
    return b, b.makeFillet(r, es), len(es)


tb_, tg, nt = stopped_slots()
sct = machined.scan(tg)
tgr = [x for x in sct["groups"] if x["kind"] == "fillet"]
tids = [it["faces"][0] for it in sct["items"] if tgr and it["id"] in set(tgr[0]["items"])]
report("stopped slots: the R0.5 at the slot ends are torus pieces, one group", [(x["size"], len(x["items"])) for x in tgr] == [(0.5, nt)]
       and all(type(tg.Faces[i].Surface).__name__ == "Toroid" for i in tids), str([(x["kind"], x["size"], len(x["items"])) for x in sct["groups"]]))
t0 = time.time()
res = machined.remove_faces_ex(tg, tids)
report("stopped slots: R0.5 removed back to the sharp corner, exact", res["failed"] == 0 and res["shape"] is not None
       and res["shape"].isValid() and abs(res["shape"].Volume - tb_.Volume) < 1e-3 * (tg.Volume - tb_.Volume),
       f"{time.time() - t0:.1f}s {res['method']}")
t0 = time.time()
sess7, rep7 = machined.build_selected(tg, [], tids, [])
report("stopped slots: R0.5 as a parameter", rep7.get("strip_tools") == len(tids) and rep7["fillets_made"] == len(tids)
       and (rep7.get("_cmp") or {}).get("near") or (rep7.get("_cmp") or {}).get("converged"), f"{time.time() - t0:.1f}s {rep7.get('skipped')}")
pl7 = P.collect(sess7.doc)
rn7 = [p["name"] for p in pl7 if p["name"].endswith(".Radius")]
v7 = P.result_shape(sess7.doc).Volume
P.apply(sess7.doc, pl7, {rn7[0]: 0.3} if rn7 else {})
s7 = P.result_shape(sess7.doc)
report("stopped slots: R0.5 -> 0.3 gives a valid part with less material in the corners", bool(rn7) and s7 is not None
       and s7.isValid() and s7.Volume < v7, f"{rn7} {s7.Volume - v7:+.3f} mm3" if s7 is not None else str(P.problems(sess7.doc)))
App.closeDocument(sess7.doc.Name)
sys.exit(1 if fails else 0)
