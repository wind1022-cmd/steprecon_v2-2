"""Generate synthetic prismatic test parts (ground-truth history known) and export STEP."""
import os
import random
import FreeCAD as App
import Part


def make_part(seed):
    rnd = random.Random(seed)
    L, W, H = rnd.choice([40, 60, 80]), rnd.choice([30, 40, 50]), rnd.choice([10, 15, 20])
    history = [f"pad rect {L}x{W} h={H}"]
    shape = Part.makeBox(L, W, H)
    # optional step / pocket from top
    if rnd.random() < 0.7:
        pw, pl, pd = rnd.choice([10, 15]), rnd.choice([15, 20]), rnd.choice([3, 5])
        x, y = rnd.choice([8, 12]), rnd.choice([8, 10])
        shape = shape.cut(Part.makeBox(pl, pw, pd, App.Vector(x, y, H - pd)))
        history.append(f"pocket rect {pl}x{pw} depth={pd} at ({x},{y})")
    # through holes
    for _ in range(rnd.randint(0, 3)):
        r = rnd.choice([2, 2.5, 3])
        cx, cy = rnd.choice([L - 8, L - 12]), rnd.choice([8 + 10 * i for i in range(max(1, int((W - 16) // 10)))])
        shape = shape.cut(Part.makeCylinder(r, H + 2, App.Vector(cx, cy, -1)))
        history.append(f"hole r={r} at ({cx},{cy})")
    # vertical-edge fillets
    if rnd.random() < 0.6:
        r = rnd.choice([2, 3])
        edges = [e for e in shape.Edges
                 if abs(e.Vertexes[0].Point.x - e.Vertexes[-1].Point.x) < 1e-6
                 and abs(e.Vertexes[0].Point.y - e.Vertexes[-1].Point.y) < 1e-6
                 and e.Length > H - 1e-6 and e.Length < H + 1e-6
                 and e.Vertexes[0].Point.x in (0, L) and e.Vertexes[0].Point.y in (0, W)]
        if edges:
            shape = shape.makeFillet(r, edges)
            history.append(f"fillet r={r} on {len(edges)} vertical outer edges")
    return shape, history


def generate(out_dir, n=10, start=0):
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i in range(start, start + n):
        shape, history = make_part(i)
        p = os.path.join(out_dir, f"synth_{i:03d}.step")
        shape.exportStep(p)
        with open(p + ".history.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(history))
        paths.append(p)
    return paths
