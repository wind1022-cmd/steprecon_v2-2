"""MCP server for STEP Studio (stdio): lets an AI client such as Claude Code work the app for you.

    claude mcp add steprecon -- cmd /c C:\\path\\to\\steprecon_v2\\mcp.cmd

The client starts this script and talks to it over stdin/stdout. The script itself is only a relay: it finds the
running STEP Studio (or starts one, with its window) and passes every message on to http://127.0.0.1:<port>/mcp.
So you watch in the STEP Studio window what the AI does, and can keep using the window yourself.

    --workspace DIR   where STEP Studio keeps its files (default: Documents\\steprecon_studio)
    --no-window       start the engine without opening the window
    --url URL         use this engine address instead of looking for one (http://127.0.0.1:8765)

Nothing but JSON-RPC lines is ever written to stdout; notes go to stderr."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))


def log(*a):
    print("[steprecon-mcp]", *a, file=sys.stderr, flush=True)


def default_workspace():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "steprecon_studio")


def alive(url):
    try:
        with urllib.request.urlopen(url + "/api/pulse", timeout=3) as r:
            return r.status == 200 and "rev" in json.loads(r.read().decode("utf-8"))
    except Exception:
        return False


def running(workspace):
    """Address of the STEP Studio that serves this workspace, or None."""
    try:
        with open(os.path.join(workspace, "studio.json"), encoding="utf-8") as f:
            url = f"http://127.0.0.1:{int(json.load(f)['port'])}"
        return url if alive(url) else None
    except Exception:
        return None


def start(workspace, window):
    cmd = [sys.executable, os.path.join(ROOT, "app.py"), "--workspace", workspace] + ([] if window else ["--no-window"])
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)       # its own console: closing that one quits the engine
    # Windows: no handles are passed on at all, so the engine writes to its new console and can never hold or
    # disturb the pipes this relay talks to its client through
    kw = {} if flags else {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                           "start_new_session": True}
    subprocess.Popen(cmd, cwd=ROOT, creationflags=flags, **kw)
    for _ in range(240):
        url = running(workspace)
        if url:
            return url
        time.sleep(0.5)
    raise RuntimeError("STEP Studio did not start (is FreeCAD installed? try app.cmd by hand)")


def forward(url, line):
    req = urllib.request.Request(url + "/mcp", data=line, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=24 * 3600) as r:
        return r.read() if r.status == 200 else b""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=default_workspace())
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--url")
    a = ap.parse_args()
    url = a.url.rstrip("/") if a.url else None
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        line = line.strip().lstrip(b"\xef\xbb\xbf").strip()     # some shells put a byte-order mark in front
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        reply = b""
        for attempt in (1, 2):
            try:
                if url is None or (attempt == 2 and not alive(url)):
                    url = (None if a.url else running(a.workspace)) or (a.url and url) or start(a.workspace, not a.no_window)
                    log("engine at", url)
                reply = forward(url, line)
                break
            except Exception as e:
                log(f"{type(e).__name__}: {e}")
                if attempt == 2 and isinstance(msg, dict) and "id" in msg:
                    reply = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
                        "code": -32000, "message": f"STEP Studio cannot be reached: {e}"}}).encode("utf-8")
        if reply.strip():
            out.write(reply.strip().replace(b"\n", b" ") + b"\n")
            out.flush()


if __name__ == "__main__":
    main()
