"""Stage 6B — CapCut hand-off package.

Creates ``READY\\<original>_clip<rank>_<len>s_capcut\\`` containing:

- the rendered clip (copied)
- ``captions.srt`` (clip-relative, from the transcript words)
- ``transcript.json`` (copied from the job)
- ``editpilot-prompts.md`` — Ollama-written (JSON mode, 60 s) prompts for CapCut EditPilot,
  with a deterministic heuristic fallback that still quotes THIS clip's timestamps/hook/fix
- ``capcut-steps.md`` — static import/edit steps, filled with the clip path
- ``social.md`` — titles / caption / hashtags from the ``smartergamers-clip`` n8n webhook
  (90 s timeout); a note is written instead when it is unreachable
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import captions as captions_mod
from .config import ClipperConfig
from .llm import OllamaError, chat_json, post_json


def _fmt(sec: float) -> str:
    return f"{int(max(0, sec) // 60)}:{int(max(0, sec) % 60):02d}"


def package_dir_for(cfg: ClipperConfig, original_stem: str, rank: int, length_s: int) -> Path:
    return cfg.READY / f"{original_stem}_clip{rank}_{length_s}s_capcut"


# ------------------------------------------------------------------ editpilot-prompts.md
def _heuristic_prompts(candidate: Dict[str, Any], clip_rel_words: Sequence[Dict[str, Any]],
                       duration: float, why: str) -> str:
    """Deterministic EditPilot prompts built only from this clip's data."""
    pred = candidate.get("predict") or {}
    lulls = pred.get("lulls") or []
    fix = pred.get("biggest_fix") or "tighten the open"
    reason = candidate.get("reason") or "strongest audio/transcript moment"
    first_words = " ".join(w["word"] for w in clip_rel_words[:6]).strip() or "(no speech)"
    hook_line = first_words[:40]
    peak = (candidate.get("signals") or {}).get("peak_at", 0.0)

    lines = [
        "# EditPilot prompts",
        "",
        f"_{why}_",
        "",
        f"Clip: {_fmt(0)} → {_fmt(duration)} ({duration:.0f} s). Picked because: {reason}.",
        f"Heuristic score {pred.get('total', '?')}/100 — biggest fix: {fix}.",
        "",
        "## 1. Hook text",
        f'Add bold white on-screen text at 0:00–0:02 reading "{hook_line}" (3 px black outline, '
        "placed at 62 % height, clear of the right-hand buttons).",
        "",
        "## 2. Trim",
    ]
    if lulls:
        for l in lulls[:3]:
            lines.append(f"- Cut the dead air from {_fmt(l['start'])} to {_fmt(l['end'])} "
                         f"({l['end'] - l['start']:.1f} s).")
    else:
        lines.append("- No dead air detected; keep the full window.")
    lines += [
        "",
        "## 3. Emphasis",
        f"- Add a 1.1x punch-in zoom at {_fmt(peak)} (loudest moment) for 0.5 s.",
        "- Auto-captions: 3 words max per line, bold, yellow highlight on the current word.",
        "",
        "## 4. Fix",
        f"- {fix}",
        "",
        "## 5. End card",
        f"- At {_fmt(max(0.0, duration - 2))} add 'Follow for more' (1.5 s) — keep it above the bottom 25 %.",
        "",
    ]
    return "\n".join(lines)


