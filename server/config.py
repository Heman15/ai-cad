# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI provider settings, stored outside the repository with owner-only permissions.

Location: $AI_CAD_CONFIG or ~/.config/ai-cad/config.json. The API key is never
returned by the HTTP API and never logged. ANTHROPIC_API_KEY in the environment
overrides the stored key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULTS = {"provider": "anthropic", "model": "claude-opus-5-5", "base_url": None, "effort": "medium", "claude_path": None}

# Presets for the settings dialog. "openai" means any OpenAI-compatible chat-completions API.
# Model names change over time; they are editable defaults, check each provider's model list.
PRESETS = {
    "claude_code": {"provider": "claude_code", "base_url": None, "model": "", "needs_key": False,
                    "note": "Uses your own Claude Code login (VS Code extension or CLI) — no API key. Model blank = your Claude Code default."},
    "claude": {"provider": "anthropic", "base_url": None, "model": "claude-opus-5-5", "needs_key": True,
               "note": "Anthropic API key from console.anthropic.com"},
    "openai": {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "needs_key": True,
               "note": "OpenAI API key"},
    "gemini": {"provider": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-2.0-flash",
               "needs_key": True, "note": "Google AI Studio key (has a free tier)"},
    "groq": {"provider": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile", "needs_key": True,
             "note": "Groq key (free tier); text only, no drawing images"},
    "openrouter": {"provider": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b-instruct:free",
                   "needs_key": True, "note": "OpenRouter key; models ending in :free cost nothing"},
    "ollama": {"provider": "openai", "base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b", "needs_key": False,
               "note": "Free, runs on this computer (install Ollama, then `ollama pull qwen2.5:7b`); needs a model with tool calling"},
    "custom": {"provider": "openai", "base_url": "", "model": "", "needs_key": False, "note": "Any OpenAI-compatible endpoint"},
}


def config_path() -> Path:
    return Path(os.environ.get("AI_CAD_CONFIG", Path.home() / ".config" / "ai-cad" / "config.json"))


def load() -> dict[str, Any]:
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save(updates: dict[str, Any]) -> None:
    data = {k: v for k, v in load().items() if k in (*DEFAULTS, "api_key")}
    switching = (updates.get("provider") and updates["provider"] != data.get("provider")) or \
        (updates.get("base_url") is not None and (updates["base_url"] or None) != data.get("base_url"))
    if switching and not updates.get("api_key"):
        data.pop("api_key", None)  # never send one service's key to another service
    for k, v in updates.items():
        if k in ("api_key", "base_url") and v == "":
            data[k] = None  # empty string clears the stored value
            if k == "api_key":
                data.pop("api_key", None)
        elif k in (*DEFAULTS, "api_key") and v is not None:
            data[k] = v
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def api_key() -> str | None:
    c = load()
    if c.get("provider") == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY") or c.get("api_key")
    return c.get("api_key") or os.environ.get("OPENAI_API_KEY")


def public() -> dict[str, Any]:
    """Settings safe to show in the UI (no secrets)."""
    c = load()
    key = api_key()
    return {
        "provider": c["provider"], "model": c["model"], "base_url": c["base_url"], "effort": c["effort"],
        "has_key": bool(key), "key_source": ("config file" if c.get("api_key") else "environment" if key else None),
        "config_path": str(config_path()), "presets": PRESETS,
        # local servers (Ollama, LM Studio) need no key
        "ready": bool(key) or c["provider"] == "claude_code"
        or (c["provider"] == "openai" and any(h in str(c.get("base_url") or "") for h in ("localhost", "127.0.0.1"))),
    }
