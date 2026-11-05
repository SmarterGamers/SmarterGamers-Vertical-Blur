"""Stage 4 — rule-based clip performance predictor (pure functions, no ffmpeg).

Inputs are the transcript words (absolute timestamps), the RMS envelope and a candidate
window. Output per candidate::

    {"total": 0-100, "hook": 0-40, "retention": 0-30, "clarity": 0-20, "length_fit": 0-10,
     "lulls": [{"start","end"}], "biggest_fix": "...", "notes": {...}}

Scoring rules
-------------
hook (0-40)       first 2 s: question / payoff / surprise / stakes words, exclamation marks,
                  raised audio energy vs. the clip mean; penalise silence and
                  "hey guys welcome back"-style intros.
retention (0-30)  dead air > 1.2 s (each lull subtracts, timestamps reported), filler words
                  (um / uh / like), long stretches without any audio energy.
clarity (0-20)    speech-to-game-audio ratio proxy (RMS during words vs. outside words),
                  caption line length (<= 3 words per chunk is ideal), 9:16 subject visible
                  (True when the source is 16:9 landscape).
length_fit (0-10) 15-35 s is best; > 60 s flagged unless the target is 60.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

HOOK_WINDOW_S = 2.0
DEAD_AIR_S = 1.2
FILLERS = {"um", "uh", "umm", "uhh", "er", "hmm", "like", "literally", "basically"}
QUESTION_WORDS = {"what", "why", "how", "wait", "who", "where", "did", "can", "is", "are", "really"}
PAYOFF_WORDS = {"no", "way", "insane", "clutch", "unreal", "crazy", "wow", "omg", "god", "holy",
                "nuke", "ace", "headshot", "win", "won", "finally", "legend", "wtf", "dude", "bro"}
STAKES_WORDS = {"last", "final", "one", "only", "never", "first", "ever", "chance", "lose", "dead",
                "hp", "seconds", "left", "round", "clutch", "overtime", "match", "point"}
INTRO_PATTERNS = [
    r"\bhey guys\b", r"\bwelcome back\b", r"\bwhat'?s up (guys|everyone|everybody)\b",
    r"\bin today'?s video\b", r"\bbefore we (start|begin)\b", r"\bdon'?t forget to\b",
    r"\bhit (the|that) (like|subscribe)\b", r"\bso (today|yeah) we\b",
]


def _norm(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower())


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _fmt(sec: float) -> str:
    return f"{int(max(0, sec) // 60)}:{int(max(0, sec) % 60):02d}"


def clip_words(words: Sequence[Dict[str, Any]], start: float, end: float) -> List[Dict[str, Any]]:
    """Clip-relative words inside [start, end)."""
    out = []
    for w in sorted(words, key=lambda w: w["start"]):
        if start <= w["start"] < end:
            out.append({"start": round(w["start"] - start, 3),
                        "end": round(min(w["end"], end) - start, 3), "word": w["word"]})
    return out


# ------------------------------------------------------------------ hook
def score_hook(rel_words: Sequence[Dict[str, Any]], rel_rms: Sequence[float],
               hop_s: float = 0.1, action: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    """0-40. Looks only at the first HOOK_WINDOW_S seconds.

    ``action`` (optional) = ``{"onsets": [clip-relative seconds], "firefight_at": seconds|None}`` from the
    moment scorer. For gameplay a fight opening cold on gunfire IS a hook, so action in the first
    2 s earns points even with no commentary, and a silent-but-active open is not penalised.
    """
    first = [w for w in rel_words if w["start"] < HOOK_WINDOW_S]
    act = action or {}
    early_hits = [t for t in (act.get("onsets") or []) if 0.0 <= t < HOOK_WINDOW_S]
    ff_at = act.get("firefight_at")
    action_open = bool(early_hits) or (ff_at is not None and ff_at < 4.0)
    text = " ".join(_norm(w["word"]) for w in first)
    toks = [_norm(w["word"]) for w in first]
    notes: Dict[str, Any] = {}

    pts = 8.0  # base
    q = sum(1 for t in toks if t in QUESTION_WORDS) + sum(1 for w in first if w["word"].strip().endswith("?"))
    p = sum(1 for t in toks if t in PAYOFF_WORDS)
    s = sum(1 for t in toks if t in STAKES_WORDS)
    ex = sum(1 for w in first if w["word"].strip().endswith("!"))
    pts += min(10, 5 * q)
    pts += min(12, 6 * p)
    pts += min(6, 3 * s)
    pts += min(8, 4 * ex)

    # raised energy in the hook vs the clip mean
    n = int(HOOK_WINDOW_S / hop_s)
    hook_rms = _mean(rel_rms[:n])
    clip_rms = _mean(rel_rms) or 1e-9
    ratio = hook_rms / clip_rms if rel_rms else 1.0
    if ratio >= 1.3:
        pts += 6
    elif ratio >= 1.1:
        pts += 3
    notes["hook_energy_ratio"] = round(ratio, 3)

    # action in the hook window (gameplay clips): up to +16
    if action_open:
        pts += min(16, 8 + 4 * len(early_hits))
        notes["action_open"] = {"early_hits": len(early_hits), "firefight_at": ff_at}

    # penalties — silence is only a problem when there is no action either
    if not first and not action_open:
        pts -= 10
        notes["silent_open"] = True
    elif first and first[0]["start"] > 1.0 and not action_open:
        pts -= 5
        notes["late_first_word"] = first[0]["start"]
    intro_hit = next((pat for pat in INTRO_PATTERNS if re.search(pat, text)), None)
    if intro_hit:
        pts -= 14
        notes["generic_intro"] = intro_hit
    filler_open = sum(1 for t in toks[:3] if t in FILLERS)
    if filler_open:
        pts -= 3 * filler_open
        notes["filler_open"] = filler_open

    notes.update({"question": q, "payoff": p, "stakes": s, "exclaim": ex, "hook_text": text})
    return int(max(0, min(40, round(pts)))), notes


# ------------------------------------------------------------------ retention
def find_lulls(rel_words: Sequence[Dict[str, Any]], rel_rms: Sequence[float], duration: float,
               hop_s: float = 0.1, dead_air_s: float = DEAD_AIR_S,
               action_onsets: Optional[Sequence[float]] = None) -> List[Dict[str, float]]:
    """Gaps > ``dead_air_s`` with no speech AND low audio energy (clip-relative)."""
    clip_mean = _mean(rel_rms) if rel_rms else 0.0
    edges = [0.0] + [w["start"] for w in rel_words] + [duration]
    ends = [0.0] + [w["end"] for w in rel_words] + [duration]
    lulls: List[Dict[str, float]] = []
    # candidate gaps between consecutive words (and clip edges)
    onsets = sorted(action_onsets or [])
    for i in range(1, len(edges)):
        gap_start = ends[i - 1]
        gap_end = edges[i]
        if gap_end - gap_start <= dead_air_s:
            continue
        if any(gap_start <= t <= gap_end for t in onsets):
            continue  # gunfire / impacts fill the gap: not dead air
        if rel_rms:
            seg = rel_rms[int(gap_start / hop_s): int(gap_end / hop_s)]
            if seg and clip_mean > 0 and _mean(seg) > clip_mean * 2.0:
                # clearly louder than the clip average: gameplay fills the gap -> not dead air
                continue
        lulls.append({"start": round(gap_start, 2), "end": round(gap_end, 2)})
    return lulls


def score_retention(rel_words: Sequence[Dict[str, Any]], rel_rms: Sequence[float], duration: float,
                    hop_s: float = 0.1, action_onsets: Optional[Sequence[float]] = None
                    ) -> Tuple[int, List[Dict[str, float]], Dict[str, Any]]:
    """0-30. Dead air, filler words and energy lulls. Game action counts as content, not dead air."""
    pts = 30.0
    lulls = find_lulls(rel_words, rel_rms, duration, hop_s, action_onsets=action_onsets)
    dead_total = sum(l["end"] - l["start"] for l in lulls)
    pts -= min(18, 4 * len(lulls) + 2 * dead_total)
    fillers = [w for w in rel_words if _norm(w["word"]) in FILLERS]
    pts -= min(8, 1.5 * len(fillers))
    notes = {"dead_air_sec": round(dead_total, 2), "lull_count": len(lulls),
             "fillers": len(fillers), "filler_at": [round(w["start"], 2) for w in fillers][:5]}
    if not rel_words and not rel_rms:
        pts = 5
    return int(max(0, min(30, round(pts)))), lulls, notes


# ------------------------------------------------------------------ clarity
def score_clarity(rel_words: Sequence[Dict[str, Any]], rel_rms: Sequence[float], hop_s: float = 0.1,
                  source_w: int = 1920, source_h: int = 1080,
                  max_caption_words: int = 3) -> Tuple[int, Dict[str, Any]]:
    """0-20. Speech-vs-game loudness proxy, caption chunk length, 9:16 subject visibility."""
    pts = 0.0
    notes: Dict[str, Any] = {}
    # speech / non-speech RMS ratio
    if rel_rms and rel_words:
        in_word = [False] * len(rel_rms)
        for w in rel_words:
            for i in range(int(w["start"] / hop_s), min(len(rel_rms), int(w["end"] / hop_s) + 1)):
                in_word[i] = True
        speech = [r for r, f in zip(rel_rms, in_word) if f]
        other = [r for r, f in zip(rel_rms, in_word) if not f]
        ratio = (_mean(speech) / _mean(other)) if (speech and other and _mean(other) > 0) else 1.5
        notes["speech_to_game_ratio"] = round(ratio, 3)
        pts += 10 if ratio >= 1.4 else 7 if ratio >= 1.1 else 4 if ratio >= 0.9 else 1
    elif rel_words:
        pts += 6
    else:
        notes["no_speech"] = True
    # caption line length: words per chunk (3-word chunks are ideal)
    if rel_words:
        n_chunks = max(1, -(-len(rel_words) // max_caption_words))
        avg_chars = _mean([len(w["word"]) for w in rel_words])
        notes["caption_chunks"] = n_chunks
        pts += 5 if avg_chars <= 7 else 3
    # 9:16 subject visible: a 16:9 source fits the centre band of the vertical canvas; a source that is
    # already vertical (OBS "nontsuru" 1080x1920 layout) needs no reframing, so it gets full marks too.
    aspect = (source_w / source_h) if (source_w > 0 and source_h > 0) else 16 / 9
    landscape = aspect >= 1.5
    vertical = aspect <= 0.75
    notes["subject_visible_916"] = bool(landscape or vertical)
    if vertical:
        notes["source_already_vertical"] = True
    pts += 5 if (landscape or vertical) else 2
    return int(max(0, min(20, round(pts)))), notes


# ------------------------------------------------------------------ length
def score_length(duration: float, target_len: float = 30.0) -> Tuple[int, Dict[str, Any]]:
    """0-10. 15-35 s best; > 60 s flagged unless target is 60."""
    notes: Dict[str, Any] = {}
    if 15 <= duration <= 35:
        pts = 10
    elif 10 <= duration < 15 or 35 < duration <= 45:
        pts = 7
    elif 45 < duration <= 60:
        pts = 5 if target_len < 60 else 9
    elif duration > 60:
        pts = 2 if target_len < 60 else 6
        if target_len < 60:
            notes["flag"] = "over 60 s"
    else:
        pts = 3
    return pts, notes


# ------------------------------------------------------------------ assemble
def biggest_fix(hook: int, retention: int, clarity: int, length_fit: int,
                lulls: Sequence[Dict[str, float]], hook_notes: Dict[str, Any],
                ret_notes: Dict[str, Any], clar_notes: Dict[str, Any], rel_words: Sequence[Dict[str, Any]],
                duration: float) -> str:
    """One concrete, clip-specific suggestion from the weakest dimension."""
    deficits = [("hook", 40 - hook), ("retention", 30 - retention), ("clarity", 20 - clarity),
                ("length", 10 - length_fit)]
    worst = max(deficits, key=lambda kv: kv[1])[0]
    if worst == "hook":
        if hook_notes.get("generic_intro"):
            first_payoff = next((w for w in rel_words if _norm(w["word"]) in PAYOFF_WORDS | QUESTION_WORDS), None)
            if first_payoff:
                return f"cut the first {first_payoff['start']:.0f} s — the hook starts at {_fmt(first_payoff['start'])}"
            return "cut the intro — open on the reaction, not the greeting"
        if hook_notes.get("silent_open") or hook_notes.get("late_first_word"):
            if rel_words:
                return f"start {rel_words[0]['start']:.1f} s later so the first word lands at 0:00"
            return "no speech in the first 2 s — start on a spoken reaction or add a text hook"
        return "front-load a line with stakes or a question in the first 2 s (e.g. 'wait, is that…')"
    if worst == "retention":
        if lulls:
            l = lulls[0]
            return f"trim the dead air at {_fmt(l['start'])}–{_fmt(l['end'])} ({l['end'] - l['start']:.1f} s)"
        if ret_notes.get("fillers"):
            return f"cut {ret_notes['fillers']} filler word(s) (first at {_fmt(ret_notes['filler_at'][0])})"
        return "tighten pacing — add a jump cut every ~5 s"
    if worst == "clarity":
        r = clar_notes.get("speech_to_game_ratio")
        if r is not None and r < 1.1:
            return f"voice is buried under game audio (ratio {r:.2f}) — raise mic +3 dB or duck the game track"
        if clar_notes.get("no_speech"):
            return "no speech — add a 3-word on-screen hook so captions have something to show"
        return "shorten caption chunks to 2-3 words and keep them in the safe zone"
    return f"clip is {duration:.0f} s — aim for 15–35 s"


def predict_candidate(words: Sequence[Dict[str, Any]], rms: Sequence[float], start: float, end: float,
                      hop_s: float = 0.1, target_len: float = 30.0, source_w: int = 1920,
                      source_h: int = 1080, signals: Optional[Dict[str, Any]] = None,
                      visual: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Full prediction for one window. Pure.

    ``signals``: ``{"onset_times": [abs seconds of non-speech action onsets], "firefight_peak_abs": s|None}``.
    ``visual``: output of :func:`clipper.visual.window_visual_stats` (menu/static/UI frame ratios). When the
    window *ends* on a menu or static screen the retention score drops and the fix says where to cut; when
    most of the window is menu/static the clip is capped well below the recommendation floor.
    """
    duration = max(0.0, end - start)
    rel_words = clip_words(words, start, end)
    rel_rms = list(rms[int(start / hop_s): int(end / hop_s)]) if rms else []
    # clip-relative action signals (from the moment scorer), optional
    sig = signals or {}
    rel_onsets = [t - start for t in (sig.get("onset_times") or []) if start <= t < end]
    ff = sig.get("firefight_peak_abs")
    action = {"onsets": rel_onsets, "firefight_at": (ff - start) if ff is not None and start <= ff < end else None}

    hook, hn = score_hook(rel_words, rel_rms, hop_s, action=action)
    retention, lulls, rn = score_retention(rel_words, rel_rms, duration, hop_s, action_onsets=rel_onsets)
    clarity, cn = score_clarity(rel_words, rel_rms, hop_s, source_w, source_h)
    length_fit, ln = score_length(duration, target_len)
    fix = biggest_fix(hook, retention, clarity, length_fit, lulls, hn, rn, cn, rel_words, duration)
    vn: Dict[str, Any] = {}
    vis = visual or {}
    if vis.get("frames"):
        vn = {k: vis.get(k) for k in ("bad_ratio", "menu_ratio", "static_ratio", "ui_ratio", "black_ratio",
                                      "tail_bad", "bad_from_sec", "window_bad", "tail_is_bad")}
        if vis.get("window_bad"):
            retention = min(retention, 5)
            vn["penalty"] = "mostly menu/static screen"
            fix = f"{int(round(100 * vis['bad_ratio']))}% of this window is a menu/static screen — not worth clipping"
        elif vis.get("tail_is_bad"):
            retention = max(0, retention - 10)
            cut_at = vis.get("bad_from_sec")
            vn["penalty"] = "ends on menu/static screen"
            if cut_at is not None and cut_at >= 12:
                trim = duration - cut_at
                fix = f"ends on a menu/static screen from {_fmt(cut_at)} — cut it there (reply 'N end {trim:.0f}s earlier')"
            else:
                fix = "ends on a menu/static screen — pick a window that finishes on the action"
    total = hook + retention + clarity + length_fit
    return {
        "total": int(total), "hook": hook, "retention": retention, "clarity": clarity,
        "length_fit": length_fit, "lulls": lulls,
        "biggest_fix": fix,
        "notes": {"hook": hn, "retention": rn, "clarity": cn, "length": ln, "visual": vn},
    }


