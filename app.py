"""STEP Studio - the desktop app: open a STEP file, see which sizes can be changed, try values, run a DOE.

    app.cmd                      (or: fc.cmd app.py)
    app.cmd C:\\parts\\part.stp    open that file right away

A window opens (the Edge app window if Edge is installed, otherwise your default browser). The engine runs in this
console window on your own computer only (address 127.0.0.1); close the console to quit. No AI model is used.
Work files and results go to the folder "steprecon_studio" in your Documents (change with --workspace)."""
import argparse
import os
import shutil
import subprocess
import sys
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from step_recon import studio  # noqa: E402


def default_workspace():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "steprecon_studio")


def open_window(url):
    """An app-style window without browser chrome if Edge or Chrome is there; else the default browser."""
    candidates = [os.path.join(os.environ.get(v, ""), p) for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")
                  for p in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe")]
    candidates += [shutil.which(n) or "" for n in ("msedge", "chrome", "google-chrome", "chromium")]
    for exe in candidates:
        if exe and os.path.isfile(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1500,920"])
                return "app window"
            except OSError:
                pass
    webbrowser.open(url)
    return "browser"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="STEP/IGES file to open at start")
    ap.add_argument("--workspace", default=default_workspace(), help="folder for models and DOE results")
    ap.add_argument("--port", type=int, default=0, help="fixed port (default: any free one)")
    ap.add_argument("--no-window", action="store_true", help="do not open a window (print the address only)")
    a = ap.parse_args()
    if a.file and not os.path.isfile(a.file):
        raise SystemExit(f"file not found: {a.file}\n(put paths with spaces or parentheses in double quotes)")
    server, st = studio.serve(a.workspace, a.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"STEP Studio  {url}")
    print(f"workspace    {a.workspace}")
    if a.file:
        print(f"opening      {a.file} ...")
        try:
            s = st.open(os.path.abspath(a.file))
            c = s["info"]["counts"]
            print(f"             {c['holes']} holes, {c['fillets']} filleted and {c['chamfers']} chamfered edges; "
                  f"identical={s['info']['identical']}")
        except Exception as e:
            print(f"             could not open it: {type(e).__name__}: {e}")
    if not a.no_window:
        print("window       " + open_window(url))
    print("Keep this console open while you work. Close it (or press Ctrl+C) to quit.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        st.close()
        server.server_close()


if __name__ == "__main__":
    main()
