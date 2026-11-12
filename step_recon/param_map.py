"""A picture of where every parameter sits on the part: four views with numbered balloons and a table.

With dozens of holes and fillets a list of names is not enough to know which number belongs to which place. This
writes one self-contained HTML file (no internet, no extra software): top / front / right / isometric line drawings
with the hidden lines removed, every feature that carries parameters drawn in colour with its number, and a table
with number, feature, position and parameters. Pointing at a table row lights the feature up in all views."""
import html
import math

import FreeCAD as App
import Part

from . import params as P

# view name -> (right, up) of the picture in model coordinates; the viewer looks along -(right x up)
_S = 1 / math.sqrt(2)
VIEWS = (
    ("Top (looking down -Z)", (1, 0, 0), (0, 1, 0)),
    ("Front (looking along +Y)", (1, 0, 0), (0, 0, 1)),
    ("Right (looking along -X)", (0, 1, 0), (0, 0, 1)),
    ("Isometric", (_S, _S, 0), (-1 / math.sqrt(6), 1 / math.sqrt(6), 2 / math.sqrt(6))),
)
COLORS = ("#d1495b", "#2e86ab", "#3c9d5d", "#b7791f", "#7b4fb0", "#c2410c", "#0f8f8f", "#b83280")
SIZE, PAD = 430, 34


def _features(doc, plist):
    """Features that carry parameters: [{"n", "label", "type", "edges", "anchors", "params"}]."""
    owner = {}
    for o in doc.Objects:
        sk = P._profile(o)
        if sk is not None and sk.TypeId == "Sketcher::SketchObject":
            owner.setdefault(sk.Name, o)
    groups, order = {}, []
    for p in plist:
        obj = doc.getObject(p["obj"])
        feat = owner.get(obj.Name, obj)
        if feat.Name not in groups:
            groups[feat.Name] = {"feat": feat, "params": []}
            order.append(feat.Name)
        groups[feat.Name]["params"].append(p)
    out = []
    for n, name in enumerate(order, 1):
        feat = groups[name]["feat"]
        edges = []
        sk = P._profile(feat)
        try:
            if sk is not None and sk.TypeId == "Sketcher::SketchObject":
                edges = list(sk.Shape.Edges)
            elif sk is not None:            # profile is a face of an earlier feature (movable faces)
                for sub in feat.Profile[1]:
                    edges += list(sk.Shape.getElement(sub).Edges)
                sk = None
            elif hasattr(feat, "Base") and feat.Base:
                base, subs = feat.Base
                edges = [base.Shape.getElement(s) for s in subs if s.startswith("Edge")]
        except Exception:
            edges = []
        anchors = []
        for e in edges[:60]:
            try:
                c = e.Curve.Center if type(e.Curve).__name__ == "Circle" and e.isClosed() else e.CenterOfMass
                if not any(c.distanceToPoint(a) < 1e-6 for a in anchors):
                    anchors.append(c)
            except Exception:
                pass
        # a balloon on every hole of a sketch, but only on two of the edges of a fillet / chamfer: the coloured
        # edges already show where it runs
        keep = 8 if sk is not None else 2
        out.append({"n": n, "name": feat.Name, "label": feat.Label, "type": feat.TypeId.split("::")[-1], "edges": edges,
                    "anchors": anchors[:keep], "count": len(anchors), "params": groups[name]["params"]})
    return out


def _matrix(right, up):
    r, u = App.Vector(*right), App.Vector(*up)
    n = r.cross(u)
    return App.Matrix(r.x, r.y, r.z, 0, u.x, u.y, u.z, 0, n.x, n.y, n.z, 0, 0, 0, 0, 1)


def _polyline(edge):
    try:
        if type(edge.Curve).__name__ in ("Line", "LineSegment"):
            pts = [edge.valueAt(edge.FirstParameter), edge.valueAt(edge.LastParameter)]
        else:
            pts = edge.discretize(Number=max(8, min(60, int(edge.Length / 1.5) + 8)))
        return [(p.x, p.y) for p in pts]
    except Exception:
        return []


