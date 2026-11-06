"""Stage 3 — transparent moment scoring.

Two layers:

1. :func:`collect_signals` (I/O) — decodes audio, builds an RMS envelope and an onset list.
   If ``bf6_clipper.py`` (v1) is present at ``BF6_CLIPPER_PATH`` its ``extract_audio_pcm`` /
   ``compute_audio_onsets`` / ``detect_events`` are used for the action-audio signal; otherwise
   (or on any failure) the runner's own ffmpeg + RMS onset detector is used and
   ``v1_used`` is recorded as ``False``. The v1 module is never required.

2. :func:`score_moments` (pure) — slides a ``target_len`` window at 1 s steps over the
   source, snaps starts to word boundaries, scores each window from its signals, applies
   named penalties, builds a one-line ``reason`` from the strongest signals and returns the
   top-5 after non-maximum suppression (<= 25 % overlap). Pure, so tests can feed a synthetic
   transcript + RMS array without ffmpeg.

Every candidate looks like::

    {"rank": 1, "start": 123.4, "end": 153.4, "duration": 30.0, "score": 0.81,
     "reason": "loud reaction + 'no way' at 0:03",
     "signals": {"audio_energy": .., "onsets": .., "speech_density": .., "exclaim": ..,
                 "pacing": .., "peak_at": .., "v1_used": bool, "penalties": [..]},
     "penalties": [..]}
"""

from __future__ import annotations

import importlib.util
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import audio as audio_mod

# --------------------------------------------------------------------------------------
# Vocabulary (shared with the predictor so hook/score agree on what a "payoff" word is)
# --------------------------------------------------------------------------------------
PAYOFF_WORDS = {
    "no", "way", "insane", "clutch", "unreal", "crazy", "wow", "omg", "god", "what", "wait",
    "bro", "dude", "lets", "let's", "go", "yes", "finally", "ace", "headshot", "nuke", "win",
    "won", "lol", "haha", "holy", "legend", "wtf", "nooo", "noo", "yo", "oh", "dang", "damn",
}
PAYOFF_PHRASES = ["no way", "let's go", "lets go", "oh my god", "are you kidding", "what the",
                  "did that just", "i can't believe", "holy", "look at this", "watch this"]
SENTENCE_END = re.compile(r"[.!?]$")

TOP_N = 5
STEP_S = 1.0
HOP_S = 0.1  # RMS hop used throughout the clipper


# --------------------------------------------------------------------------------------
# Signal collection (I/O layer)
# --------------------------------------------------------------------------------------
def _load_v1(path: Path):
    """Imports bf6_clipper.py by path. Returns the module or None (never raises)."""
    try:
        if not path or not Path(path).is_file():
            return None
        spec = importlib.util.spec_from_file_location("bf6_clipper_v1", str(path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in ("extract_audio_pcm", "compute_audio_onsets", "detect_events"):
            if not callable(getattr(mod, name, None)):
                return None
        return mod
    except Exception:
        return None


def v1_available(path: Path) -> bool:
    return _load_v1(path) is not None


def collect_signals(source: Path, v1_path: Optional[Path] = None,
                    sample_rate: int = 22050) -> Dict[str, Any]:
    """Returns ``{"rms": [...], "hop_s", "onsets": [...], "events": [...], "v1_used": bool, "duration"}``.

    Always produces an RMS envelope with the runner's own decoder (needed for leading-silence
    and energy signals). Onsets come from v1 when it loads and succeeds, else from
    :func:`audio.rms_onsets`.
    """
    pcm = audio_mod.decode_pcm(source, sample_rate=sample_rate)
    rms = audio_mod.rms_envelope(pcm, sample_rate=sample_rate, hop_s=HOP_S)
    duration = len(rms) * HOP_S

    onsets: List[float] = []
    events: List[Any] = []
    v1_used = False
    mod = _load_v1(v1_path) if v1_path else None
    if mod is not None:
        try:
            v1_pcm = mod.extract_audio_pcm(str(source), start_s=0.0, duration_s=None, sample_rate=sample_rate)
            raw = mod.compute_audio_onsets(v1_pcm, sample_rate=sample_rate, onset_z=2.5)
            # v1 returns (onset_timestamps, frame_times, z_scores); older builds may return
            # just the timestamps, so accept both shapes.
            raw_onsets = raw[0] if isinstance(raw, tuple) and len(raw) == 3 else raw
            onsets = [float(t) for t in list(raw_onsets)]
            try:
                events = [_jsonable(e) for e in list(mod.detect_events(raw_onsets, min_onsets=5, merge_gap=6.0))]
            except Exception:
                events = []
            v1_used = True
        except Exception:
            onsets, events, v1_used = [], [], False
    if not v1_used:
        onsets = audio_mod.rms_onsets(rms, hop_s=HOP_S, z=2.5)

    return {"rms": rms, "hop_s": HOP_S, "onsets": onsets, "events": events,
            "v1_used": v1_used, "duration": duration}


def _jsonable(obj: Any) -> Any:
    """Best-effort conversion of v1 event objects (tuples / dataclasses / dicts) to JSON."""
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(obj).items()}
    return str(obj)


# --------------------------------------------------------------------------------------
# Pure scoring helpers
# --------------------------------------------------------------------------------------
def fmt_ts(sec: float) -> str:
    """0:03 style (minutes:seconds) used in reasons."""
    sec = max(0.0, sec)
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower())


