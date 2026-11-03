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
        "# EditPilot prompts (heuristic fallback)",
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
    pred = candidate.get("predict") or {}
    transcript = " ".join(w["word"] for w in clip_rel_words)[:1200]
    system = ("You write CapCut EditPilot prompts for a 9:16 gaming Short. Reply ONLY with JSON: "
              '{"hook_text": "<=8 words", "prompts": ["<prompt 1>", "<prompt 2>", "<prompt 3>", "<prompt 4>"], '
              '"notes": "<one sentence>"}. Every prompt must reference a concrete timestamp from the data.')
    user = json.dumps({
        "duration_s": round(duration, 1),
        "reason": candidate.get("reason"),
        "biggest_fix": pred.get("biggest_fix"),
        "lulls": pred.get("lulls"),
        "score": {k: pred.get(k) for k in ("total", "hook", "retention", "clarity", "length_fit")},
        "transcript": transcript,
    })
    try:
        out = chat_json(cfg.OLLAMA_URL, cfg.OLLAMA_MODEL, system, user, timeout=cfg.OLLAMA_TIMEOUT_SEC)
        prompts = out.get("prompts") or []
        if not isinstance(prompts, list) or not prompts:
            raise OllamaError("Ollama JSON had no 'prompts' list")
        body = [f"# EditPilot prompts ({cfg.OLLAMA_MODEL})", "",
                f"Clip: 0:00 → {_fmt(duration)} ({duration:.0f} s). Why: {candidate.get('reason')}",
                f"Biggest fix (heuristic): {pred.get('biggest_fix')}", "",
                f'**Hook text:** "{out.get("hook_text", "")}"', ""]
        for i, p in enumerate(prompts, 1):
            body.append(f"{i}. {p}")
        if out.get("notes"):
            body += ["", f"_Note: {out['notes']}_"]
        body.append("")
        return "\n".join(body)
    except OllamaError as exc:
        return _heuristic_prompts(candidate, clip_rel_words, duration,
                                  why=f"Ollama failed ({exc}); these prompts were generated heuristically from the clip data.")


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
                  original_stem: str, words: Sequence[Dict[str, Any]], game: str = "Battlefield 6",
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