def build_editpilot_prompts(cfg: ClipperConfig, candidate: Dict[str, Any],
                            clip_rel_words: Sequence[Dict[str, Any]], duration: float) -> str:
    """Build editpilot-prompts.md.

    The deterministic prompts (``_heuristic_prompts``) are ALWAYS written first: their
    timestamps come straight from the clip's own lull/peak data, so they are never generic.
    Ollama is then asked for extra, imperative editing instructions. Each LLM line is kept
    only if it is an editing instruction that cites a concrete clip timestamp; anything
    else (captions-style filler, vague advice) is dropped, and if fewer than two survive
    the file says so and carries the heuristic prompts alone.
    """
    pred = candidate.get("predict") or {}
    base = _heuristic_prompts(candidate, clip_rel_words, duration,
                              why="Deterministic prompts from this clip's own timestamps (always included).")
    transcript = " ".join(w["word"] for w in clip_rel_words)[:1200]
    first_words = " ".join(w["word"] for w in clip_rel_words[:8]).strip()
    system = (
        "You write edit instructions for CapCut's EditPilot for ONE 9:16 gaming Short. "
        "Each instruction is one imperative sentence an editor executes on the timeline "
        "(trim, cut, zoom, add text, speed-ramp, duck audio, add transition, add sound effect), "
        "and MUST name a clip timestamp as m:ss taken from the data (lulls, peak_at, hook, first words). "
        "Never write captions, hype lines, or descriptions of the clip. "
        'Reply ONLY with JSON: {"hook_text": "<on-screen hook, <=6 words, quoting or paraphrasing the first words>", '
        '"prompts": ["<instruction 1>", "<instruction 2>", "<instruction 3>", "<instruction 4>"]}'
    )
    user = json.dumps({
        "duration_s": round(duration, 1),
        "first_words_0_to_2s": first_words,
        "picked_because": candidate.get("reason"),
        "biggest_fix": pred.get("biggest_fix"),
        "lulls_to_cut": [{"start": _fmt(l["start"]), "end": _fmt(l["end"])} for l in (pred.get("lulls") or [])[:3]],
        "peak_at": _fmt((candidate.get("signals") or {}).get("peak_at", 0.0)),
        "score": {k: pred.get(k) for k in ("total", "hook", "retention", "clarity", "length_fit")},
        "transcript": transcript,
    })
    verbs = ("trim", "cut", "zoom", "punch", "add", "duck", "lower", "raise", "speed", "slow",
             "freeze", "transition", "insert", "remove", "keep", "mute", "boost", "overlay", "place")
    ts_re = re.compile(r"\b\d{1,2}:\d{2}\b")
    try:
        out = chat_json(cfg.OLLAMA_URL, cfg.OLLAMA_MODEL, system, user, timeout=cfg.OLLAMA_TIMEOUT_SEC)
        raw = out.get("prompts") if isinstance(out, dict) else None
        if not isinstance(raw, list):
            raise OllamaError("Ollama JSON had no 'prompts' list")
        kept, dropped = [], 0
        for p in raw:
            text = str(p).strip().strip("<>").strip()
            low = text.lower()
            if ts_re.search(text) and any(v in low for v in verbs) and len(text) > 20:
                kept.append(text)
            else:
                dropped += 1
        hook = str(out.get("hook_text") or "").strip().strip("<>")
        extra = ["", f"## Extra suggestions from {cfg.OLLAMA_MODEL}"]
        if hook:
            extra.append(f'- Alternative hook text: "{hook[:60]}"')
        if len(kept) >= 2:
            extra += [f"- {t}" for t in kept]
            if dropped:
                extra.append(f"- _({dropped} suggestion(s) dropped: not a timestamped edit instruction.)_")
        else:
            extra.append(f"- _Ollama returned {len(raw)} line(s) but only {len(kept)} were timestamped edit "
                         "instructions, so only the deterministic prompts above are offered._")
        return base + "\n".join(extra) + "\n"
    except OllamaError as exc:
        return base + f"\n## Extra suggestions\n- _Ollama failed ({exc}); only the deterministic prompts above are offered._\n"


# ------------------------------------------------------------------ capcut-steps.md
def build_capcut_steps(clip_path: Path, srt_path: Path, prompts_path: Path) -> str:
    return f"""# CapCut steps

1. Open CapCut → **New project** → **Import** → `{clip_path}`.
2. Drag the clip onto the timeline. Ratio is already 9:16 (1080x1920); do not re-crop.
3. **Text → Auto captions** is optional: the clip already has burned-in captions. If you want
   editable ones instead, re-render without `--captions` and import `{srt_path}`
   (Text → Import subtitles).
4. Open `{prompts_path}` and paste each prompt into **EditPilot** (AI assistant) one at a time,
   top to bottom. Review each change before applying the next.
5. Keep all text inside the safe zone: above the bottom 25 % and at least 120 px from the right edge.
6. **Export** → 1080x1920, 60 fps if the source was 60, H.264, "High" bitrate → save beside this file.
7. Copy the title / caption / hashtags from `social.md` when uploading.
"""