def _view_svg(title, right, up, shape, feats):
    m = _matrix(right, up)
    turned = shape.copy()
    turned.transformShape(m)
    visible, hidden = [], []
    try:
        import TechDraw
        parts = TechDraw.projectEx(turned, App.Vector(0, 0, 1))
        for k, s in enumerate(parts):
            if s.isNull():
                continue
            if k in (0, 1, 3):
                visible += [_polyline(e) for e in s.Edges]
            elif k in (5, 6):
                hidden += [_polyline(e) for e in s.Edges]
    except Exception:       # no hidden-line removal available: plain wireframe
        visible = [_polyline(e) for e in turned.Edges]
    marks = []
    for f in feats:
        lines = []
        for e in f["edges"]:
            c = e.copy()
            c.transformShape(m)
            lines.append(_polyline(c))
        pts = [m.multVec(a) for a in f["anchors"]]
        marks.append((f, lines, [(p.x, p.y) for p in pts]))
    xs = [x for line in visible + hidden for x, _ in line] or [0, 1]
    ys = [y for line in visible + hidden for _, y in line] or [0, 1]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    k = (SIZE - 2 * PAD) / max(x1 - x0, y1 - y0, 1e-9)
    ox = PAD + ((SIZE - 2 * PAD) - (x1 - x0) * k) / 2
    oy = PAD + ((SIZE - 2 * PAD) - (y1 - y0) * k) / 2

    def px(p):
        return ox + (p[0] - x0) * k, SIZE - (oy + (p[1] - y0) * k)

    def path(line):
        return "M" + " L".join("%.1f %.1f" % px(p) for p in line)

    out = [f'<figure><svg viewBox="0 0 {SIZE} {SIZE}" role="img" aria-label="{html.escape(title)}">']
    out.append('<path class="hid" d="%s"/>' % " ".join(path(ln) for ln in hidden if len(ln) > 1))
    out.append('<path class="vis" d="%s"/>' % " ".join(path(ln) for ln in visible if len(ln) > 1))
    cx, cy = SIZE / 2, SIZE / 2
    for f, lines, anchors in marks:
        col = COLORS[(f["n"] - 1) % len(COLORS)]
        d = " ".join(path(ln) for ln in lines if len(ln) > 1)
        out.append(f'<g class="f" data-f="{f["n"]}" style="--c:{col}">')
        if d:
            out.append(f'<path class="hl" d="{d}"/>')
        for a in anchors:
            x, y = px(a)
            vx, vy = x - cx, y - cy
            length = math.hypot(vx, vy) or 1.0
            bx, by = x + 20 * vx / length, y + 20 * vy / length     # balloon pushed away from the middle
            bx, by = min(max(bx, 12), SIZE - 12), min(max(by, 12), SIZE - 12)
            out.append(f'<line class="lead" x1="{x:.1f}" y1="{y:.1f}" x2="{bx:.1f}" y2="{by:.1f}"/>'
                       f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="2.2"/>'
                       f'<circle class="bal" cx="{bx:.1f}" cy="{by:.1f}" r="9.5"/>'
                       f'<text x="{bx:.1f}" y="{by + 3.6:.1f}">{f["n"]}</text>')
        out.append("</g>")
    out.append(f"</svg><figcaption>{html.escape(title)}</figcaption></figure>")
    return "\n".join(out)


