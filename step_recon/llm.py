"""LLM backends for the reconstruction agent.

  anthropic : Claude API (needs ANTHROPIC_API_KEY), or any gateway speaking the Anthropic Messages protocol
              (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, as used for Claude Code) - then standard library only
  openai    : any OpenAI-compatible /chat/completions server -- Ollama, LM Studio, vLLM, llama.cpp server,
              OpenAI itself. Standard library only (no extra pip packages).

Both expose the same tiny interface so agent.py does not care which one is used:
    start(system, tools, user_text) -> None
    complete() -> Reply(text, calls)         # one model turn
    add_results([(Call, result_str), ...])   # feed tool results back
    transcript() -> list                      # JSON-serialisable conversation for debugging
Tool schemas are given in Anthropic format ({name, description, input_schema}).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")


@dataclass
class Call:
    id: str
    name: str
    args: dict
    error: str = ""          # set when the model produced arguments we could not parse


@dataclass
class Reply:
    text: str = ""
    calls: list = field(default_factory=list)


def _plain(obj):
    """Best-effort conversion of SDK objects to JSON-friendly data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: _plain(v) for k, v in vars(obj).items()}
    return obj


# ------------------------------------------------------------------ Anthropic
class AnthropicBackend:
    name = "anthropic"

    def __init__(self, model=None, client=None, max_tokens=4096, **_):
        self.model = model or os.environ.get("STEPRECON_MODEL", "claude-opus-5-5")
        self.max_tokens = max_tokens
        if client is None:
            if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
                sys.path.insert(0, _VENDOR)
            try:
                import anthropic
            except ImportError as e:
                raise RuntimeError("the 'anthropic' package is not installed (run setup, or pip install anthropic)") from e
            client = anthropic.Anthropic()
        self.client = client

    def start(self, system, tools, user_text):
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.tools = tools
        self.messages = [{"role": "user", "content": user_text}]

    def complete(self):
        resp = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=self.system,
                                           tools=self.tools, messages=self.messages)
        self.messages.append({"role": "assistant", "content": resp.content})
        calls = [Call(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
        return Reply(text, calls)

    def add_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": s} for c, s in results]})

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def transcript(self):
        return _plain(self.messages)


class AnthropicHttpBackend:
    """Anthropic Messages API over plain HTTP (standard library only, no SDK).

    For gateways that speak the Anthropic protocol and serve other models - the same kind of endpoint Claude Code
    is pointed at with ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN - and for the Claude API when the SDK is missing.
    Environment: ANTHROPIC_BASE_URL (default https://api.anthropic.com), ANTHROPIC_AUTH_TOKEN (sent as a Bearer
    token) or ANTHROPIC_API_KEY (sent as x-api-key)."""
    name = "anthropic"

    def __init__(self, model=None, base_url=None, max_tokens=16384, timeout=1800, temperature=None, **_):
        self.model = (model or os.environ.get("STEPRECON_MODEL") or os.environ.get("ANTHROPIC_MODEL")
                      or "claude-opus-5-5")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not (self.token or self.api_key):
            raise RuntimeError("set ANTHROPIC_AUTH_TOKEN (gateway) or ANTHROPIC_API_KEY (Claude API)")
        self.max_tokens, self.timeout, self.temperature = max_tokens, timeout, temperature
        self.custom = "api.anthropic.com" not in self.base_url
        self.last_info = ""

    def start(self, system, tools, user_text):
        # prompt caching is a Claude API feature; other models behind a gateway may reject the field
        self.system = system if self.custom else [{"type": "text", "text": system,
                                                   "cache_control": {"type": "ephemeral"}}]
        self.tools = tools
        self.messages = [{"role": "user", "content": user_text}]

    def _post(self, payload):
        url = self.base_url + ("/messages" if self.base_url.endswith("/v1") else "/v1/messages")
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.api_key:
            headers["x-api-key"] = self.api_key
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers),
                                            timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503, 504, 529) and attempt < 3:    # busy: wait and retry
                    time.sleep(self.retry_wait * (attempt + 1))
                    continue
                raise RuntimeError(f"LLM server returned HTTP {e.code}: {text[:400]}") from None
            except urllib.error.URLError as e:
                raise RuntimeError(f"cannot reach LLM server at {self.base_url}: {e.reason}") from None

    retry_wait = 5

    def complete(self):
        payload = {"model": self.model, "max_tokens": self.max_tokens, "system": self.system,
                   "tools": self.tools, "messages": self.messages}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        data = self._post(payload)
        # a reasoning model can spend the whole budget thinking and return nothing: ask again with more room
        # instead of wasting a turn
        while data.get("stop_reason") == "max_tokens" and self.max_tokens < 64000 \
                and not any(blk.get("type") == "tool_use" for blk in data.get("content") or []):
            self.max_tokens = payload["max_tokens"] = min(self.max_tokens * 2, 64000)
            data = self._post(payload)
        content = data.get("content") or []
        calls, text = [], ""
        for blk in content:
            if blk.get("type") == "tool_use":
                args = blk.get("input")
                calls.append(Call(blk.get("id") or f"call{len(calls)}", blk.get("name", ""),
                                  args if isinstance(args, dict) else {},
                                  "" if isinstance(args, dict) else "tool input is not a JSON object"))
            elif blk.get("type") == "text":
                text += blk.get("text", "")
        text = strip_think(text)
        if not calls:
            calls = parse_text_calls(text)      # a model that wrote the call as JSON text instead
            for c in calls:
                content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
        # an empty assistant message is rejected on the next request
        self.messages.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no answer)"}]})
        usage = data.get("usage") or {}
        self.last_info = f"[stop_reason={data.get('stop_reason')} output_tokens={usage.get('output_tokens')}]"
        if data.get("stop_reason") == "max_tokens":
            self.last_info += " the answer was cut off: raise --max-tokens"
        return Reply(text, calls)

    def add_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": s} for c, s in results]})

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def transcript(self):
        return _plain(self.messages)