# ------------------------------------------------------------------ social.md
def build_social(cfg: ClipperConfig, title: str, game: str, channel: str, duration: float,
                 description: str) -> str:
    payload = {"title": title, "game": game, "channel": channel,
               "duration_seconds": round(duration, 1), "description": description}
    try:
        resp = post_json(cfg.SOCIAL_WEBHOOK_URL, payload, timeout=cfg.SOCIAL_TIMEOUT_SEC)
    except Exception as exc:
        return (f"# Social copy\n\n_Could not reach the `smartergamers-clip` webhook at "
                f"{cfg.SOCIAL_WEBHOOK_URL}: {exc}_\n\nRequest that was sent:\n\n```json\n"
                f"{json.dumps(payload, indent=2)}\n```\n\nWrite the title / caption / hashtags by hand, "
                f"or re-run **/package** once n8n is up.\n")
    body = ["# Social copy", "", f"Source: `{cfg.SOCIAL_WEBHOOK_URL}`", ""]
    if isinstance(resp, dict):
        for key in ("titles", "title", "caption", "description", "hashtags", "tags"):
            if key in resp:
                val = resp[key]
                body.append(f"## {key}")
                if isinstance(val, list):
                    body += [f"- {v}" for v in val]
                else:
                    body.append(str(val))
                body.append("")
        extra = {k: v for k, v in resp.items() if k not in ("titles", "title", "caption", "description", "hashtags", "tags")}
        if extra:
            body += ["## raw", "```json", json.dumps(extra, indent=2, ensure_ascii=False), "```", ""]
    else:
        body += ["```", str(resp), "```", ""]
    return "\n".join(body)


# ------------------------------------------------------------------ main entry
def build_package(cfg: ClipperConfig, job_dir: Path, output: Path, rank: int, candidate: Dict[str, Any],
                  original_stem: str, words: Sequence[Dict[str, Any]], game: str = "gameplay",
                  channel: str = "SmarterGamers") -> Dict[str, Any]:
    """Builds the package directory and returns ``{"package_dir", "files"}``."""
    start = float(candidate.get("start", 0.0))
    end = float(candidate.get("end", start))
    duration = max(0.0, end - start)
    pkg = package_dir_for(cfg, original_stem, rank, int(round(duration)))
    pkg.mkdir(parents=True, exist_ok=True)
    files: List[str] = []

    clip_dst = pkg / Path(output).name
    if Path(output).resolve() != clip_dst.resolve():
        shutil.copy2(output, clip_dst)
    files.append(str(clip_dst))

    chunks = captions_mod.caption_chunks(words, start, end)
    srt_path = pkg / "captions.srt"
    srt_path.write_text(captions_mod.build_srt(chunks), encoding="utf-8")
    files.append(str(srt_path))

    tj_src = Path(job_dir) / "transcript.json"
    tj_dst = pkg / "transcript.json"
    if tj_src.exists():
        shutil.copy2(tj_src, tj_dst)
    else:
        tj_dst.write_text(json.dumps({"segments": []}), encoding="utf-8")
    files.append(str(tj_dst))

    rel_words = [{"start": w["start"] - start, "end": w["end"] - start, "word": w["word"]}
                 for w in words if start <= w["start"] < end]
    prompts_path = pkg / "editpilot-prompts.md"
    prompts_path.write_text(build_editpilot_prompts(cfg, candidate, rel_words, duration), encoding="utf-8")
    files.append(str(prompts_path))

    steps_path = pkg / "capcut-steps.md"
    steps_path.write_text(build_capcut_steps(clip_dst, srt_path, prompts_path), encoding="utf-8")
    files.append(str(steps_path))

    title = f"{original_stem} — {candidate.get('reason', 'clip')}"[:90]
    description = " ".join(w["word"] for w in rel_words)[:500] or candidate.get("reason", "")
    social_path = pkg / "social.md"
    social_path.write_text(build_social(cfg, title, game, channel, duration, description), encoding="utf-8")
    files.append(str(social_path))

    return {"package_dir": str(pkg), "files": files}