_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mut:#667085;--line:#d8dde5;--vis:#2b3442;--hid:#c3cad6;--row:#eef3fb}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#12161c;--card:#1a2029;--ink:#e6eaf0;--mut:#98a2b3;
--line:#2c3440;--vis:#d5dbe5;--hid:#3a4453;--row:#222b38}}
:root[data-theme=dark]{--bg:#12161c;--card:#1a2029;--ink:#e6eaf0;--mut:#98a2b3;--line:#2c3440;--vis:#d5dbe5;--hid:#3a4453;--row:#222b38}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI","Malgun Gothic",system-ui,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 40px}h1{font-size:20px;margin:0 0 4px}p.sub{margin:0 0 16px;color:var(--mut)}
.views{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px;margin-bottom:18px}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px}
figcaption{font-size:12px;color:var(--mut);text-align:center;padding:2px 0 4px}svg{display:block;width:100%;height:auto}
.vis{fill:none;stroke:var(--vis);stroke-width:1;stroke-linecap:round}.hid{fill:none;stroke:var(--hid);stroke-width:.7;stroke-dasharray:3 3}
.hl{fill:none;stroke:var(--c);stroke-width:2.4;stroke-linecap:round}.lead{stroke:var(--c);stroke-width:1}.dot{fill:var(--c)}
.bal{fill:var(--card);stroke:var(--c);stroke-width:1.6}.f text{font-size:10.5px;font-weight:600;text-anchor:middle;fill:var(--ink)}
.f.on .hl{stroke-width:5}.f.on .bal{fill:var(--c)}.f.on text{fill:#fff}.dim .f:not(.on){opacity:.18}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:640px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600}tr.on td{background:var(--row)}td.n{font-weight:700;white-space:nowrap}
td.n i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}code{font:12.5px Consolas,monospace}
td.v{font-variant-numeric:tabular-nums;white-space:nowrap}td.p div{display:flex;justify-content:space-between;gap:16px}
"""
_JS = """
const root=document.querySelector('main');
function on(n,s){document.querySelectorAll('[data-f="'+n+'"]').forEach(e=>e.classList.toggle('on',s));root.classList.toggle('dim',s);}
document.querySelectorAll('[data-f]').forEach(e=>{e.addEventListener('mouseenter',()=>on(e.dataset.f,true));
e.addEventListener('mouseleave',()=>on(e.dataset.f,false));e.addEventListener('click',()=>{const r=document.querySelector('tr[data-f="'+e.dataset.f+'"]');
if(r&&e.tagName!=='TR')r.scrollIntoView({block:'center',behavior:'smooth'});});});
"""


def build_html(doc, plist, title):
    shape = P.result_shape(doc)
    feats = _features(doc, plist)
    views = "\n".join(_view_svg(t, r, u, shape, feats) for t, r, u in VIEWS)
    rows = []
    for f in feats:
        col = COLORS[(f["n"] - 1) % len(COLORS)]
        where = ""
        if f["anchors"]:
            a = f["anchors"][0]
            where = "%.4g, %.4g, %.4g" % (a.x, a.y, a.z)
            if f["count"] > 1:
                where += " (+%d)" % (f["count"] - 1)
        plines = "".join('<div><code>%s</code><span>%.6g %s</span></div>'
                         % (html.escape(p["name"]), p["value"], "deg" if p["kind"] == "angle" else "mm")
                         for p in f["params"])
        rows.append(f'<tr data-f="{f["n"]}"><td class="n"><i style="background:{col}"></i>{f["n"]}</td>'
                    f'<td>{html.escape(f["label"])}</td><td>{html.escape(f["type"])}</td><td class="v">{where}</td>'
                    f'<td class="p">{plines}</td></tr>')
    bb = shape.BoundBox
    sub = "%d parameters in %d features &middot; part size %.4g x %.4g x %.4g mm" % (
        len(plist), len(feats), bb.XLength, bb.YLength, bb.ZLength)
    return ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)} parameters</title><style>{_CSS}</style></head><body><main>"
            f"<h1>{html.escape(title)}</h1><p class=\"sub\">{sub}</p><div class=\"views\">{views}</div>"
            "<div class=\"tablewrap\"><table><thead><tr><th>No.</th><th>Feature</th><th>Type</th>"
            "<th>Position x, y, z (mm)</th><th>Parameters</th></tr></thead><tbody>"
            + "".join(rows) + f"</tbody></table></div></main><script>{_JS}</script></body></html>")


def table(doc, plist):
    """Rows for the parameter CSV: number of the feature in the picture, name, value, unit, position."""
    rows = []
    for f in _features(doc, plist):
        a = f["anchors"][0] if f["anchors"] else None
        for p in f["params"]:
            rows.append({"no": f["n"], "feature": f["label"], "name": p["name"], "value": round(p["value"], 6),
                         "unit": "deg" if p["kind"] == "angle" else "mm",
                         "x": "" if a is None else round(a.x, 4), "y": "" if a is None else round(a.y, 4),
                         "z": "" if a is None else round(a.z, 4)})
    return rows
