"""Captions — word-level transcript -> clip-relative SRT and a safe-zone ASS for burn-in.

Design (1080x1920 Shorts/TikTok canvas):
- max 3 words per caption chunk, bold white text, 3 px black outline, centred
- optional yellow highlight of the word currently being spoken (one ASS event per word)
- placed at ~62 % of the frame height (``CAPTION_Y``), i.e. above the bottom ~25 % where the
  platform caption/description sits, and inside horizontal margins that keep text >= 120 px
  from the right edge (like/comment column)

:func:`safe_zone_rect` returns the allowed rectangle and :func:`layout_chunk` returns the
estimated bounding box of each chunk, so tests can assert every box lies inside the zone.
Font size shrinks automatically when a chunk would be too wide.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PLAY_W, PLAY_H = 1080, 1920
SIDE_MARGIN = 120                  # keeps text clear of the right-hand like/comment column
CAPTION_Y = int(PLAY_H * 0.62)     # vertical centre of captions (1190 px) — "blur" layout
ZONE_TOP = int(PLAY_H * 0.52)      # allowed band: 52 % .. 75 % of the frame height
ZONE_BOTTOM = int(PLAY_H * 0.75)
# "stream" layout (facecam top, gameplay 660..1470): captions sit in the band under the gameplay panel,
# still above the bottom ~20 % where the platform UI lives.
STREAM_CAPTION_Y = int(PLAY_H * 0.80)   # 1536 px
STREAM_ZONE = (int(PLAY_H * 0.77), int(PLAY_H * 0.84))
FONT_NAME = "Arial"
FONT_SIZE = 78
MIN_FONT_SIZE = 40
OUTLINE_PX = 3
SHADOW_PX = 1
MAX_WORDS = 3
MIN_CHUNK_SEC = 0.35
CHAR_W_RATIO = 0.62                # conservative average glyph width / font size for bold Arial
LINE_H_RATIO = 1.25

WHITE = "&H00FFFFFF"
YELLOW = "&H0000FFFF"              # ASS is BGR: 00FFFF = yellow
BLACK = "&H00000000"


# Profanity masking for burned-in captions (advertiser-friendly Shorts): the word keeps its first letter and
# length so the caption still reads naturally ("f***ing"). The audio is untouched. Matching is on the
# letters only, so "fucking," / "FUCK!" / "bullshit" are all caught; false positives on ordinary words are
# avoided by matching whole tokens, not substrings (except the compound forms listed).
PROFANITY_ROOTS = ("fuck", "shit", "bitch", "asshole", "cunt", "motherfuck", "dumbass", "bullshit", "goddamn",
                   "dick", "pussy", "cocksucker", "whore", "slut", "nigg", "fag", "retard")
MASK_PROFANITY = True

# Emoji reactions appended to a caption chunk when it contains a trigger word (first match wins; one
# emoji per chunk so it reads as a reaction, not confetti). Rendered via EMOJI_FONT (Segoe UI Emoji on
# Windows, Noto Color Emoji elsewhere; libass draws the glyph, in colour where the build supports it).
EMOJI_FONT = "Segoe UI Emoji"
EMOJI_RULES = (
    (("let's go", "lets go", "yes", "yeah", "hell yeah", "clutch"), "\U0001F525"),   # fire
    (("holy", "oh my god", "omg", "what the", "no way", "bro", "dude"), "\U0001F631"),  # screaming
    (("tank", "boom", "explode", "exploded", "nuke", "rocket", "rpg"), "\U0001F4A5"),  # collision
    (("kill", "killed", "headshot", "snipe", "sniped", "clapped", "cooked"), "\U0001F480"),  # skull
    (("lol", "lmao", "haha", "funny", "bruh", "nice"), "\U0001F602"),                 # joy
    (("president", "king", "goat", "legend"), "\U0001F451"),                         # crown
    (("turbo", "engine", "drift", "car", "truck", "speed"), "\U0001F3CE"),           # race car
    (("ghost",), "\U0001F47B"),
    (("love", "thank", "follow", "sub", "subs"), "\U0001F49C"),                      # purple heart
    (("dead", "died", "rip", "gg"), "\U0001FAE1"),                                   # salute
    (("sick", "insane", "crazy", "wild"), "\U0001F92F"),                             # exploding head
)
ENABLE_EMOJI = True


def emoji_for(text: str) -> str:
    """One reaction emoji for a caption chunk, or "" when no trigger word is present."""
    if not ENABLE_EMOJI:
        return ""
    low = " " + "".join(ch if ch.isalnum() or ch in "' " else " " for ch in text.lower()) + " "
    for triggers, emo in EMOJI_RULES:
        for t in triggers:
            if f" {t} " in low:
                return emo
    return ""


def mask_profanity(word: str) -> str:
    """"fucking," -> "f******,"; non-profane words are returned unchanged."""
    import re as _re
    m = _re.match(r"^(\W*)([A-Za-z'\-]+)(\W*)$", word)
    if not m:
        return word
    pre, core, post = m.groups()
    low = core.lower().replace("'", "").replace("-", "")
    hit = any(low.startswith(r) or low.endswith(r) or (r in low and len(low) <= len(r) + 4) for r in PROFANITY_ROOTS)
    if not hit:
        return word
    return f"{pre}{core[0]}{'*' * (len(core) - 1)}{post}"


@dataclass
class CaptionChunk:
    start: float            # clip-relative seconds
    end: float
    words: List[Dict[str, Any]]  # each {"start","end","word"} clip-relative

    @property
    def text(self) -> str:
        return " ".join(w["word"].strip() for w in self.words)


def safe_zone_rect(layout: str = "blur") -> Tuple[int, int, int, int]:
    """(x0, y0, x1, y1) in canvas pixels where caption boxes must lie for the given render layout."""
    if layout == "stream":
        return (SIDE_MARGIN, STREAM_ZONE[0], PLAY_W - SIDE_MARGIN, STREAM_ZONE[1])
    return (SIDE_MARGIN, ZONE_TOP, PLAY_W - SIDE_MARGIN, ZONE_BOTTOM)


def caption_chunks(words: Sequence[Dict[str, Any]], clip_start: float, clip_end: float,
                   max_words: int = MAX_WORDS) -> List[CaptionChunk]:
    """Groups words inside [clip_start, clip_end) into <= ``max_words`` chunks, clip-relative times."""
    from clipper.score_moments import strip_voice_commands   # local import: score_moments imports nothing from here
    inside = strip_voice_commands([w for w in sorted(words, key=lambda w: w["start"])
                                   if clip_start <= w["start"] < clip_end and w["word"].strip()])
    chunks: List[CaptionChunk] = []
    buf: List[Dict[str, Any]] = []

    def flush():
        if not buf:
            return
        rel = [{"start": round(w["start"] - clip_start, 3),
                "end": round(min(w["end"], clip_end) - clip_start, 3),
                "word": (mask_profanity(w["word"].strip()) if MASK_PROFANITY else w["word"].strip())} for w in buf]
        start = rel[0]["start"]
        end = max(rel[-1]["end"], start + MIN_CHUNK_SEC)
        chunks.append(CaptionChunk(start=start, end=min(end, clip_end - clip_start), words=rel))
        buf.clear()

    for w in inside:
        # start a new chunk on sentence end or when full or after a long pause
        if buf and (len(buf) >= max_words or (w["start"] - buf[-1]["end"]) > 1.0):
            flush()
        buf.append(w)
        if w["word"].strip()[-1:] in ".!?":
            flush()
    flush()

    # do not let a chunk linger over the next one
    for a, b in zip(chunks, chunks[1:]):
        if a.end > b.start:
            a.end = b.start
    return chunks


def layout_chunk(chunk: CaptionChunk, layout: str = "blur") -> Dict[str, Any]:
    """Estimates font size and bounding box for a chunk so it fits the safe zone of ``layout``.

    Returns ``{"font_size", "bbox": (x0, y0, x1, y1), "center": (x, y)}``.
    """
    x0, _, x1, _ = safe_zone_rect(layout)
    max_w = x1 - x0
    size = FONT_SIZE
    text = chunk.text
    while size > MIN_FONT_SIZE and _text_width(text, size) > max_w:
        size -= 2
    w = min(max_w, _text_width(text, size))
    h = size * LINE_H_RATIO + 2 * OUTLINE_PX
    cx = (x0 + x1) // 2
    cy = STREAM_CAPTION_Y if layout == "stream" else CAPTION_Y
    bbox = (int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2))
    return {"font_size": size, "bbox": bbox, "center": (cx, cy)}


def _text_width(text: str, size: int) -> float:
    return len(text) * size * CHAR_W_RATIO + 2 * OUTLINE_PX


# ------------------------------------------------------------------ SRT
def _srt_time(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(chunks: Sequence[CaptionChunk]) -> str:
    out = []
    for i, c in enumerate(chunks, 1):
        out += [str(i), f"{_srt_time(c.start)} --> {_srt_time(c.end)}", c.text, ""]
    return "\n".join(out)


# ------------------------------------------------------------------ ASS
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(chunks: Sequence[CaptionChunk], highlight_current_word: bool = True, layout: str = "blur") -> str:
    """ASS document: one style, events positioned with \\pos at the safe-zone centre of ``layout``.

    Emoji (see :func:`emoji_for`) are rendered with ``EMOJI_FONT`` via an inline font tag so libass
    finds a glyph; the words themselves stay in ``FONT_NAME``."""
    header = f"""[Script Info]
