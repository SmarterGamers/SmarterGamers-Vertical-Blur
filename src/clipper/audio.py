"""Audio / probe helpers shared by the clipper stages.

- :func:`find_ffmpeg` / :func:`find_ffprobe` — tool discovery (reuses the engine's logic)
- :func:`probe` — ffprobe JSON -> compact dict (duration, width, height, fps, audio tracks)
- :func:`decode_pcm` — mono 16-bit PCM via ``ffmpeg -f s16le`` (all tracks downmixed)
- :func:`rms_envelope` — per-hop RMS array (numpy if present, pure Python otherwise)
- :func:`rms_onsets` — simple z-score onset detector used when ``bf6_clipper`` is unavailable

opencv is never imported. numpy is optional; the pure-Python paths are slower but correct.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _engine_module():
    """Imports core.ffmpeg_engine lazily (adds src/ to sys.path if needed)."""
    src_dir = Path(__file__).resolve().parent.parent
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    from core.ffmpeg_engine import FFmpegEngine  # noqa: WPS433 (deferred import on purpose)
    return FFmpegEngine


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg") or _engine_module().find_ffmpeg()


def find_ffprobe() -> Optional[str]:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    ff = find_ffmpeg()
    if ff:
        for name in ("ffprobe.exe", "ffprobe"):
            beside = Path(ff).parent / name
            if beside.exists():
                return str(beside)
    return None


def probe(path: Path) -> Dict[str, Any]:
    """Returns ``{duration_sec, width, height, fps, audio_tracks:[{index,codec,channels}], has_video}``."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise RuntimeError("ffprobe not found")
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=30)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"ffprobe failed on {path}: {res.stderr.strip()[-300:]}")
    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    duration = float(data.get("format", {}).get("duration") or (v or {}).get("duration") or 0.0)
    fps = 0.0
    if v:
        r = v.get("r_frame_rate", "0/1")
        num, _, den = r.partition("/")
        try:
            fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        except ValueError:
            fps = 0.0

    return {
        "has_video": v is not None,
        "duration_sec": round(duration, 3),
        "width": int(v.get("width", 0)) if v else 0,
        "height": int(v.get("height", 0)) if v else 0,
        "fps": round(fps, 3),
        "audio_tracks": [
            {"index": i, "codec": s.get("codec_name", "unknown"), "channels": int(s.get("channels", 0) or 0)}
            for i, s in enumerate(audio)
        ],
    }


def decode_pcm(path: Path, sample_rate: int = 22050, start_s: float = 0.0,
               duration_s: Optional[float] = None, mix_all_tracks: bool = True) -> bytes:
    """Decodes the file's audio to mono signed 16-bit little-endian PCM bytes.

    With ``mix_all_tracks`` every audio stream (game + mic on OBS recordings) is downmixed
    with ``amix`` so a loud mic reaction and a loud in-game explosion both count.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(path)]
    if duration_s:
        cmd += ["-t", f"{duration_s:.3f}"]

    n_tracks = 1
    if mix_all_tracks:
        try:
            n_tracks = max(1, len(probe(path)["audio_tracks"]))
        except Exception:
            n_tracks = 1
    if n_tracks > 1:
        inputs = "".join(f"[0:a:{i}]" for i in range(n_tracks))
        cmd += ["-filter_complex", f"{inputs}amix=inputs={n_tracks}:duration=longest[a]", "-map", "[a]"]
    else:
        cmd += ["-map", "0:a:0?"]
    cmd += ["-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    res = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg pcm decode failed: {res.stderr.decode('utf-8', 'replace')[-300:]}")
    return res.stdout


def rms_envelope(pcm: bytes, sample_rate: int = 22050, hop_s: float = 0.1) -> List[float]:
    """Per-hop RMS (0..1 scale) of 16-bit mono PCM. Index i covers [i*hop_s, (i+1)*hop_s)."""
    hop = max(1, int(sample_rate * hop_s))
    try:
        import numpy as np  # optional
        arr = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        n = len(arr) // hop
        if n == 0:
            return []
        frames = arr[: n * hop].reshape(n, hop)
        return [float(x) for x in np.sqrt((frames ** 2).mean(axis=1))]
    except ImportError:
        import array
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        out: List[float] = []
        for i in range(0, len(samples) - hop + 1, hop):
            chunk = samples[i:i + hop]
            out.append(math.sqrt(sum((s / 32768.0) ** 2 for s in chunk) / hop))
        return out


def rms_onsets(env: Sequence[float], hop_s: float = 0.1, z: float = 2.5,
               min_gap_s: float = 0.5) -> List[float]:
    """Timestamps where the RMS jumps more than ``z`` standard deviations above the mean rise.

    Pure fallback for v1's ``compute_audio_onsets``: diff the envelope, threshold on a z-score,
    and merge detections closer than ``min_gap_s``.
    """
    if len(env) < 3:
        return []
    diffs = [max(0.0, env[i] - env[i - 1]) for i in range(1, len(env))]
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
    std = math.sqrt(var)
    if std <= 1e-9:
        return []
    out: List[float] = []
    for i, d in enumerate(diffs):
        if (d - mean) / std >= z:
            t = (i + 1) * hop_s
            if not out or t - out[-1] >= min_gap_s:
                out.append(round(t, 3))
    return out
