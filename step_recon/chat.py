"""The chat panel of STEP Studio: the user writes what they want, a language model works the app through tools.py.

    user text -> model -> tool calls -> engine (always checked there) -> results -> model -> answer

The model runs wherever llm.make_backend points (a company gateway speaking the Anthropic protocol, the Claude API,
or an OpenAI-compatible server such as Ollama). What leaves this computer is the conversation and the tool results:
feature names, sizes, volumes, file names - never the STEP file or the geometry itself.

Building DOE variants writes many files, so run_doe waits here until the user presses the button in the panel.
A conversation turn runs on its own thread; everything that touches the model goes through studio.worker."""
import threading
import time
from urllib.parse import urlparse

from . import mcp, tools

MAX_TURNS = 16          # model turns per message of the user
KEEP_RESULTS = 6        # older tool results are shortened so that long sessions fit small models
SYSTEM = mcp.INSTRUCTIONS + """

You are the assistant built into the STEP Studio window. Be brief: say what you did and the numbers that matter.
- Each message of the user starts with a line in [brackets] written by the app: which part is open and which \
feature the user has selected in the 3D view. "this", "here", "이거", "여기" mean that feature.
- For a DOE call plan_doe and then run_doe straight away: the app shows the plan and asks the user to confirm \
before anything is built, so do not ask for confirmation yourself.
- If a request is unclear, ask one short question instead of guessing.
- Never invent a parameter name or a number; if you have not called get_part in this conversation, call it first."""