; Generated by SmarterGamers Personal Clipper
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Clip,{FONT_NAME},{FONT_SIZE},{WHITE},{YELLOW},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,{OUTLINE_PX},{SHADOW_PX},5,{SIDE_MARGIN},{SIDE_MARGIN},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: List[str] = []
    for c in chunks:
        lay = layout_chunk(c, layout)
        cx, cy = lay["center"]
        pos = f"{{\\an5\\pos({cx},{cy})\\fs{lay['font_size']}\\b1}}"
        emo = emoji_for(c.text)
        tail = f" {{\\fn{EMOJI_FONT}}}{emo}{{\\fn{FONT_NAME}}}" if emo else ""
        if not highlight_current_word or len(c.words) == 1:
            lines.append(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Clip,,0,0,0,,{pos}{_esc(c.text)}{tail}")
            continue
        # one event per spoken word: the current word is yellow, the rest white
        for i, w in enumerate(c.words):
            seg_start = c.start if i == 0 else w["start"]
            seg_end = c.words[i + 1]["start"] if i + 1 < len(c.words) else c.end
            if seg_end <= seg_start:
                continue
            parts = []
            for j, ww in enumerate(c.words):
                tok = _esc(ww["word"])
                parts.append(f"{{\\c{YELLOW}&}}{tok}{{\\c{WHITE}&}}" if j == i else tok)
            lines.append(f"Dialogue: 0,{_ass_time(seg_start)},{_ass_time(seg_end)},Clip,,0,0,0,,{pos}{' '.join(parts)}{tail}")
    return header + "\n".join(lines) + "\n"


def write_caption_files(words: Sequence[Dict[str, Any]], clip_start: float, clip_end: float,
                        out_dir: Path, ass_name: str = "captions.ass",
                        srt_name: str = "captions.srt", layout: str = "blur") -> Dict[str, Any]:
    """Writes clip-relative ``captions.ass`` + ``captions.srt`` into ``out_dir`` for the render ``layout``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = caption_chunks(words, clip_start, clip_end)
    ass_path = out_dir / ass_name
    srt_path = out_dir / srt_name
    ass_path.write_text(build_ass(chunks, layout=layout), encoding="utf-8")
    srt_path.write_text(build_srt(chunks), encoding="utf-8")
    return {"ass": str(ass_path), "srt": str(srt_path), "chunks": len(chunks)}
