"""Bootstrap FreeCAD so modules can `import Part` from a plain Python interpreter or from freecadcmd.

Discovery order: already importable (freecadcmd / conda env) -> $FREECAD_BIN (folder that contains FreeCAD.pyd/.so)
-> well-known install locations on Windows, Linux and macOS.
"""
import glob
import os
import sys


def _candidates():
    env = os.environ.get("FREECAD_BIN")
    if env:
        yield env
    pf = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "") + r"\Programs"]
    for base in pf:                                           # Windows
        for p in sorted(glob.glob(os.path.join(base, "FreeCAD*", "bin")), reverse=True):
            yield p
    for p in ("/usr/lib/freecad/lib", "/usr/lib/freecad-python3/lib", "/usr/lib64/freecad/lib",   # Linux distros
              "/usr/local/lib/freecad/lib", "/opt/freecad/lib"):
        yield p
    for pre in (os.environ.get("CONDA_PREFIX"), sys.prefix):  # conda-forge freecad
        if pre:
            yield os.path.join(pre, "lib")
            yield os.path.join(pre, "Library", "bin")
    yield "/Applications/FreeCAD.app/Contents/Resources/lib"  # macOS


def _has_freecad(folder):
    return bool(glob.glob(os.path.join(folder, "FreeCAD.*")))


def find_freecad():
    """Folder containing the FreeCAD python module, or None if FreeCAD is importable already / not found."""
    for c in _candidates():
        if os.path.isdir(c) and _has_freecad(c):
            return c
    return None


try:
    import FreeCAD  # noqa: F401  (already on the path: freecadcmd, conda, FreeCAD's own python)
except ImportError:
    _found = find_freecad()
    if _found:
        sys.path.insert(0, _found)
        os.environ.setdefault("FREECAD_BIN", _found)
        if hasattr(os, "add_dll_directory") and os.name == "nt":      # Python >= 3.8 DLL search on Windows
            try:
                os.add_dll_directory(_found)
            except OSError:
                pass
    try:
        import FreeCAD  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "FreeCAD was not found. Install FreeCAD 1.x and either run this program with FreeCAD's own python "
            "(Windows: <FreeCAD>\\bin\\python.exe) or set FREECAD_BIN to the folder containing FreeCAD.pyd/.so. "
            "See README.md.") from e
