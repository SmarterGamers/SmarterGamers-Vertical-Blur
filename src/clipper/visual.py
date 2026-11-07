"""Visual dead-time check for candidate windows (menus, scoreboards, deploy maps, static screens).

The audio-only scorer cannot see that the back half of a window is a trophy screen or a deploy map.
This module samples 1 fps frames from the source for a given window and classifies each frame as

* ``black``  — pure black (loading),
* ``menu``   — 64-bit dHash within Hamming distance ``MAX_HAMMING`` of a known menu screen
               (``menu_refs.json``, built by the upload pipeline's gameplay gate from real menu screens),
* ``static`` — near-identical to the previous frame,
* ``ui``     — dark, low-saturation, flat frame (deploy map / settings / scoreboard shapes),

and reports ratios plus where a bad *tail* begins, so the predictor can penalise and the review message
can say "ends on a menu from 0:16 — reply '5 end 14s earlier'".

Same geometry as the gate so the reference hashes transfer: for vertical split-screen sources
(h > w) only the centre game band is analysed and the fixed facecam box is masked; a 16:9 source is
analysed whole. Pure Python over ffmpeg's rawvideo output — no PIL/numpy needed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

W, H = 160, 90                 # analysis frame size (gray + rgb)
MAX_HAMMING = 10
STATIC_DIFF = 1.5              # mean abs diff on the 9x8 thumbnail
UI_SAT_MAX = 0.35
UI_FLAT_MIN = 0.18
UI_LUMA_MAX = 85
UI_MOTION_MAX = 4.0            # a dim UI frame must also be near-motionless (dusk gameplay moves; menus don't)
DARK_LUMA_MAX = 16             # ...unless it is near-black and flat: settings/pause menus with a moving cursor
DARK_FLAT_MIN = 0.6
STATIC_RUN_MIN = 5             # a freeze counts fully only when it lasts >= 5 s (scoreboard, map, trophy screen)
TAIL_SEC = 10.0                # "ends on a menu" looks at the last 10 s
TAIL_BAD_FRACTION = 0.5        # ...and calls it bad when >= 50 % of those frames are bad
BAD_WINDOW_FRACTION = 0.5      # whole window is bad when the weighted bad share is >= 50 %
MIN_GOOD_PREFIX_SEC = 12.0     # ...unless >= 12 s of clean footage precede the junk: then it's "cut it there"
MOSTLY_BAD = 0.75              # ...but a window that is >= 75 % junk is bad no matter where the clean bit sits
LOW_ACTION_BAD = 0.3           # low_action: >= 30 % junk frames (measured: real fights 0.0, dark/idle talk 0.37+)
LOW_ACTION_MOTION = 5.0        # ...or barely any picture motion (measured: gameplay 14-38, garage/shop 1.9)
FACECAM_DRAWBOX = "drawbox=x=0:y=ih-ih*0.42:w=iw*0.22:h=ih*0.42:color=black:t=fill"
_NO_WINDOW = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0


def load_menu_refs(path: Optional[Path]) -> List[int]:
    """Reference dHashes (hex strings or ints). Missing/invalid file → empty list (hash match disabled)."""
    try:
        if path and Path(path).is_file():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            out = []
            for v in data if isinstance(data, list) else data.get("hashes", []):
                out.append(int(v, 16) if isinstance(v, str) else int(v))
            return out
    except (OSError, ValueError, TypeError):
        pass
    return []


def _facecam_drawbox(facecam_box: Optional[str]) -> Optional[str]:
    """``x,y,w,h`` fractions (config FACECAM_BOX) → an ffmpeg drawbox that blacks the webcam out."""
    try:
        x, y, w, h = [float(t) for t in str(facecam_box).split(",")]
    except (TypeError, ValueError):
        return None
    return f"drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:color=black:t=fill"


def _vf(source_w: int, source_h: int, fps: float, facecam_box: Optional[str] = None) -> str:
    vertical = source_h > source_w > 0
    parts = [f"fps={fps}"]
    if vertical:
        parts += ["crop=iw:ih*0.32:0:ih*0.34", FACECAM_DRAWBOX]
    elif _facecam_drawbox(facecam_box):
        # 16:9 stream recording: the streamer moving in the webcam is not gameplay motion
        parts.append(_facecam_drawbox(facecam_box))
    parts += [f"scale={W}:{H}", "format=rgb24"]
    return ",".join(parts)


def _frames(source: Path, start: float, end: float, source_w: int, source_h: int, fps: float,
            facecam_box: Optional[str] = None) -> List[bytes]:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.0, end - start):.3f}",
           "-i", str(source), "-vf", _vf(source_w, source_h, fps, facecam_box), "-f", "rawvideo", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    fb = W * H * 3
    raw = p.stdout
    return [raw[i:i + fb] for i in range(0, len(raw) - fb + 1, fb)]


def _analyse_frame(rgb: bytes) -> Dict[str, Any]:
    n = W * H
    gray = bytearray(n)
    hist = [0] * 256
    sat_sum = 0.0
    for i in range(n):
        r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
        y = (r * 299 + g * 587 + b * 114) // 1000
        gray[i] = y
        hist[y] += 1
        mx = r if r >= g and r >= b else (g if g >= b else b)
        mn = r if r <= g and r <= b else (g if g <= b else b)
        sat_sum += (mx - mn) / mx if mx else 0.0
    black = sum(hist[10:]) == 0
    # 9x8 thumbnail by block averaging (for dHash + static diff)
    thumb = []
    bw, bh = W / 9.0, H / 8.0
    for ty in range(8):
        y0, y1 = int(ty * bh), int((ty + 1) * bh)
        for tx in range(9):
            x0, x1 = int(tx * bw), int((tx + 1) * bw)
            s = 0
            for y in range(y0, y1):
                row = y * W
                s += sum(gray[row + x0:row + x1])
            thumb.append(s // max(1, (y1 - y0) * (x1 - x0)))
    h_val = 0
    for r in range(8):
        for c in range(8):
            h_val = (h_val << 1) | (1 if thumb[r * 9 + c] > thumb[r * 9 + c + 1] else 0)
    acc, med = 0, 0
    for idx, cnt in enumerate(hist):
        acc += cnt
        if acc >= n // 2:
            med = idx
            break
    flat = sum(hist[max(0, med - 8): min(256, med + 9)]) / n
    sat = sat_sum / n
    return {"black": black, "thumb": thumb, "dhash": h_val, "med_luma": med, "flat": flat, "sat": sat}


def window_visual_stats(source: Path, start: float, end: float, source_w: int = 1920, source_h: int = 1080,
                        refs: Optional[Sequence[int]] = None, fps: float = 1.0,
                        facecam_box: Optional[str] = None) -> Dict[str, Any]:
    """Classify 1 fps frames in [start, end). Returns ratios + tail info; ``frames == 0`` means 'unknown'.

    ``low_action`` flags windows that are not outright menus but are not gameplay worth posting unreviewed
    either: dark/idle stretches while the streamer talks, lying still, garage/shop screens.
    """
    try:
        frames = _frames(Path(source), start, end, source_w, source_h, fps, facecam_box)
    except Exception:
        frames = []
    empty = {"frames": 0, "bad_ratio": 0.0, "menu_ratio": 0.0, "static_ratio": 0.0, "ui_ratio": 0.0,
             "black_ratio": 0.0, "tail_bad": 0.0, "bad_from_sec": None, "window_bad": False, "tail_is_bad": False,
             "low_action": False}
    if not frames:
        return empty
    refs = list(refs or [])
    labels: List[str] = []
    prev = None
    counts = {"black": 0, "menu": 0, "static": 0, "ui": 0}
    motions: List[float] = []
    lumas: List[int] = []
    for fb in frames:
        a = _analyse_frame(fb)
        lumas.append(a["med_luma"])
        if a["black"]:
            labels.append("black"); counts["black"] += 1; prev = None
            continue
        label = "ok"
        motion = sum(abs(x - y) for x, y in zip(a["thumb"], prev)) / 72.0 if prev is not None else None
        if motion is not None:
            motions.append(motion)
        dark_flat = a["med_luma"] <= DARK_LUMA_MAX and a["flat"] >= DARK_FLAT_MIN
        if refs and any((a["dhash"] ^ ref).bit_count() <= MAX_HAMMING for ref in refs):
            label = "menu"
        elif dark_flat:
            label = "ui"
        elif motion is not None and motion < STATIC_DIFF:
            label = "static"
        elif (a["sat"] <= UI_SAT_MAX and a["flat"] >= UI_FLAT_MIN and a["med_luma"] <= UI_LUMA_MAX
              and motion is not None and motion < UI_MOTION_MAX):
            label = "static"          # dim, flat and barely moving: treat like a freeze
        if label != "ok":
            counts[label] += 1
        labels.append(label)
        prev = a["thumb"]
    n = len(labels)
    # Weighting: menu/black/dark-UI frames are always bad; a freeze is fully bad only as a run >= STATIC_RUN_MIN
    # (shorter stills happen in real play), otherwise half.
    weight = [0.0] * n
    i = 0
    while i < n:
        lab = labels[i]
        j = i
        while j < n and labels[j] == lab:
            j += 1
        if lab in ("menu", "black", "ui"):
            w = 1.0
        elif lab == "static":
            w = 1.0 if (j - i) >= STATIC_RUN_MIN else 0.5
        else:
            w = 0.0
        for k in range(i, j):
            weight[k] = w
        i = j
    bad = [w > 0 for w in weight]
    bad_n = sum(bad)
    weighted = sum(weight) / n
    tail_n = max(1, min(n, int(round(TAIL_SEC * fps))))
    tail_bad = sum(bad[-tail_n:]) / tail_n
    # where does the final bad stretch begin? (allow single-frame gaps)
    bad_from = None
    if tail_bad >= TAIL_BAD_FRACTION or weighted >= BAD_WINDOW_FRACTION:
        i = n - 1
        while i >= 0 and (bad[i] or (i - 1 >= 0 and bad[i - 1])):
            i -= 1
        if i < n - 1:
            bad_from = round((i + 1) / fps, 1)
    good_prefix = bad_from if bad_from is not None else (0.0 if weighted >= BAD_WINDOW_FRACTION else n / fps)
    window_bad = (weighted >= BAD_WINDOW_FRACTION and good_prefix < MIN_GOOD_PREFIX_SEC) or weighted >= MOSTLY_BAD
    motion_mean = sum(motions) / len(motions) if motions else None
    low_action = window_bad or weighted >= LOW_ACTION_BAD or (motion_mean is not None and motion_mean < LOW_ACTION_MOTION)
    tail_is_bad = tail_bad >= TAIL_BAD_FRACTION or (weighted >= BAD_WINDOW_FRACTION and not window_bad)
    return {
        "frames": n, "bad_ratio": round(weighted, 3), "menu_ratio": round(counts["menu"] / n, 3),
        "static_ratio": round(counts["static"] / n, 3), "ui_ratio": round(counts["ui"] / n, 3),
        "black_ratio": round(counts["black"] / n, 3), "tail_bad": round(tail_bad, 3), "bad_from_sec": bad_from,
        "window_bad": window_bad, "tail_is_bad": tail_is_bad,
        "low_action": low_action,
        "motion_mean": round(motion_mean, 2) if motion_mean is not None else None,
        "luma_mean": round(sum(lumas) / len(lumas), 1) if lumas else None,
    }
