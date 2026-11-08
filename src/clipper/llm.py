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
        "think": False,                      # qwen3 / nemotron: skip the thinking pass (JSON only, fast)
        "options": {"temperature": 0.2, "num_predict": 320},
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


VERDICT_SYSTEM = (
    "You are a short-form gaming video editor reviewing one candidate clip for TikTok/YouTube Shorts. "
    "You get a plain-text description of the clip. Answer with ONE JSON object only, no prose, with exactly these keys: "
    "verdict (one plain-English sentence: what works or what is wrong), "
    "fix (one concrete edit, with a timestamp when relevant), "
    "hook_line (on-screen text hook, max 8 words, specific to what happens or what is said; never generic like "
    "'Firefight detected'), "
    "title (YouTube Shorts title, max 60 chars, specific, no emojis, never the words clip/9:16/detected), "
    "agrees (true if you agree with the heuristic score and its biggest_fix, else false). "
    "'gunfire/impact' in the audio analysis means loud game sounds, not literal fire. "
    "The hook and title must fit the GAME given: never shooter words (gunfire, grenade, sniper, weapon, firefight) "
    "for racing, flight, sports, ship or coding videos, and never the filler phrase 'live stream'."
)


def verdict_for_candidate(base_url: str, model: str, candidate: Dict[str, Any],
                          timeout: int = 60, spoken: str = "", game: str = "",
                          shooter: bool = True) -> Dict[str, Any]:
    """Asks Ollama for a verdict on one candidate. Returns the ``llm`` dict (raises OllamaError).

    The prompt is labelled plain text, not JSON: small models under ``format: json`` tend to echo a JSON user
    turn back verbatim. ``spoken`` (the streamer's words inside the window) is what makes hooks and titles
    specific instead of a paraphrase of the signal string.
    """
    pred = candidate.get("predict") or {}
    reason = str(candidate.get("reason") or "").replace("firefight detected", "gunfire/impact")
    if not shooter:   # a loud crash in a racing/flight/sports game is not gunfire
        reason = reason.replace("gunfire/impact", "loud game moment")
    user = (
        f"GAME: {game or 'unknown (a live gaming stream)'}.\n"
        f"CLIP WINDOW: {candidate.get('start')}-{candidate.get('end')}s of a live stream recording.\n"
        f"WHAT THE AUDIO ANALYSIS FOUND: {reason}.\n"
        f"WHAT THE STREAMER SAYS IN THIS WINDOW: {spoken.strip() or '(nothing - no speech)'}\n"
        f"HEURISTIC SCORE: {pred.get('total')}/100 (hook {pred.get('hook')}, retention {pred.get('retention')}, "
        f"clarity {pred.get('clarity')}); its biggest_fix: {pred.get('biggest_fix')}.\n"
        "Now give the JSON object."
    )
    out = chat_json(base_url, model, VERDICT_SYSTEM, user, timeout)
    title = str(out.get("title", "")).strip()
    return {
        "verdict": str(out.get("verdict", "")).strip(),
        "fix": str(out.get("fix", "")).strip(),
        "hook_line": str(out.get("hook_line", "")).strip(),
        "title": title[:100],
        "agrees": bool(out.get("agrees", False)),
    }
