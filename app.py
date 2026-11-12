"""STEP Studio - the desktop app: open a STEP file, see which sizes can be changed, try values, run a DOE.

    app.cmd                      (or: fc.cmd app.py)
    app.cmd C:\\parts\\part.stp    open that file right away

A window opens (the Edge app window if Edge is installed, otherwise your default browser). The engine runs in this
console window on your own computer only (address 127.0.0.1); close the console to quit. Without settings the app
uses no AI model. Two ways to add one:
  - an AI client drives the app over MCP (mcp.cmd, or the address printed at start + /mcp);
  - the chat panel in the window, when a model is configured through the environment (same variables as run.py):
        ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_MODEL)   a gateway speaking the Anthropic protocol
        ANTHROPIC_API_KEY                                              the Claude API
        STEPRECON_BASE_URL (+ STEPRECON_MODEL), --ai openai            an OpenAI-compatible server (Ollama ...)
    Then the conversation and the sizes of the features are sent to that server; the STEP file itself is not.
Work files and results go to the folder "steprecon_studio" in your Documents (change with --workspace)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from step_recon import chat, studio  # noqa: E402


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
    ap.add_argument("--ai", default="auto", choices=["auto", "off", "anthropic", "openai"],
                    help="model for the chat panel (default auto: from the environment, off if nothing is set)")
    ap.add_argument("--model", default=None, help="model name for the chat panel")
    ap.add_argument("--base-url", default=None, help="address of the model server for the chat panel")
    ap.add_argument("--max-tokens", type=int, default=None, help="output limit per model turn")
    a = ap.parse_args()
    if a.file and not os.path.isfile(a.file):
        raise SystemExit(f"file not found: {a.file}\n(put paths with spaces or parentheses in double quotes)")
    try:
        chat.from_settings(None, a.ai, model=a.model, base_url=a.base_url, max_tokens=a.max_tokens)
    except Exception as e:
        raise SystemExit(f"chat panel: {e}\n(start with --ai off to run without a model)")
    server, st = studio.serve(a.workspace, a.port, lambda s: chat.from_settings(
        s, a.ai, model=a.model, base_url=a.base_url, max_tokens=a.max_tokens))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"STEP Studio  {url}")
    print(f"MCP          {url}mcp")
    print("AI chat      " + (st.chat.label if st.chat.factory else "off (no model configured)"))
    mark = os.path.join(a.workspace, "studio.json")     # lets mcp_server.py find this engine
    with open(mark, "w", encoding="utf-8") as f:
        json.dump({"port": server.server_address[1], "pid": os.getpid()}, f)
    print(f"workspace    {a.workspace}")
    if a.file:
        print(f"opening      {a.file} ...")
        try:
            s = st.worker.call(st.open, os.path.abspath(a.file))
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
        try:
            os.remove(mark)
        except OSError:
            pass
        st.worker.call(st.close)
        server.server_close()


if __name__ == "__main__":
    main()