def predict_all(candidates: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]], rms: Sequence[float],
                hop_s: float = 0.1, target_len: float = 30.0, source_w: int = 1920,
                source_h: int = 1080, visuals: Optional[Sequence[Optional[Dict[str, Any]]]] = None
                ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Adds ``predict`` to each candidate (copied) and returns (candidates, recommended_rank).

    ``recommended_rank`` is ``None`` unless some candidate is both a ``strong_moment`` (real game
    action, from the scorer) AND scores at least ``MIN_RECOMMEND_TOTAL``. "Least boring" is not
    a recommendation; the review message tells the user when nothing qualifies.
    """
    out: List[Dict[str, Any]] = []
    best_rank, best_total = None, -1
    for i, c in enumerate(candidates):
        cc = dict(c)
        csig = c.get("signals") or {}
        vis = (visuals[i] if visuals and i < len(visuals) else None) or {}
        cc["predict"] = predict_candidate(words, rms, float(c["start"]), float(c["end"]),
                                          hop_s, target_len, source_w, source_h,
                                          signals={"onset_times": csig.get("onset_times") or [],
                                                   "firefight_peak_abs": csig.get("firefight_peak_abs")},
                                          visual=vis)
        if not cc.get("strong_moment", True):
            cc["predict"]["biggest_fix"] = "no game action in this window — not worth clipping as a highlight"
        if vis.get("window_bad"):
            cc["strong_moment"] = False      # a menu with gunfire audio behind it is still a menu
        out.append(cc)
        eligible = cc.get("strong_moment", True) and cc["predict"]["total"] >= MIN_RECOMMEND_TOTAL
        if eligible and cc["predict"]["total"] > best_total:
            best_total, best_rank = cc["predict"]["total"], cc.get("rank")
    return out, best_rank


MIN_RECOMMEND_TOTAL = 70  # below this nothing gets the ⭐; the user still sees the list
