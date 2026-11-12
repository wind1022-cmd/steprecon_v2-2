"""Back end of the desktop app (app.py): one part at a time, everything through a small local HTTP API.

    open STEP -> blank + features (machined.build) -> parameters -> edit / design-of-experiments -> STEP per variant

The page (studio.html) is served from here too. Only this computer can reach the server (127.0.0.1); nothing is
sent anywhere else. FreeCAD is not thread-safe, so requests are handled one after the other."""
import csv
import json
import math
import os
import re
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import FreeCAD as App

from . import analyze, machined, param_map, params as P

HERE = os.path.dirname(os.path.abspath(__file__))
PALETTE = {"hole": "#3b82f6", "fillet": "#10b981", "chamfer": "#f59e0b", "face": "#a855f7", "other": "#64748b"}


def _category(feat_type, label):
    if label.startswith("Face_"):
        return "face"
    if feat_type in ("Pocket", "Hole") or label.startswith("Hole"):
        return "hole"
    if feat_type == "Fillet":
        return "fillet"
    if feat_type == "Chamfer":
        return "chamfer"
    return "other"


def _r(v, n=3):
    return tuple(round(x, n) for x in v)


def _canon(v):
    big = max(range(3), key=lambda i: abs(v[i]))
    return v if v[big] > 0 else v * -1


def _face_key(f):
    """Identity of the SURFACE a face lies on. A feature "makes" the faces whose surface did not exist before it;
    a neighbouring face that merely gets trimmed keeps its surface and so stays with whoever made it."""
    s = f.Surface
    kind = type(s).__name__
    try:
        if kind == "Plane":
            n = _canon(App.Vector(s.Axis).normalize())
            return (kind, _r(n, 4), round(n.dot(s.Position), 3))
        if kind in ("Cylinder", "Cone", "Toroid"):
            a = _canon(App.Vector(s.Axis).normalize())
            c = App.Vector(s.Center)
            foot = c - a * c.dot(a) if kind == "Cylinder" else c
            size = (round(s.Radius, 4),) if kind == "Cylinder" else \
                (round(s.MajorRadius, 4), round(s.MinorRadius, 4)) if kind == "Toroid" else \
                (round(s.Radius, 4), round(s.SemiAngle, 4))
            return (kind, _r(a, 4), _r(foot), size)
        if kind == "Sphere":
            return (kind, _r(s.Center), round(s.Radius, 4))
    except Exception:
        pass
    c = f.CenterOfMass
    return (kind, round(f.Area, 3), _r(c))