def _is_exclaim_or_question(w: str) -> bool:
    return w.rstrip().endswith("!") or w.rstrip().endswith("?")


def _window_words(words: Sequence[Dict[str, Any]], start: float, end: float) -> List[Dict[str, Any]]:
    return [w for w in words if w["start"] >= start and w["start"] < end]


def _inside_speech(t: float, win_words: Sequence[Dict[str, Any]], margin: float = 0.03) -> bool:
    """True when time ``t`` falls inside (or within ``margin`` s of) a spoken word."""
    for w in win_words:
        if w["start"] - margin <= t <= w["end"] + margin:
            return True
    return False


# A candidate is a "strong moment" only if it has real action evidence: enough non-speech onsets,
# or a v1 firefight event, or a genuinely loud peak. Without that, the window is just talk.
STRONG_MIN_ACTION_ONSETS = 6
STRONG_MIN_ENERGY = 0.75


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _percentile(xs: Sequence[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


def snap_start(words: Sequence[Dict[str, Any]], start: float, max_shift: float = 2.0,
               lead: float = 0.3) -> Tuple[float, Optional[int]]:
    """Moves ``start`` to ``lead`` seconds before the nearest word start (within ``max_shift``).

    Returns (snapped_start, index_of_that_word or None if no word is near).
    """
    best_i, best_d = None, None
    for i, w in enumerate(words):
        d = abs(w["start"] - start)
        if d <= max_shift and (best_d is None or d < best_d):
            best_i, best_d = i, d
    if best_i is None:
        return start, None
    return max(0.0, words[best_i]["start"] - lead), best_i


def _penalties_for(words: Sequence[Dict[str, Any]], word_idx: Optional[int], start: float,
                   win_words: Sequence[Dict[str, Any]], onsets_in: int,
                   rms: Sequence[float], hop_s: float, silence_thr: float) -> List[str]:
    pens: List[str] = []
    # Mid-sentence start: previous word ends < 0.4 s before this word and it has no sentence-ending punctuation
    if word_idx is not None and word_idx > 0:
        prev = words[word_idx - 1]
        gap = words[word_idx]["start"] - prev["end"]
        if gap < 0.4 and not SENTENCE_END.search(prev["word"].strip()):
            pens.append("mid_sentence_start")
    # Leading silence > 1.5 s (no speech and low RMS)
    first_word_at = win_words[0]["start"] - start if win_words else None
    i0 = int(start / hop_s)
    i1 = int((start + 1.5) / hop_s)
    lead_rms = rms[i0:i1] if rms else []
    quiet = (not lead_rms) or _mean(lead_rms) < silence_thr
    if (first_word_at is None or first_word_at > 1.5) and quiet:
        pens.append("leading_silence")
    if not win_words and onsets_in == 0:
        pens.append("no_speech_no_onsets")
    return pens


VOICE_COMMANDS = (("clip", "it"), ("clip", "that"), ("clip", "this"), ("clipit",), ("clippit",), ("clip-it",))
VOICE_COMMAND_GAP = 1.0        # max seconds between "clip" and "it"
VOICE_COMMAND_LEAD = 0.3       # the window ends this long before the command starts (command stays out of the clip)


def find_voice_commands(words: Sequence[Dict[str, Any]]) -> List[Dict[str, float]]:
    """Times at which the streamer says "clip it" / "clip that" / "clip this" (Whisper variants included).

    Returns ``[{"start", "end", "text"}]`` per command, in time order. The clip the streamer means is the
    stretch BEFORE the command (like a replay buffer), so callers window backwards from ``start``.
    """
    ws = sorted(words, key=lambda w: w["start"])
    norm = [_norm_word(w["word"]) for w in ws]
    found: List[Dict[str, float]] = []
    i = 0
    while i < len(ws):
        hit = None
        for pat in VOICE_COMMANDS:
            n = len(pat)
            if i + n <= len(ws) and tuple(norm[i:i + n]) == pat:
                if n == 1 or (ws[i + n - 1]["start"] - ws[i]["end"]) <= VOICE_COMMAND_GAP:
                    hit = n
                    break
        if hit:
            found.append({"start": float(ws[i]["start"]), "end": float(ws[i + hit - 1]["end"]),
                          "text": " ".join(norm[i:i + hit])})
            i += hit
        else:
            i += 1
    return found


def score_moments(words: Sequence[Dict[str, Any]], rms: Sequence[float], duration: float,
                  target_len: float = 30.0, onsets: Optional[Sequence[float]] = None,
                  hop_s: float = HOP_S, v1_used: bool = False, top_n: int = TOP_N,
                  step_s: float = STEP_S, events: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Pure scoring. See module docstring for the candidate shape.

    ``events``: v1 bf6_clipper firefight clusters (``{"start","end","peak","onset_count"}``); a window
    overlapping one is action-confirmed. Each candidate carries ``strong_moment`` (bool) so the caller
    can say "nothing worth clipping" instead of recommending the least-boring stretch of chat.
    """
    onsets = list(onsets or [])
    events = [e for e in (events or []) if isinstance(e, dict) and "start" in e and "end" in e]
    words = sorted(words, key=lambda w: w["start"])
    target_len = float(target_len)
    if duration <= 0:
        return []
    if duration <= target_len:
        starts = [0.0]
        target_len = duration
    else:
        starts = [i * step_s for i in range(int((duration - target_len) / step_s) + 1)]

    rms_mean = _mean(rms)
    rms_p95 = _percentile(rms, 0.95) or 1e-6
    silence_thr = max(1e-4, rms_mean * 0.25)

    raw: List[Dict[str, Any]] = []
    seen_starts = set()

    def evaluate(start: float, widx: Optional[int], end: float) -> Dict[str, Any]:
        """Score one window [start, end). Everything below is window-local."""

        win_words = _window_words(words, start, end)
        i0, i1 = int(start / hop_s), int(end / hop_s)
        win_rms = rms[i0:i1]
        energy = _mean(win_rms) / rms_p95 if win_rms else 0.0               # ~0..1
        peak_i = max(range(len(win_rms)), key=lambda k: win_rms[k]) if win_rms else 0
        peak_at = round(peak_i * hop_s, 2)
        onsets_all = [t for t in onsets if start <= t < end]
        # An audio onset that lands inside a spoken word is almost always speech (plosives, laughs),
        # not game action. A stream-setup chat with no gameplay produced "35 action hits" this way.
        # Count only onsets that fall OUTSIDE word spans (with a small margin) as action.
        onsets_in = [t for t in onsets_all if not _inside_speech(t, win_words)]
        speech_onsets = len(onsets_all) - len(onsets_in)
        speech_density = len(win_words) / target_len                         # words / s
        exclaims = [w for w in win_words if _is_exclaim_or_question(w["word"])]
        text = " ".join(_norm_word(w["word"]) for w in win_words)
        payoff_hits = [w for w in win_words if _norm_word(w["word"]) in PAYOFF_WORDS]
        phrase_hits = [p for p in PAYOFF_PHRASES if p in text]
        # pacing: how bursty speech is (std of gaps between words) — burstier = more reaction-like
        gaps = [b["start"] - a["end"] for a, b in zip(win_words, win_words[1:])]
        pacing = (math.sqrt(_mean([(g - _mean(gaps)) ** 2 for g in gaps])) if len(gaps) > 1 else 0.0)

        exclaim_score = min(1.0, (len(exclaims) + 0.5 * len(payoff_hits) + 1.5 * len(phrase_hits)) / 6.0)
        onset_score = min(1.0, len(onsets_in) / 8.0)
        density_score = min(1.0, speech_density / 3.0)
        pacing_score = min(1.0, pacing / 1.5)

        # hook position: reward windows whose first strong moment lands early (but not at 0:00)
        first_hit = min([t - start for t in onsets_in] + [w["start"] - start for w in exclaims + payoff_hits]
                        + [peak_at if energy > 0.5 else target_len])
        hook_pos = 1.0 - max(0.0, min(1.0, (first_hit - 2.0) / target_len))

        v1_events_in = [e for e in events if float(e["end"]) > start and float(e["start"]) < end]
        # Strong = real action (non-speech onsets / v1 firefight / a loud peak) OR a genuine reaction
        # moment (payoff phrase like "no way"/"let's go" with raised energy). Plain chat is neither.
        # "Loud peak" is judged against the WHOLE VOD (peak vs global p95 AND clearly above the global
        # mean), so a flat, uniformly noisy recording cannot register as loud everywhere.
        peak_rms = max(win_rms) if win_rms else 0.0
        loud_peak = (rms_mean > 0 and peak_rms >= STRONG_MIN_ENERGY * rms_p95 and peak_rms >= 2.0 * rms_mean)
        reaction = bool(phrase_hits) and exclaim_score >= 0.6 and (loud_peak or energy >= 0.3) and rms_p95 > 1.5 * rms_mean
        strong = (len(onsets_in) >= STRONG_MIN_ACTION_ONSETS or bool(v1_events_in) or loud_peak or reaction)

        score = (0.28 * min(1.0, energy) + 0.22 * onset_score + 0.28 * exclaim_score
                 + 0.09 * density_score + 0.05 * pacing_score + 0.08 * hook_pos)
        if v1_events_in:
            score = min(1.0, score + 0.15)        # confirmed firefight: clear bonus

        pens = _penalties_for(words, widx, start, win_words, len(onsets_in), rms, hop_s, silence_thr)
        if not strong:
            pens.append("no_action")              # talk only: cannot be a highlight on its own
        for p in pens:
            score *= {"mid_sentence_start": 0.85, "leading_silence": 0.7,
                      "no_speech_no_onsets": 0.3, "no_action": 0.5}[p]

        # ---- reason: strongest signals, with clip-relative timestamps ----
        parts: List[str] = []
        ranked = sorted([("energy", min(1.0, energy)), ("onsets", onset_score),
                         ("exclaim", exclaim_score), ("density", density_score)],
                        key=lambda kv: kv[1], reverse=True)
        for name, val in ranked[:2]:
            if val < 0.15:
                continue
            if name == "energy":
                parts.append(f"loud reaction at {fmt_ts(peak_at)}")
            elif name == "onsets":
                first = onsets_in[0] - start if onsets_in else 0.0
                parts.append(f"{len(onsets_in)} action hits from {fmt_ts(first)}")
            elif name == "exclaim":
                anchor = (phrase_hits[0] if phrase_hits else
                          _norm_word(exclaims[0]["word"]) if exclaims else
                          _norm_word(payoff_hits[0]["word"]) if payoff_hits else "")
                at = (exclaims[0]["start"] if exclaims else payoff_hits[0]["start"] if payoff_hits else start) - start
                parts.append(f"'{anchor}' at {fmt_ts(at)}")
            elif name == "density":
                parts.append(f"dense talk ({speech_density:.1f} w/s)")
        if not parts:
            parts.append("quiet window" if not win_words else "steady talk")
        if v1_events_in:
            ev = v1_events_in[0]
            parts.insert(0, f"firefight detected at {fmt_ts(max(0.0, float(ev.get('peak', ev['start'])) - start))}")
        if not strong:
            parts = [p for p in parts if not p.endswith("w/s)")] or parts
            parts.append("talk only, no game action detected")
        reason = " + ".join(parts)
        if pens:
            reason += f" (−{', '.join(pens)})"

        return {
            "start": round(start, 2), "end": round(end, 2), "duration": round(end - start, 2),
            "score": round(max(0.0, min(1.0, score)), 4), "reason": reason,
            "strong_moment": bool(strong),
            "signals": {
                "audio_energy": round(energy, 4), "onsets": len(onsets_in),
                "speech_onsets": speech_onsets, "v1_events": len(v1_events_in),
                "onset_times": [round(t, 2) for t in onsets_in],
                "firefight_peak_abs": (float(v1_events_in[0].get("peak", v1_events_in[0]["start"])) if v1_events_in else None),
                "speech_density": round(speech_density, 3), "exclaim": round(exclaim_score, 4),
                "pacing": round(pacing, 3), "peak_at": peak_at, "first_hit": round(first_hit, 2),
                "hook_pos": round(hook_pos, 3), "payoff_words": len(payoff_hits),
                "phrases": phrase_hits, "v1_used": bool(v1_used), "penalties": list(pens),
            },
            "penalties": list(pens),
        }

    for s0 in starts:
        start, widx = snap_start(words, s0)
        start = min(start, max(0.0, duration - target_len))
        key = round(start, 2)
        if key in seen_starts:
            continue
        seen_starts.add(key)
        raw.append(evaluate(start, widx, start + target_len))

    # "clip it" said on stream = the streamer's own replay button: the window BEFORE the command is a
    # candidate no matter what the audio heuristics think, ranked first and marked strong.
    voice: List[Dict[str, Any]] = []
    for cmd in find_voice_commands(words):
        end = max(0.0, min(duration, cmd["start"] - VOICE_COMMAND_LEAD))
        start = max(0.0, end - target_len)
        if end - start < 5.0:
            continue
        c = evaluate(start, None, end)
        c["score"] = 1.0
        c["strong_moment"] = True
        c["penalties"] = [p for p in c["penalties"] if p != "no_action"]
        c["signals"]["penalties"] = list(c["penalties"])
        c["signals"]["voice_command"] = {"text": cmd["text"], "at": round(cmd["start"], 2)}
        c["reason"] = f"you said '{cmd['text']}' at {fmt_ts(cmd['start'])} — this is the {int(round(end - start))} s before it" + \
                      (f" + {c['reason']}" if c["reason"] else "")
        voice.append(c)

    raw.sort(key=lambda c: c["score"], reverse=True)
    picked = non_max_suppress(voice + raw, max_overlap=0.25, top_n=top_n + len(voice))
    for i, c in enumerate(picked, 1):
        c["rank"] = i
    return picked


def overlap_fraction(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    shortest = max(1e-9, min(a["end"] - a["start"], b["end"] - b["start"]))
    return inter / shortest


def non_max_suppress(cands: Sequence[Dict[str, Any]], max_overlap: float = 0.25,
                     top_n: int = TOP_N) -> List[Dict[str, Any]]:
    """Greedy NMS on score-sorted windows: keep a window only if it overlaps every kept one <= max_overlap."""
    kept: List[Dict[str, Any]] = []
    for c in cands:
        if all(overlap_fraction(c, k) <= max_overlap for k in kept):
            kept.append(c)
        if len(kept) >= top_n:
            break
    return kept
