"""A saved .FCStd must carry display data: without it FreeCAD opens a console-made file with every Body and
feature hidden and only the sketches visible, which looks as if nothing but lines had been made."""
import os
import re
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from step_recon import fc_tools  # noqa: E402
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

fails = 0


def report(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


s = fc_tools.Session(Part.makeBox(20, 10, 5))
s.pad("XY", [{"type": "rect", "p1": [0, 0], "p2": [20, 10]}], 5)
s.pocket("XY", [{"type": "circle", "c": [10, 5], "r": 2}], through_all=True, offset=5)
s.new_body()
s.pad("XY", [{"type": "rect", "p1": [30, 0], "p2": [40, 10]}], 5)
path = os.path.join(tempfile.mkdtemp(), "t.FCStd")
s.save(path)

with zipfile.ZipFile(path) as z:
    names = z.namelist()
    xml = z.read("GuiDocument.xml").decode("utf-8") if "GuiDocument.xml" in names else ""
report("GuiDocument.xml written once", names.count("GuiDocument.xml") == 1)
vis = dict(re.findall(r'<ViewProvider name="(\w+)".*?<Bool value="(\w+)"/>', xml))
report("every object listed", set(vis) == {o.Name for o in s.doc.Objects}, f"{len(vis)} entries")
shown = sorted(n for n, v in vis.items() if v == "true")
want = sorted([b.Name for b in s.bodies] + [b.Tip.Name for b in s.bodies])
report("bodies and their tips shown, the rest hidden", shown == want, str(shown))
report("sketches hidden", all(vis[o.Name] == "false" for o in s.doc.Objects if o.TypeId.startswith("Sketcher")))

s.save(path)    # saving again must not add a second copy
with zipfile.ZipFile(path) as z:
    report("still one after re-save", z.namelist().count("GuiDocument.xml") == 1)
d = App.openDocument(path)
report("file reopens with both bodies", len([o for o in d.Objects if o.TypeId == "PartDesign::Body"]) == 2)

sys.exit(1 if fails else 0)