class Studio:
    def __init__(self, workspace):
        self.workspace = workspace
        os.makedirs(workspace, exist_ok=True)
        self.doc = None
        self.info = None
        self.run_dir = None
        self.rows = []

    # ------------------------------------------------------------ model
    def open(self, path, name=None):
        """Read a STEP/IGES file and turn it into blank + features."""
        t0 = time.time()
        self.close()
        name = name or os.path.basename(path)
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.splitext(name)[0]) or "part"
        self.dir = os.path.join(self.workspace, base + time.strftime("_%Y%m%d_%H%M%S"))
        os.makedirs(self.dir, exist_ok=True)
        ref = analyze.load_shape(path)
        sess, report = machined.build(ref)
        fcstd = os.path.join(self.dir, base + ".FCStd")
        saved = sess.save(fcstd)
        cmp_ = sess.compare()
        self.doc, self.base, self.fcstd = sess.doc, base, fcstd
        self.ref_volume = ref.Volume
        self._collect()
        self.info = {"name": name, "base": base, "folder": self.dir, "seconds": round(time.time() - t0, 1),
                     "identical": bool(cmp_.get("converged")), "xor_pct": cmp_.get("xor_pct"),
                     "faces": len(ref.Faces), "skipped": report.get("skipped", []),
                     "counts": {"holes": report["holes_made"], "fillets": report["fillets_made"],
                                "chamfers": report["chamfers_made"], "faces": report.get("faces_made", 0)},
                     "sketches": saved.get("sketches", {})}
        return self.state(mesh=True)

    def close(self):
        if self.doc is not None:
            try:
                App.closeDocument(self.doc.Name)
            except Exception:
                pass
        self.doc, self.info, self.run_dir, self.rows = None, None, None, []

    def _collect(self):
        self.plist = P.collect(self.doc)
        self.base_values = {p["name"]: p["value"] for p in self.plist}
        self.features = param_map._features(self.doc, self.plist)

    def _body_features(self):
        out = []
        for b in self.doc.Objects:
            if b.TypeId == "PartDesign::Body":
                out += [o for o in b.Group if o.isDerivedFrom("PartDesign::Feature")]
        return out

    def _face_owner(self, shape):
        """final face index -> number of the feature that made it (0 = blank)."""
        number = {f["name"]: f["n"] for f in self.features}
        owner_of_key, prev = {}, set()
        for o in self._body_features():
            try:
                keys = {_face_key(f) for f in o.Shape.Faces}
            except Exception:
                continue
            n = number.get(o.Name, 0)
            for k in keys - prev:
                owner_of_key[k] = n
            prev = keys
        owner = [owner_of_key.get(_face_key(f), 0) for f in shape.Faces]
        # a movable face that has not been moved made no faces of its own: show the face it would move
        for o in self._body_features():
            if not hasattr(o, "SR_Pair") or o.Name not in number:
                continue
            try:
                src, subs = o.Profile
                target = src.Shape.getElement(subs[0])
                k, c, a = _face_key(target), target.CenterOfMass, target.Area
            except Exception:
                continue
            for i, f in enumerate(shape.Faces):
                if owner[i] == 0 and _face_key(f) == k and abs(f.Area - a) < 1e-3 * max(a, 1.0) \
                        and f.CenterOfMass.distanceToPoint(c) < 1e-3:
                    owner[i] = number[o.Name]
        return owner

    def mesh(self):
        """Triangles (with a feature number per vertex) and edge lines of the current solid."""
        shape = P.result_shape(self.doc)
        bb = shape.BoundBox
        defl = max(bb.DiagonalLength * 0.0015, 1e-3)
        owner = self._face_owner(shape)
        pos, nor, fid, idx = [], [], [], []
        for fi, face in enumerate(shape.Faces):
            try:
                verts, tris = face.tessellate(defl)
            except Exception:
                continue
            base = len(pos) // 3
            acc = [[0.0, 0.0, 0.0] for _ in verts]
            for a, b, c in tris:
                u, v = verts[b] - verts[a], verts[c] - verts[a]
                n = u.cross(v)
                for k in (a, b, c):
                    acc[k][0] += n.x
                    acc[k][1] += n.y
                    acc[k][2] += n.z
                idx += [base + a, base + b, base + c]
            for v, n in zip(verts, acc):
                length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
                pos += [round(v.x, 4), round(v.y, 4), round(v.z, 4)]
                nor += [round(n[0] / length, 4), round(n[1] / length, 4), round(n[2] / length, 4)]
                fid.append(owner[fi])
        lines = []
        for e in shape.Edges:
            try:
                pts = e.discretize(Deflection=defl) if type(e.Curve).__name__ not in ("Line", "LineSegment") \
                    else [e.valueAt(e.FirstParameter), e.valueAt(e.LastParameter)]
            except Exception:
                continue
            for a, b in zip(pts, pts[1:]):
                lines += [round(a.x, 4), round(a.y, 4), round(a.z, 4), round(b.x, 4), round(b.y, 4), round(b.z, 4)]
        return {"positions": pos, "normals": nor, "feature": fid, "indices": idx, "lines": lines,
                "bbox": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]}

    def state(self, mesh=False):
        if self.doc is None:
            return {"loaded": False}
        current = {p["name"]: p["value"] for p in P.collect(self.doc)}
        feats = []
        for f in self.features:
            cat = _category(f["type"], f["label"])
            a = f["anchors"][0] if f["anchors"] else None
            side = ""
            if cat == "face":       # which way the face looks, e.g. "-Z"
                try:
                    o = self.doc.getObject(f["name"])
                    face = o.Profile[0].Shape.getElement(o.Profile[1][0])
                    u0, u1, v0, v1 = face.ParameterRange
                    nrm = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
                    k = max(range(3), key=lambda i: abs(nrm[i]))
                    if abs(nrm[k]) > 0.999:
                        side = ("+" if nrm[k] > 0 else "-") + "XYZ"[k]
                except Exception:
                    pass
            kind = ""
            if cat == "face":
                kind = str(getattr(self.doc.getObject(f["name"]), "SR_Kind", ""))
            feats.append({"n": f["n"], "label": f["label"], "type": f["type"], "category": cat, "side": side,
                          "kind": kind,
                          "color": PALETTE[cat], "count": f.get("count", 1),
                          "position": None if a is None else [round(a.x, 3), round(a.y, 3), round(a.z, 3)],
                          "params": [{"name": p["name"], "short": p["name"].split(".", 1)[-1],
                                      "base": round(self.base_values[p["name"]], 6),
                                      "value": round(current.get(p["name"], p["value"]), 6),
                                      "unit": "deg" if p["kind"] == "angle" else "mm"} for p in f["params"]]})
        shape = P.result_shape(self.doc)
        out = {"loaded": True, "info": self.info, "features": feats, "problems": P.problems(self.doc),
               "measure": self._measure(shape) if shape is not None else None,
               "reference_volume": round(self.ref_volume, 4)}
        if mesh:
            out["mesh"] = self.mesh()
        return out

    @staticmethod
    def _measure(shape):
        bb = shape.BoundBox
        solids = shape.Solids or [shape]
        vol = sum(s.Volume for s in solids) or 1e-12
        c = [sum(getattr(s.CenterOfMass, ax) * s.Volume for s in solids) / vol for ax in "xyz"]
        return {"volume": round(vol, 4), "area": round(shape.Area, 4), "cog_x": round(c[0], 4),
                "cog_y": round(c[1], 4), "cog_z": round(c[2], 4), "size_x": round(bb.XLength, 4),
                "size_y": round(bb.YLength, 4), "size_z": round(bb.ZLength, 4)}

    def set_values(self, values, from_base=True):
        """Apply parameter values (others go back to the part's own values) and rebuild."""
        if self.doc is None:
            raise ValueError("no part is open")
        unknown = [k for k in values if k not in self.base_values]
        if unknown:
            raise ValueError("unknown parameter: " + ", ".join(unknown))
        if from_base:
            P.apply(self.doc, self.plist, self.base_values)
        P.apply(self.doc, self.plist, {k: float(v) for k, v in values.items()})
        self.doc.recompute()
        return P.problems(self.doc)

    # ------------------------------------------------------------ design of experiments
    def doe_start(self, names):
        self.run_dir = os.path.join(self.dir, "doe" + time.strftime("_%H%M%S"))
        os.makedirs(self.run_dir, exist_ok=True)
        self.rows, self.doe_names = [], list(names)
        return {"folder": self.run_dir}

    def doe_variant(self, index, values):
        row = {"run": index, "ok": False, "file": "", "error": ""}
        row.update({k: float(values.get(k, self.base_values[k])) for k in self.doe_names})
        try:
            bad = self.set_values(values)
            if bad:
                row["error"] = "; ".join(bad)[:300]
            else:
                shape = P.result_shape(self.doc)
                row.update(self._measure(shape))
                name = f"run_{index:03d}.step"
                shape.exportStep(os.path.join(self.run_dir, name))
                row["file"], row["ok"] = name, True
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"[:300]
        self.rows.append(row)
        self._write_results()
        return row

    def _write_results(self):
        fields = ["run"] + self.doe_names + ["ok", "volume", "area", "cog_x", "cog_y", "cog_z", "size_x", "size_y",
                                             "size_z", "file", "error"]
        with open(os.path.join(self.run_dir, "results.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.rows)

    def doe_finish(self):
        self.set_values({})         # back to the part's own values
        archive = os.path.join(self.run_dir, self.base + "_doe.zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for n in sorted(os.listdir(self.run_dir)):
                if n.endswith((".step", ".csv")):
                    z.write(os.path.join(self.run_dir, n), n)
        return {"folder": self.run_dir, "zip": os.path.basename(archive), "ok": sum(1 for r in self.rows if r["ok"]),
                "total": len(self.rows)}


def make_handler(studio):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, separators=(",", ":")).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _json(self):
            raw = self._body()
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _local_only(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._local_only():
                return self._send(403, {"error": "local use only"})
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    with open(os.path.join(HERE, "studio.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if url.path == "/api/state":
                    return self._send(200, studio.state(mesh=True))
                if url.path == "/api/mesh":
                    return self._send(200, studio.mesh())
                if url.path == "/api/file":
                    name = os.path.basename(parse_qs(url.query).get("name", [""])[0])
                    for folder in (studio.run_dir, getattr(studio, "dir", None)):
                        p = os.path.join(folder, name) if folder and name else ""
                        if p and os.path.isfile(p):
                            with open(p, "rb") as f:
                                return self._send(200, f.read(), "application/octet-stream",
                                                  {"Content-Disposition": f'attachment; filename="{name}"'})
                    return self._send(404, {"error": "file not found"})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            if not self._local_only():
                return self._send(403, {"error": "local use only"})
            url = urlparse(self.path)
            try:
                if url.path == "/api/open":
                    q = parse_qs(url.query)
                    name = os.path.basename(q.get("name", ["part.stp"])[0])
                    if q.get("path"):
                        path = q["path"][0]
                        self._body()
                        if not os.path.isfile(path):
                            return self._send(400, {"error": f"file not found: {path}"})
                    else:
                        data = self._body()
                        if not data:
                            return self._send(400, {"error": "empty file"})
                        ext = os.path.splitext(name)[1].lower() or ".stp"
                        if ext not in (".stp", ".step", ".igs", ".iges"):
                            return self._send(400, {"error": "STEP 또는 IGES 파일만 열 수 있습니다"})
                        up = os.path.join(studio.workspace, "_upload")
                        os.makedirs(up, exist_ok=True)
                        path = os.path.join(up, "upload" + ext)
                        with open(path, "wb") as f:
                            f.write(data)
                    return self._send(200, studio.open(path, name))
                if url.path == "/api/set":
                    body = self._json()
                    bad = studio.set_values(body.get("values", {}))
                    st = studio.state(mesh=not bad)
                    return self._send(200, st)
                if url.path == "/api/doe/start":
                    return self._send(200, studio.doe_start(self._json().get("names", [])))
                if url.path == "/api/doe/variant":
                    b = self._json()
                    return self._send(200, studio.doe_variant(int(b["index"]), b.get("values", {})))
                if url.path == "/api/doe/finish":
                    self._body()
                    out = studio.doe_finish()
                    out["state"] = studio.state(mesh=True)
                    return self._send(200, out)
                if url.path == "/api/reveal":
                    self._body()
                    folder = studio.run_dir or getattr(studio, "dir", None)
                    if folder and hasattr(os, "startfile"):
                        os.startfile(folder)
                    return self._send(200, {"folder": folder})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def serve(workspace, port=0):
    """Returns (server, studio); call server.serve_forever()."""
    studio = Studio(workspace)
    server = HTTPServer(("127.0.0.1", port), make_handler(studio))
    return server, studio