def _sdk_available():
    if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
        sys.path.insert(0, _VENDOR)
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


# ------------------------------------------------------------- OpenAI-compatible
def flatten_schema(schema):
    """Local servers (llama.cpp grammars etc.) often choke on anyOf; keep the first branch and note the rest."""
    if isinstance(schema, list):
        return [flatten_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    s = dict(schema)
    if "anyOf" in s:
        branches = s.pop("anyOf")
        first = flatten_schema(branches[0]) if branches else {}
        s = {**first, **{k: v for k, v in s.items() if k != "description"}}
        extra = "; or an object for an arbitrary plane" if len(branches) > 1 else ""
        s["description"] = (schema.get("description", "") + extra).strip("; ")
        return s
    return {k: flatten_schema(v) for k, v in s.items()}


def _brief_params(schema):
    props = schema.get("properties", {})
    req = set(schema.get("required", []))
    parts = []
    for k, v in props.items():
        t = v.get("type", "any")
        if "enum" in v:
            t = "|".join(map(str, v["enum"]))
        parts.append(f"{k}{'' if k in req else '?'}: {t}")
    return "{" + ", ".join(parts) + "}"


def text_protocol_prompt(tools):
    lines = ["", "TOOL PROTOCOL: you cannot call functions natively. To use a tool, reply with ONE JSON object and "
             "nothing else, exactly like {\"tool\": \"pad\", \"args\": {...}}. Wait for the TOOL RESULT message before "
             "the next step. Available tools:"]
    for t in tools:
        lines.append(f"- {t['name']} {_brief_params(t['input_schema'])}: {t['description']}")
    return "\n".join(lines)


_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_think(text):
    """Remove <think>...</think> reasoning blocks (Qwen3, DeepSeek-R1...). They would pollute the history and may
    contain example JSON that must not be mistaken for a tool call."""
    return _THINK.sub("", text).strip()


def parse_text_calls(text):
    """Find JSON objects like {"tool": name, "args": {...}} in free text (code fences tolerated)."""
    calls, i, n = [], 0, 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < len(text):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        chunk = text[i:j + 1]
        try:
            obj = json.loads(chunk)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and ("tool" in obj or "name" in obj):
            args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            n += 1
            calls.append(Call(f"text{n}", obj.get("tool") or obj.get("name"), args if isinstance(args, dict) else {}))
        i = j + 1
    return calls


class OpenAICompatBackend:
    name = "openai"

    def __init__(self, model=None, base_url=None, api_key=None, tool_mode="auto", temperature=0.2,
                 max_tokens=4096, timeout=900, reasoning_effort=None, **_):
        self.model = model or os.environ.get("STEPRECON_MODEL", "qwen3:8b")
        self.base_url = (base_url or os.environ.get("STEPRECON_BASE_URL", "http://localhost:11434/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("STEPRECON_API_KEY") or os.environ.get("OPENAI_API_KEY") or "none"
        if tool_mode not in ("auto", "native", "text"):
            raise ValueError("tool_mode must be auto, native or text")
        self.tool_mode = tool_mode
        self.mode = "text" if tool_mode == "text" else "native"
        self.temperature, self.max_tokens, self.timeout = temperature, max_tokens, timeout
        self.reasoning_effort = reasoning_effort     # low/medium/high for reasoning models (gpt-oss, ...)
        self.last_info = ""
        self._last_native = False

    # -- conversation setup
    def start(self, system, tools, user_text):
        self.system_base, self.tools = system, tools
        self.messages = [{"role": "system", "content": self._system_text()}, {"role": "user", "content": user_text}]

    def _system_text(self):
        return self.system_base + (text_protocol_prompt(self.tools) if self.mode == "text" else "")

    def _switch_to_text(self):
        """Server rejected native tools: rewrite the history in the JSON-text protocol."""
        self.mode = "text"
        new = [{"role": "system", "content": self._system_text()}]
        for m in self.messages[1:]:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = [{"tool": c["function"]["name"], "args": json.loads(c["function"]["arguments"] or "{}")}
                         for c in m["tool_calls"]]
                new.append({"role": "assistant", "content": "\n".join(json.dumps(c) for c in calls)})
            elif m["role"] == "tool":
                new.append({"role": "user", "content": "TOOL RESULT: " + m["content"]})
            else:
                new.append(m)
        self.messages = new

    # -- HTTP
    def _post(self, payload):
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def complete(self):
        data = None
        for attempt in range(2):
            payload = {"model": self.model, "messages": self.messages, "temperature": self.temperature,
                       "max_tokens": self.max_tokens}
            if self.reasoning_effort:
                payload["reasoning_effort"] = self.reasoning_effort
            if self.mode == "native":
                payload["tools"] = [{"type": "function", "function": {
                    "name": t["name"], "description": t["description"],
                    "parameters": flatten_schema(t["input_schema"])}} for t in self.tools]
            try:
                data = self._post(payload)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if self.mode == "native" and self.tool_mode == "auto" and attempt == 0 and "tool" in body.lower():
                    self._switch_to_text()       # e.g. Ollama: "model does not support tools"
                    continue
                raise RuntimeError(f"LLM server returned HTTP {e.code}: {body[:400]}") from None
            except urllib.error.URLError as e:
                raise RuntimeError(f"cannot reach LLM server at {self.base_url}: {e.reason}") from None
        msg = data["choices"][0]["message"]
        think = msg.get("reasoning_content") or msg.get("reasoning") or ""
        usage = data.get("usage") or {}
        reason = data["choices"][0].get("finish_reason")
        self.last_info = (f"[finish_reason={reason} output_tokens={usage.get('completion_tokens')} "
                          f"reasoning_chars={len(think) if isinstance(think, str) else '?'}]")
        if reason == "length":
            self.last_info += " the answer was cut off: raise --max-tokens or use --reasoning-effort low"
        text = strip_think(msg.get("content") or "")
        calls, native = [], False
        for tc in msg.get("tool_calls") or []:
            raw = tc["function"].get("arguments", "{}")
            err, args = "", raw
            if isinstance(raw, str):
                try:
                    args = json.loads(raw or "{}")
                except ValueError:
                    args, err = {}, f"tool arguments are not valid JSON: {raw[:200]}"
            calls.append(Call(tc.get("id") or f"call{len(calls)}", tc["function"]["name"], args, err))
            native = True
        if native:
            keep = {"role": "assistant", "content": text, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                for c in calls]}
        else:
            calls = parse_text_calls(text)       # text protocol, or a model that "forgot" native calling
            keep = {"role": "assistant", "content": text}
        self._last_native = native
        self.messages.append(keep)
        return Reply(text, calls)

    def add_results(self, results):
        if self._last_native:
            for c, s in results:
                self.messages.append({"role": "tool", "tool_call_id": c.id, "content": s})
        else:
            body = "\n".join(f"TOOL RESULT [{c.name}]: {s}" for c, s in results)
            self.messages.append({"role": "user", "content": body})

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def transcript(self):
        return _plain(self.messages)


def make_backend(kind=None, **cfg):
    kind = (kind or os.environ.get("STEPRECON_BACKEND", "anthropic")).lower()
    if kind in ("anthropic", "claude"):
        cfg = {k: v for k, v in cfg.items() if v is not None}
        gateway = cfg.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if gateway or not _sdk_available():
            return AnthropicHttpBackend(**cfg)
        return AnthropicBackend(**cfg)
    if kind in ("openai", "local", "ollama", "lmstudio", "vllm"):
        return OpenAICompatBackend(**{k: v for k, v in cfg.items() if v is not None})
    raise ValueError(f"unknown backend {kind!r} (use anthropic or openai)")