class Chat:
    def __init__(self, studio, factory=None, label=""):
        self.studio = studio
        self.factory = factory      # () -> llm backend; None = no model configured
        self.label = label          # shown in the panel, e.g. "model @ host"
        self.backend = None
        self.items = []             # what the panel shows: {"id", "role": user|ai|tool|error, "text"}
        self.busy = False
        self.pending = None         # a DOE plan waiting for the user's yes
        self.seq = 0
        self._stop = False
        self._gate = threading.Event()
        self._answer = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------ what the page reads
    def brief(self):
        return {"enabled": self.factory is not None, "busy": self.busy, "seq": self.seq, "pending": self.pending is not None}

    def public(self):
        return {"enabled": self.factory is not None, "label": self.label, "busy": self.busy, "seq": self.seq,
                "pending": self.pending, "items": self.items[-200:]}

    def _add(self, role, text):
        with self._lock:
            self.items.append({"id": len(self.items) + 1, "role": role, "text": text, "time": time.strftime("%H:%M")})
            self.seq += 1

    # ------------------------------------------------------------ what the page does
    def send(self, text, selected=0):
        text = str(text or "").strip()
        if not text:
            raise ValueError("빈 메시지입니다")
        if self.factory is None:
            raise ValueError("AI 모델이 설정되지 않았습니다")
        with self._lock:
            if self.busy:
                raise ValueError("이전 요청을 처리하는 중입니다")
            self.busy, self._stop = True, False
        self._add("user", text)
        threading.Thread(target=self._turn, args=(text, int(selected or 0)), daemon=True).start()
        return self.brief()

    def approve(self, ok):
        if self.pending is None:
            raise ValueError("확인을 기다리는 실험 계획이 없습니다")
        self._answer = bool(ok)
        self._gate.set()
        return self.brief()

    def stop(self):
        self._stop = True
        if self.pending is not None:
            self._answer = False
            self._gate.set()
        return self.brief()

    def clear(self):
        if self.busy:
            raise ValueError("처리 중에는 지울 수 없습니다")
        with self._lock:
            self.backend, self.items = None, []
            self.seq += 1
        return self.brief()

    # ------------------------------------------------------------ one turn of the conversation
    def _context(self, selected):
        st = self.studio
        if st.doc is None:
            return "[No part is open.]"
        line = f"[Part {st.info['name']} is open, {len(st.features)} features."
        f = next((f for f in st.features if f["n"] == selected), None)
        if f is not None:
            names = ", ".join(p["name"] for p in f["params"][:6])
            line += f" The user has feature #{f['n']} selected ({f['label']}; parameters: {names})."
        else:
            line += " No feature is selected."
        return line + "]"

    def _run_tool(self, name, args):
        st = self.studio
        seen = st.notes[-1]["id"] if st.notes else 0
        if name == "run_doe":
            text, bad = self._run_doe()
        else:
            text, bad = st.worker.call(tools.call, st, name, args)
        new = [n["text"] for n in st.notes if n["id"] > seen]
        for t in new or ([] if name in ("get_part", "doe_results") else [name]):
            self._add("tool", t)
        return text, bad

    def _run_doe(self):
        st = self.studio
        plan = st.plan
        if st.doc is None or not plan or plan["finished"]:
            return st.worker.call(tools.call, st, "run_doe", {})
        if not plan["started"]:
            self.pending = {"names": plan["names"], "rows": plan["rows"][:60], "total": len(plan["rows"]),
                            "fixed": plan["fixed"], "design": plan["design"]}
            self._gate.clear()
            self.seq += 1
            self._gate.wait()
            self.pending = None
            self.seq += 1
            if not self._answer:
                self._add("tool", "실험 계획 실행을 취소함")
                return "The user looked at the plan and chose NOT to run it. Ask what they would like to change.", True
        while True:         # in short pieces: the window stays alive and the user can stop
            text, bad = st.worker.call(tools.call, st, "run_doe", {"max_seconds": 5})
            if bad or plan["finished"]:
                return text, bad
            if self._stop:
                return text + "\nThe user pressed stop; the remaining runs were not built.", False

    def _trim(self):
        """Shorten tool results that are no longer the recent ones (both message formats)."""
        holders = []
        for m in getattr(self.backend, "messages", []):
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                holders.append(m)
            elif m.get("role") == "user" and isinstance(m.get("content"), list):
                holders += [b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
                            and isinstance(b.get("content"), str)]
        for h in holders[:-KEEP_RESULTS]:
            if len(h["content"]) > 500:
                h["content"] = h["content"][:300] + "\n...(older result shortened; call the tool again if needed)"

    def _turn(self, text, selected):
        fresh = self.backend is None
        try:
            prompt = self._context(selected) + "\n" + text
            if fresh:
                self.backend = self.factory()
                self.backend.start(SYSTEM, tools.TOOLS, prompt)
            else:
                self.backend.add_user(prompt)
            for _ in range(MAX_TURNS):
                if self._stop:
                    self._add("tool", "중지됨")
                    break
                self._trim()
                reply = self.backend.complete()
                if reply.text.strip():
                    self._add("ai", reply.text.strip())
                if not reply.calls:
                    break
                results = []
                for c in reply.calls:
                    if c.error:
                        results.append((c, f"The call could not be read: {c.error}. Send valid JSON arguments."))
                    elif self._stop:
                        results.append((c, "Not done: the user pressed stop."))
                    else:
                        results.append((c, self._run_tool(c.name, c.args)[0]))
                self.backend.add_results(results)
            else:
                self._add("error", "한 번의 요청에 쓸 수 있는 단계를 다 썼습니다. 이어서 하려면 다시 말씀해 주세요.")
        except Exception as e:
            self._add("error", f"AI 서버 오류: {type(e).__name__}: {e}"[:500])
            msgs = getattr(self.backend, "messages", None)
            if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "user" and isinstance(msgs[-1].get("content"), str) and not fresh:
                msgs.pop()              # the question that got no answer; the talk so far stays usable
            else:
                self.backend = None     # mid-way through tool calls: start the model's memory afresh
        finally:
            self.pending = None
            self.busy = False
            self.seq += 1


def from_settings(studio, kind="auto", **cfg):
    """A Chat for the model the environment points at (see llm.py), or a disabled one.

    kind: auto | off | anthropic | openai. auto = anthropic if ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY is set,
    openai if STEPRECON_BACKEND=openai or STEPRECON_BASE_URL is set, else off."""
    import os

    from . import llm
    if kind == "auto":
        if os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"):
            kind = "anthropic"
        elif os.environ.get("STEPRECON_BACKEND", "").lower() in ("openai", "local", "ollama") or os.environ.get("STEPRECON_BASE_URL"):
            kind = "openai"
        else:
            kind = "off"
    if kind == "off":
        return Chat(studio)
    cfg = {k: v for k, v in cfg.items() if v is not None}
    probe = llm.make_backend(kind, **cfg)       # fails here, at start, if the settings are incomplete
    host = urlparse(getattr(probe, "base_url", "") or "https://api.anthropic.com").netloc
    return Chat(studio, lambda: llm.make_backend(kind, **cfg), f"{probe.model} · {host}")
