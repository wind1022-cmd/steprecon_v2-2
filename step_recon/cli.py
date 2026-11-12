"""Command-line options shared by run.py and eval_batch.py (choose Claude API or a local model)."""
import os

from . import llm


def add_llm_args(ap):
    g = ap.add_argument_group("LLM backend")
    g.add_argument("--backend", default=os.environ.get("STEPRECON_BACKEND", "anthropic"),
                   choices=["anthropic", "openai"],
                   help="anthropic = Claude API or a gateway speaking the Anthropic protocol (ANTHROPIC_BASE_URL + "
                        "ANTHROPIC_AUTH_TOKEN); openai = any OpenAI-compatible server (Ollama, LM Studio, vLLM...)")
    g.add_argument("--model", default=None, help="model name (default: claude-opus-5-5 for anthropic, qwen3:8b for openai/Ollama)")
    g.add_argument("--base-url", default=None,
                   help="openai: default http://localhost:11434/v1 (Ollama). LM Studio: http://localhost:1234/v1, "
                        "vLLM: http://localhost:8000/v1. anthropic: gateway address (default: ANTHROPIC_BASE_URL)")
    g.add_argument("--tool-mode", default="auto", choices=["auto", "native", "text"],
                   help="openai backend: native function calling, JSON-in-text protocol, or auto-detect (default)")
    g.add_argument("--profile", default=None, choices=["full", "basic"],
                   help="tool set: full (24 tools, long prompt) or basic (9 tools, short prompt for small local "
                        "models). Default: full for anthropic, basic for openai")
    g.add_argument("--max-faces-prompt", type=int, default=None,
                   help="faces listed in the prompt (default 80; 30 is kinder to small context windows)")
    g.add_argument("--temperature", type=float, default=0.2, help="openai backend sampling temperature")
    g.add_argument("--reasoning-effort", default=None, choices=["low", "medium", "high"],
                   help="openai backend, reasoning models (gpt-oss...): how long the model thinks per turn. "
                        "low is much faster")
    g.add_argument("--max-tokens", type=int, default=None,
                   help="openai backend: output limit per turn incl. reasoning (default 4096)")
    return g


def backend_from_args(a):
    if a.backend == "anthropic":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise SystemExit("ANTHROPIC_API_KEY is not set (gateway: ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL; "
                             "or use --backend openai with a local model)")
        return llm.make_backend("anthropic", model=a.model, base_url=a.base_url, max_tokens=a.max_tokens)
    return llm.make_backend("openai", model=a.model, base_url=a.base_url, tool_mode=a.tool_mode,
                            temperature=a.temperature, reasoning_effort=a.reasoning_effort,
                            max_tokens=a.max_tokens)


def run_kwargs(a):
    model = a.model or os.environ.get("STEPRECON_MODEL") or os.environ.get("ANTHROPIC_MODEL") or ""
    claude = a.backend == "anthropic" and (not model or "claude" in model.lower())
    profile = a.profile or ("full" if claude else "basic")      # other models behind a gateway start small
    kw = {"profile": profile}
    if a.max_faces_prompt:
        kw["max_faces"] = a.max_faces_prompt
    elif a.backend == "openai":
        kw["max_faces"] = 40
    return kw
