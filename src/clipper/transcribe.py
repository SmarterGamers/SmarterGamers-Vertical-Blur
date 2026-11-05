"""Stage 2 — word-level transcription with faster-whisper, run in a CHILD process.

Why a child process: a bad CUDA / ctranslate2 / onnxruntime setup on Windows crashes the
interpreter with an access violation (exit code -1073741819 / 0xC0000005) that no
``try/except`` can catch. Running the model in ``subprocess`` keeps the runner alive:

1. try ``device=cuda, compute_type=float16``
2. if the child exits non-zero (including negative codes) retry ``device=cpu, compute_type=int8``
3. if that fails too → :class:`TranscribeError` with the install fix and the child's stderr tail

The device that worked is cached in ``WHISPER_DEVICE_CACHE`` (job-independent) so later runs
skip the failed probe. faster-whisper is imported only inside the worker (``--worker`` mode),
so the runner starts without it and /health can report it as missing.

Outputs in the job dir: ``transcript.json``
(``{"segments":[{"start","end","text","words":[{"start","end","word"}]}]}``) and ``transcript.srt``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VENV_PYTHON = r"C:\SmarterGamers\clipper-venv314\Scripts\python.exe"
PIP_FIX = f'{VENV_PYTHON} -m pip install faster-whisper "av<19"'
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# (device, compute_type) attempts, in order
ATTEMPTS: Tuple[Tuple[str, str], ...] = (("cuda", "float16"), ("cpu", "int8"))


class TranscribeError(RuntimeError):
    """Raised with a ``fix`` hint when transcription cannot run."""

    def __init__(self, message: str, fix: Optional[str] = None):
        super().__init__(message)
        self.fix = fix


def faster_whisper_version() -> Optional[str]:
    """Version string if faster-whisper is importable, else None (used by /health).

    Only the package metadata is read (no ctranslate2 import) so this can never crash the runner.
    """
    try:
        from importlib.metadata import version
        return version("faster-whisper")
    except Exception:
        return None


# ------------------------------------------------------------------ formatting helpers
def _fmt_srt_time(t: float) -> str:
    t = max(0.0, float(t))
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: List[Dict[str, Any]]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{_fmt_srt_time(seg['start'])} --> {_fmt_srt_time(seg['end'])}", seg["text"].strip(), ""]
    return "\n".join(lines)


def load_words(transcript_path: Path) -> List[Dict[str, Any]]:
    """Flattens transcript.json into a sorted list of ``{"start","end","word"}``."""
    data = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    words: List[Dict[str, Any]] = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []):
            words.append({"start": float(w["start"]), "end": float(w["end"]), "word": str(w["word"])})
    words.sort(key=lambda w: w["start"])
    return words


# ------------------------------------------------------------------ worker (child process)
def run_worker(source: Path, out_json: Path, device: str, compute_type: str, model_name: str) -> int:
    """Child-process entry: transcribe ``source`` and write ``out_json``. Exit 0 on success.

    Exit codes: 2 = faster-whisper missing, 3 = model load failed, 4 = decode failed.
    numpy floats are cast to plain ``float`` before json.dump.
    """
    try:
        from faster_whisper import WhisperModel  # noqa: WPS433 (lazy on purpose)
    except ImportError as exc:
        sys.stderr.write(f"faster-whisper import failed: {exc}\n")
        return 2
    # ``cuda:1`` → device="cuda", device_index=1 (pin Whisper to a specific GPU, e.g. a second card).
    dev, _, idx = device.partition(":")
    kwargs = {"device_index": int(idx)} if dev == "cuda" and idx.isdigit() else {}
    try:
        model = WhisperModel(model_name, device=dev, compute_type=compute_type, **kwargs)
    except Exception as exc:
        sys.stderr.write(f"model load failed ({model_name}, {device}/{compute_type}): {exc}\n")
        return 3
    try:
        seg_iter, _info = model.transcribe(str(source), word_timestamps=True, vad_filter=True, beam_size=5)
        segments: List[Dict[str, Any]] = []
        for seg in seg_iter:
            words = [
                {"start": round(float(w.start), 3), "end": round(float(w.end), 3), "word": str(w.word).strip()}
                for w in (seg.words or [])
                if w.word and str(w.word).strip()
            ]
            segments.append({"start": round(float(seg.start), 3), "end": round(float(seg.end), 3),
                             "text": str(seg.text).strip(), "words": words})
    except Exception as exc:
        sys.stderr.write(f"transcribe failed: {exc}\n")
        return 4
    out_json.write_text(json.dumps({"segments": segments, "device": device, "compute_type": compute_type},
                                   indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


# ------------------------------------------------------------------ parent side
def _read_cached_device(cache: Optional[Path]) -> Optional[Tuple[str, str]]:
    try:
        if cache and cache.is_file():
            dev, _, comp = cache.read_text(encoding="utf-8").strip().partition("/")
            if (dev == "cpu" or dev.startswith("cuda")) and comp:
                return dev, comp
    except OSError:
        pass
    return None


def _write_cached_device(cache: Optional[Path], device: str, compute: str) -> None:
    try:
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(f"{device}/{compute}\n", encoding="utf-8")
    except OSError:
        pass


def transcribe_job(job_dir: Path, source: Path, model_name: str = "small.en", device: str = "auto",
                   python_exe: Optional[str] = None, device_cache: Optional[Path] = None,
                   timeout_sec: int = 3600, media_sec: Optional[float] = None) -> Dict[str, Any]:
    """Transcribes ``source`` via a child process and writes transcript.json / transcript.srt.

    ``device``: ``auto`` (cuda then cpu, honouring the cache), ``cuda``, ``cuda:N`` (pin to GPU N) or ``cpu``.
    A pinned GPU still falls back to CPU if its child fails, so a missing second card never blocks a job.
    ``media_sec``: source duration; when given, each attempt gets a cap derived from it (see loop)
    so a hung GPU child falls through to CPU quickly instead of blocking for ``timeout_sec``.
    Returns the /transcribe payload (without ``ok``). Raises :class:`TranscribeError`.
    """
    job_dir = Path(job_dir)
    raw_json = job_dir / "transcript.raw.json"
    py = python_exe or sys.executable

    if device == "cuda":
        attempts: List[Tuple[str, str]] = [("cuda", "float16")]
    elif device.startswith("cuda:"):
        attempts = [(device, "float16"), ("cpu", "int8")]
    elif device == "cpu":
        attempts = [("cpu", "int8")]
    else:
        attempts = list(ATTEMPTS)
        cached = _read_cached_device(device_cache)
        if cached and cached in attempts:
            attempts.remove(cached)
            attempts.insert(0, cached)

    errors: List[str] = []
    used: Optional[Tuple[str, str]] = None
    for dev, comp in attempts:
        if raw_json.exists():
            raw_json.unlink()
        cmd = [py, "-X", "faulthandler", str(Path(__file__).resolve()), "--worker",
               "--input", str(source), "--output", str(raw_json),
               "--device", dev, "--compute-type", comp, "--model", model_name]
        # Per-attempt cap. A GPU child can HANG (not crash) when the GPU is busy with OBS/NVENC/etc.;
        # without a tight cap it held the runner thread for an hour. CUDA normally finishes a 1 h VOD
        # in well under a minute, so give it 60 s + 0.5x media length; CPU gets 120 s + 6x media length.
        ratio = 0.5 if dev.startswith("cuda") else 6.0
        base = 60 if dev.startswith("cuda") else 120
        attempt_timeout = int(min(timeout_sec, base + ratio * max(0.0, media_sec or 0.0))) if media_sec else timeout_sec
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW,
                                  timeout=attempt_timeout)
        except subprocess.TimeoutExpired:
            errors.append(f"{dev}/{comp}: timed out after {attempt_timeout} s")
            continue
        if proc.returncode == 0 and raw_json.exists():
            used = (dev, comp)
            break
        tail = (proc.stderr or "").strip()[-400:]
        errors.append(f"{dev}/{comp}: exit {proc.returncode} ({hex(proc.returncode & 0xFFFFFFFF)}) {tail}")
        if proc.returncode == 2:  # library missing: no point trying another device
            break

    if used is None:
        raise TranscribeError("faster-whisper could not transcribe: " + " | ".join(errors), fix=PIP_FIX)
    _write_cached_device(device_cache, *used)

    data = json.loads(raw_json.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    n_words = sum(len(s.get("words", [])) for s in segments)
    tj = job_dir / "transcript.json"
    ts = job_dir / "transcript.srt"
    tj.write_text(json.dumps({"segments": segments}, indent=1, ensure_ascii=False), encoding="utf-8")
    ts.write_text(segments_to_srt(segments), encoding="utf-8")
    raw_json.unlink(missing_ok=True)

    payload: Dict[str, Any] = {"transcript": str(tj), "srt": str(ts), "words": n_words,
                               "model": model_name, "device": used[0], "compute_type": used[1]}
    if n_words == 0:
        payload["note"] = "no speech detected"
    if errors:
        payload["fallback_from"] = errors
    return payload


# ------------------------------------------------------------------ CLI (worker mode)
def _main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="faster-whisper worker (used by the clipper runner)")
    p.add_argument("--worker", action="store_true", help="run as the child transcription worker")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--device", default="cpu")
    p.add_argument("--compute-type", default="int8")
    p.add_argument("--model", default="small.en")
    a = p.parse_args(argv)
    return run_worker(Path(a.input), Path(a.output), a.device, a.compute_type, a.model)


if __name__ == "__main__":
    sys.exit(_main())
