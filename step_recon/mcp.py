"""Model Context Protocol server side: lets an MCP client (Claude Code, Claude Desktop, ...) drive STEP Studio.

Only what is needed: initialize, ping, tools/list, tools/call - JSON-RPC 2.0 messages, one in, one out.
Transports:
    HTTP   POST http://127.0.0.1:<port>/mcp   (studio.py; answers with plain JSON, no event stream)
    stdio  mcp_server.py                      (a thin relay to that address, started by the client)
Standard library only."""
from . import tools

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = """STEP Studio turns a STEP part without modelling history into a fixed blank plus features whose \
sizes are named parameters (hole diameters and positions, fillet radii, chamfer sizes, position of flat faces = \
thickness), and builds variants of the part for a design of experiments (DOE). The user watches the part in the \
STEP Studio window; everything you change appears there.

How to work:
- Start with get_part (or open_part with a file path). A freshly opened part has no dimensions yet, only
candidates: call make_dimensions with the groups / items the user wants to vary (only those are computed, so pick
what is needed rather than everything). Then use the exact parameter names get_part lists.
- set_values rebuilds and checks the solid; a value that cannot be built is refused and nothing changes. \
find_limits tells how far a parameter can go.
- For a DOE: propose factors and ranges, call plan_doe, show the plan to the user, and call run_doe only after \
they agree. If run_doe says runs remain, call it again.
- Use show_feature to point at what you are talking about. Refer to features by number and place.
- Lengths are mm, angles degrees, volumes mm3. Do not guess numbers: read them from the tool results.
- Answer in the user's language."""


def _result(i, result):
    return {"jsonrpc": "2.0", "id": i, "result": result}


def _error(i, code, message):
    return {"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": message}}


def handle(studio, msg):
    """One JSON-RPC message -> the reply, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "not a JSON-RPC 2.0 message")
    method, i, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or "id" not in msg:
        return None                 # a reply or a notification (notifications/initialized, cancelled ...)
    if method == "initialize":
        want = params.get("protocolVersion")
        return _result(i, {"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "steprecon-studio", "title": "STEP Studio", "version": "2.1.0"},
                           "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(i, {})
    if method == "tools/list":
        return _result(i, {"tools": [{"name": t["name"], "description": t["description"],
                                      "inputSchema": t["input_schema"]} for t in tools.TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        if name not in tools.NAMES:
            return _error(i, -32602, f"unknown tool: {name}")
        text, bad = tools.call(studio, name, params.get("arguments") or {})
        return _result(i, {"content": [{"type": "text", "text": text}], "isError": bool(bad)})
    if method in ("resources/list", "prompts/list", "resources/templates/list"):
        return _result(i, {method.split("/")[0] if "templates" not in method else "resourceTemplates": []})
    return _error(i, -32601, f"method not found: {method}")
