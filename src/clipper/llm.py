"""Tiny Ollama client (stdlib only) used by /ollama_verdict and the CapCut package builder.

``chat_json`` posts to ``/api/chat`` with ``format: "json"`` and returns the parsed object.
Every failure raises :class:`OllamaError`; callers decide how to degrade (they never crash).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class OllamaError(RuntimeError):
    pass


def chat_json(base_url: str, model: str, system: str, user: str, timeout: int = 60) -> Dict[str, Any]:
    """Calls Ollama /api/chat in JSON mode and returns the parsed assistant message."""
    body = json.dumps({
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise OllamaError(f"Ollama HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
    except Exception as exc:  # URLError, timeout, connection refused
        raise OllamaError(f"Ollama unreachable at {base_url}: {exc}") from exc
    try:
        content = json.loads(raw)["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, ValueError, TypeError) as exc:
        raise OllamaError(f"Ollama returned non-JSON content: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise OllamaError("Ollama JSON was not an object")
    return parsed


def post_json(url: str, payload: Dict[str, Any], timeout: int = 90) -> Any:
    """Generic JSON POST (used for the social webhook). Raises on any failure."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def verdict_for_candidate(base_url: str, model: str, candidate: Dict[str, Any],
                          timeout: int = 60) -> Dict[str, Any]:
    """Asks Ollama for a verdict on one candidate. Returns the ``llm`` dict (raises OllamaError)."""
    pred = candidate.get("predict") or {}
    system = ("You review 9:16 gaming clips for TikTok/Shorts. Reply ONLY with JSON: "
              '{"verdict": "<one sentence>", "fix": "<one concrete edit>", '
              '"hook_line": "<<=8 word on-screen hook>", "agrees": true|false}. '
              "'agrees' means you agree with the heuristic score and biggest_fix.")
    user = json.dumps({
        "window": f"{candidate.get('start')}-{candidate.get('end')}s",
        "reason": candidate.get("reason"),
        "signals": candidate.get("signals"),
        "heuristic": {k: pred.get(k) for k in ("total", "hook", "retention", "clarity", "length_fit", "biggest_fix")},
        "lulls": pred.get("lulls"),
    })
    out = chat_json(base_url, model, system, user, timeout)
    return {
        "verdict": str(out.get("verdict", "")).strip(),
        "fix": str(out.get("fix", "")).strip(),
        "hook_line": str(out.get("hook_line", "")).strip(),
        "agrees": bool(out.get("agrees", False)),
    }
