"""Scripted (no LLM) check of multi-body support on real Fusion 360 Gallery designs.

Picks designs that stay multi-solid after loading and whose solids are all trivially recognisable (axis-aligned box
or cylinder along a global axis), rebuilds each solid in its own body with one pad, and reports convergence.

  <FreeCAD python> dev/multibody_fusion_check.py data/fusion_idx.csv [--n 40]
"""
import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import analyze, dataset, fc_tools  # noqa: E402


def recognise(solid):
    """Return pad keyword arguments for a box / axis-aligned cylinder, else None."""
    bb = solid.BoundBox
    types = sorted(type(f.Surface).__name__ for f in solid.Faces)
    if types == ["Plane"] * 6 and abs(solid.Volume - bb.XLength * bb.YLength * bb.ZLength) < 1e-6 * max(1.0, solid.Volume):
        return dict(plane="XY", offset=bb.ZMin, length=bb.ZLength,
                    geometry=[{"type": "rect", "p1": [bb.XMin, bb.YMin], "p2": [bb.XMax, bb.YMax]}])
    if types == ["Cylinder", "Plane", "Plane"]:
        cyl = next(f.Surface for f in solid.Faces if type(f.Surface).__name__ == "Cylinder")
        ax, c, r = cyl.Axis, cyl.Center, cyl.Radius
        if abs(abs(ax.z) - 1) < 1e-9:
            h, kw = bb.ZLength, dict(plane="XY", offset=bb.ZMin, geometry=[{"type": "circle", "c": [c.x, c.y], "r": r}])
        elif abs(abs(ax.x) - 1) < 1e-9:
            h, kw = bb.XLength, dict(plane="YZ", offset=bb.XMin, geometry=[{"type": "circle", "c": [c.y, c.z], "r": r}])
        elif abs(abs(ax.y) - 1) < 1e-9:      # XZ sketch normal is -Y: plane sits at Y = -offset, reversed pads toward +Y
            h, kw = bb.YLength, dict(plane="XZ", offset=-bb.YMin, reversed=True,
                                     geometry=[{"type": "circle", "c": [c.x, c.z], "r": r}])
        else:
            return None
        if abs(solid.Volume - math.pi * r * r * h) < 1e-6 * max(1.0, solid.Volume):
            return dict(length=h, **kw)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--max-faces", type=int, default=60)
    a = ap.parse_args()
    rows = [r for r in dataset.read_index(a.index, {"prismatic", "revolved", "mixed"}, a.max_faces)
            if int(r.get("n_solids") or 1) > 1]
    print(f"{len(rows)} multi-solid analytic designs with <= {a.max_faces} faces in the index")
    tried = ok = 0
    for r in rows:
        if tried >= a.n:
            break
        ref = analyze.load_shape(r["path"])
        plans = [recognise(s) for s in ref.Solids]
        if len(plans) < 2 or any(p is None for p in plans):
            continue
        tried += 1
        s = fc_tools.Session(ref, f"mb{tried}")
        try:
            for i, p in enumerate(plans):
                if i:
                    assert s.new_body()["ok"]
                res = s.pad(**p)
                assert res["ok"], res
            c = s.compare()
            good = c["converged"] and c["ref_solids"] == c["new_solids"] == len(plans)
            ok += good
            print(f"{'PASS' if good else 'MISS'}  {os.path.basename(r['path'])}: solids={len(plans)} "
                  f"xor={c['xor_pct']}% faces {c['ref_faces']}/{c['new_faces']} bodies={c.get('bodies')}")
        except Exception as e:
            print(f"MISS  {os.path.basename(r['path'])}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{tried} designs rebuilt as one body per solid (scripted pads, no LLM)")

    # Regression: two OVERLAPPING solids (a rod running into a disc). The fuse works but removeSplitter returns an
    # invalid shape; the loader used to throw the whole merge away and keep a self-overlapping 2-solid compound.
    p = os.path.join(os.path.dirname(rows[0]["path"]), "41125_711db4bf_0021.step") if rows else ""
    if os.path.exists(p):
        sh = analyze.load_shape(p)
        good = len(sh.Solids) == 1 and sh.isValid() and abs(sh.Volume - 18.4983) < 1e-3
        print(f"{'PASS' if good else 'MISS'}  overlapping solids merge on load: solids={len(sh.Solids)} "
              f"volume={round(sh.Volume, 4)}")


if __name__ == "__main__":
    main()
