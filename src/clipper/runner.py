"""Personal Clipper runner — the Windows-side HTTP service n8n talks to.

Run::

    python src/clipper/runner.py [--host 127.0.0.1] [--port 5690]

Stdlib only (``http.server`` + ``json`` + ``subprocess``). One thread per request
(``ThreadingHTTPServer``). Implements every endpoint in ``n8n/CONTRACT.md``:

    GET  /health
    POST /guard /ingest /transcribe /score /predict /ollama_verdict /render /package /log

Every error is ``{"ok": false, "stage": "<stage>", "error": "<msg>", "fix": "<cmd or null>"}``
with HTTP 500; every success is ``{"ok": true, ...}``.

Per-job state lives in ``WORKING\\<job_id>\\``: ``source.<ext>``, ``meta.json`` (probe +
original name + hash), ``transcript.json`` / ``transcript.srt``, ``signals.json`` (RMS +
onsets cache), ``candidates.json``, ``captions.ass``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

# Make ``clipper`` and ``core`` importable whether run as a script or a module.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clipper import audio as audio_mod  # noqa: E402
from clipper import captions as captions_mod  # noqa: E402
from clipper import capcut_package  # noqa: E402
from clipper import score_moments  # noqa: E402
from clipper import transcribe as transcribe_mod  # noqa: E402
from clipper import visual  # noqa: E402
from clipper.config import ClipperConfig, load_config  # noqa: E402
from clipper.ledger import Ledger, append_run_log, file_hash  # noqa: E402
from clipper.llm import OllamaError, verdict_for_candidate  # noqa: E402
from predictor import score_clip  # noqa: E402

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%S")   # when this process came up (shown by /health)


class StageError(Exception):
    """Raised by a stage to produce the uniform error JSON."""

    def __init__(self, stage: str, message: str, fix: Optional[str] = None):
        super().__init__(message)
        self.stage = stage
        self.fix = fix


# ======================================================================================
# Stage implementations (plain functions so they can be tested without HTTP)
# ======================================================================================
class Stages:
    def __init__(self, cfg: ClipperConfig):
        self.cfg = cfg
        self.ledger = Ledger(cfg.LEDGER)

    # ---------------------------------------------------------------- helpers
    def job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            raise StageError("job", f"invalid job_id {job_id!r}", None)
        d = self.cfg.WORKING / job_id
        if not d.is_dir():
            raise StageError("job", f"job dir not found: {d}", "run /ingest first")
        return d

    def meta(self, job_dir: Path) -> Dict[str, Any]:
        mp = job_dir / "meta.json"
        if not mp.exists():
            raise StageError("job", f"meta.json missing in {job_dir}", "run /ingest first")
        return json.loads(mp.read_text(encoding="utf-8"))

    def words(self, job_dir: Path, stage: str) -> List[Dict[str, Any]]:
        tj = job_dir / "transcript.json"
        if not tj.exists():
            raise StageError(stage, f"transcript.json missing in {job_dir}", "run /transcribe first")
        return transcribe_mod.load_words(tj)

    def signals(self, job_dir: Path, source: Path) -> Dict[str, Any]:
        """RMS + onsets, cached in signals.json so /score and /predict share one decode."""
        sp = job_dir / "signals.json"
        if sp.exists():
            try:
                return json.loads(sp.read_text(encoding="utf-8"))
            except ValueError:
                pass
        sig = score_moments.collect_signals(source, self.cfg.BF6_CLIPPER_PATH)
        sp.write_text(json.dumps(sig), encoding="utf-8")
        return sig

    # ---------------------------------------------------------------- GET /health
    def health(self) -> Dict[str, Any]:
        fw = transcribe_mod.faster_whisper_version()
        return {
            "ffmpeg": audio_mod.find_ffmpeg(),
            "ffprobe": audio_mod.find_ffprobe(),
            "faster_whisper": fw,
            "bf6_clipper": score_moments.v1_available(self.cfg.BF6_CLIPPER_PATH),
            "model": self.cfg.WHISPER_MODEL if fw else None,
            "python": str(self.cfg.PYTHON_EXE),
            "repo": str(self.cfg.REPO_DIR),
            "head": self._git_head(),
            "started": _STARTED_AT,
        }

    def _git_head(self) -> Optional[str]:
        try:
            return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(self.cfg.REPO_DIR),
                                  capture_output=True, text=True, timeout=30,
                                  creationflags=_NO_WINDOW).stdout.strip() or None
        except Exception:
            return None

    # ---------------------------------------------------------------- POST /guard
    def guard(self, body: Dict[str, Any]) -> Dict[str, Any]:
        path = Path(str(body.get("path", "")))
        # Temp / hidden locations (download fragments, editor scratch) and non-video names are skipped
        # quietly, and so is a file that vanished before we got to it: none of these deserve an alert.
        if any(part.startswith(".") for part in path.parts) or path.suffix.lower() not in self.cfg.VIDEO_EXTENSIONS:
            return {"skip": True, "reason": "not_video"}
        if not path.is_file():
            return {"skip": True, "reason": "gone"}
        digest = file_hash(path)
        if self.ledger.is_processed(digest) and "_redo" not in path.name.lower():   # *_redo* = clip it again
            return {"skip": True, "reason": "already_processed", "hash": digest}
        try:
            info = audio_mod.probe(path)
        except Exception as exc:
            raise StageError("guard", f"ffprobe failed: {exc}", "check that ffmpeg/ffprobe are on PATH")
        if not info["has_video"]:
            return {"skip": True, "reason": "not_video"}
        if info["duration_sec"] < self.cfg.MIN_SOURCE_SEC:
            return {"skip": True, "reason": "too_short", "duration_sec": info["duration_sec"]}
        return {"skip": False, "hash": digest, "duration_sec": info["duration_sec"]}

    # ---------------------------------------------------------------- POST /ingest
    def ingest(self, body: Dict[str, Any]) -> Dict[str, Any]:
        src = Path(str(body.get("path", "")))
        if not src.is_file():
            raise StageError("ingest", f"file not found: {src}", None)
        digest = file_hash(src)
        job_id = f"{src.stem}_{time.strftime('%Y%m%d_%H%M%S')}"
        job_dir = self.cfg.WORKING / job_id
        n = 1
        while job_dir.exists():
            n += 1
            job_dir = self.cfg.WORKING / f"{job_id}_{n}"
        job_id = job_dir.name
        job_dir.mkdir(parents=True, exist_ok=True)
        dst = job_dir / f"source{src.suffix.lower()}"
        # Files dropped in INBOX are consumed (moved). Anything else — OBS/ShadowPlay capture folders that
        # are watched in place — is left where it is: hard-link when on the same volume (instant, no extra
        # disk), else copy.
        try:
            in_inbox = src.resolve().parent == self.cfg.INBOX.resolve()
        except OSError:
            in_inbox = False
        if in_inbox:
            shutil.move(str(src), str(dst))
        else:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        try:
            info = audio_mod.probe(dst)
        except Exception as exc:
            raise StageError("ingest", f"ffprobe failed on {dst}: {exc}", None)
        meta = {
            "job_id": job_id, "job_dir": str(job_dir), "source": str(dst),
            "original": src.name, "original_stem": src.stem, "original_path": str(src),
            "hash": digest, "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **info,
        }
        (job_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {
            "job_id": job_id, "job_dir": str(job_dir), "source": str(dst),
            "duration_sec": info["duration_sec"], "width": info["width"], "height": info["height"],
            "fps": info["fps"], "audio_tracks": info["audio_tracks"],
        }

    # ---------------------------------------------------------------- POST /transcribe
    def transcribe(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_dir = self.job_dir(str(body.get("job_id", "")))
        meta = self.meta(job_dir)
        # Re-runs of the same recording (same content hash) reuse the earlier transcript instead of
        # spending another Whisper pass — a 5 h VOD is ~10 min of GPU time.
        digest = meta.get("hash")
        if digest and not (job_dir / "transcript.json").exists() and not body.get("force"):
            for other in sorted(self.cfg.WORKING.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
                if other == job_dir or not (other / "transcript.json").exists():
                    continue
                try:
                    if json.loads((other / "meta.json").read_text(encoding="utf-8")).get("hash") == digest:
                        for name in ("transcript.json", "transcript.srt"):
                            if (other / name).exists():
                                shutil.copy2(other / name, job_dir / name)
                        words = transcribe_mod.load_words(job_dir / "transcript.json")
                        return {"transcript": str(job_dir / "transcript.json"), "srt": str(job_dir / "transcript.srt"),
                                "words": len(words), "device": "cache", "reused_from": other.name}
                except (OSError, ValueError):
                    continue
        try:
            return transcribe_mod.transcribe_job(
                job_dir, Path(meta["source"]),
                model_name=str(body.get("model") or self.cfg.WHISPER_MODEL),
                device=str(body.get("device") or self.cfg.WHISPER_DEVICE),
                python_exe=str(self.cfg.PYTHON_EXE),
                device_cache=self.cfg.WHISPER_DEVICE_CACHE,
                media_sec=float(meta.get("duration_sec") or 0.0) or None,
            )
        except transcribe_mod.TranscribeError as exc:
            raise StageError("transcribe", str(exc), exc.fix)

    # ---------------------------------------------------------------- POST /score
    def score(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_dir = self.job_dir(str(body.get("job_id", "")))
        meta = self.meta(job_dir)
        target_len = float(body.get("target_len") or self.cfg.TARGET_LEN_SEC)
        words = self.words(job_dir, "score")
        try:
            sig = self.signals(job_dir, Path(meta["source"]))
        except Exception as exc:
            raise StageError("score", f"audio analysis failed: {exc}", "check ffmpeg; install numpy for speed")
        duration = float(meta.get("duration_sec") or sig.get("duration") or 0.0)
        cands = score_moments.score_moments(
            words, sig["rms"], duration, target_len=target_len, onsets=sig["onsets"],
            hop_s=sig.get("hop_s", 0.1), v1_used=bool(sig.get("v1_used")),
            events=sig.get("events") or [],
            top_n=self.candidate_count(duration, body.get("top_n")),
        )
        (job_dir / "candidates.json").write_text(json.dumps(cands, indent=1), encoding="utf-8")
        strong = [c for c in cands if c.get("strong_moment")]
        verdict = ("ok" if strong else
                   "no_strong_moment: no game action found in this VOD — the candidates below are talk only")
        return {"candidates": cands, "target_len": target_len, "v1_used": bool(sig.get("v1_used")),
                "words": len(words), "strong_count": len(strong), "verdict": verdict}

    # ---------------------------------------------------------------- POST /predict
    def predict(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_dir = self.job_dir(str(body.get("job_id", "")))
        meta = self.meta(job_dir)
        cands = body.get("candidates")
        if not isinstance(cands, list) or not cands:
            raise StageError("predict", "candidates list is required", "pass the /score candidates")
        words = self.words(job_dir, "predict")
        try:
            sig = self.signals(job_dir, Path(meta["source"]))
        except Exception as exc:
            raise StageError("predict", f"audio analysis failed: {exc}", None)
        target_len = float(body.get("target_len") or self.cfg.TARGET_LEN_SEC)
        sw, sh = int(meta.get("width") or 1920), int(meta.get("height") or 1080)
        # Visual dead-time check per window (menus / deploy maps / static screens the audio can't see).
        refs = visual.load_menu_refs(self.cfg.MENU_REFS)
        visuals = []
        for c in cands:
            try:
                visuals.append(visual.window_visual_stats(Path(meta["source"]), float(c["start"]), float(c["end"]),
                                                          sw, sh, refs=refs,
                                                          facecam_box=None if self._no_cam(meta) else self.cfg.FACECAM_BOX))
            except Exception:
                visuals.append(None)
        out, rec = score_clip.predict_all(
            cands, words, sig["rms"], hop_s=sig.get("hop_s", 0.1), target_len=target_len,
            source_w=sw, source_h=sh, visuals=visuals,
        )
        (job_dir / "candidates.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        return {"candidates": out, "recommended": rec}

    # ---------------------------------------------------------------- POST /ollama_verdict
    def ollama_verdict(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_dir = self.job_dir(str(body.get("job_id", "")))
        cands = body.get("candidates")
        if not isinstance(cands, list):
            raise StageError("ollama_verdict", "candidates list is required", None)
        tj = job_dir / "transcript.json"
        words = transcribe_mod.load_words(tj) if tj.exists() else []
        out = []
        for c in cands:
            cc = dict(c)
            try:
                s0, s1 = float(c.get("start", 0)), float(c.get("end", 0))
                spoken = " ".join(w["word"] for w in score_moments.strip_voice_commands(
                    [w for w in words if s0 <= float(w["start"]) < s1]))[:400]
                cc["llm"] = verdict_for_candidate(self.cfg.OLLAMA_URL, self.cfg.OLLAMA_MODEL, cc,
                                                  timeout=self.cfg.OLLAMA_TIMEOUT_SEC, spoken=spoken)
            except Exception as exc:  # never raise on LLM failure
                cc["llm"] = None
                cc["llm_error"] = str(exc)
            out.append(cc)
        return {"candidates": out, "model": self.cfg.OLLAMA_MODEL}

    # ---------------------------------------------------------------- POST /render
    def render(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_dir = self.job_dir(str(body.get("job_id", "")))
        meta = self.meta(job_dir)
        try:
            rank = int(body.get("rank", 1))
            start = float(body["start"])
            end = float(body["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StageError("render", f"start/end/rank are required numbers: {exc}", None)
        duration = end - start
        if duration <= 0:
            raise StageError("render", "end must be after start", None)
        use_captions = bool(body.get("captions", True))
        offset = int(body.get("offset", 0) or 0)
        audio_mode = str(body.get("audio_mode") or "mix_all")
        volume = float(body.get("volume", 0.0) or 0.0)

        source = Path(meta["source"])
        # Each render gets its own work dir: renders for several picks can run concurrently (n8n fires
        # one request per item) and the CLI writes a fixed output name, so sharing job_dir lost clips.
        work = job_dir / f"render_clip{rank}"
        work.mkdir(parents=True, exist_ok=True)
        # Layout: a landscape stream recording gets the facecam-top / gameplay-centre "stream" layout (a 16:9
        # frame letterboxed in the middle of a Short is unwatchable); vertical sources keep the classic blur.
        layout = str(body.get("layout") or self.cfg.LAYOUT or "auto").lower()
        if layout == "auto":
            sw, sh = int(meta.get("width") or 0), int(meta.get("height") or 0)
            layout = "stream" if sw > sh > 0 and not self._no_cam(meta) else "blur"
        captions_path: Optional[Path] = None
        if use_captions:
            tj = job_dir / "transcript.json"
            words = transcribe_mod.load_words(tj) if tj.exists() else []
            res = captions_mod.write_caption_files(words, start, end, work, layout=layout)
            captions_path = Path(res["ass"])

        main_py = self.cfg.REPO_DIR / "src" / "main.py"
        cmd = [str(self.cfg.PYTHON_EXE), str(main_py), str(source), "--preset", "shorts",
               "--start", f"{start:.3f}", "--duration", f"{duration:.3f}",
               "--audio-mode", audio_mode, "--volume", f"{volume:.1f}", "--offset", str(offset),
               "--hwaccel", "--layout", layout]
        if layout == "stream" and self.cfg.FACECAM_BOX:
            cmd += ["--facecam", str(self.cfg.FACECAM_BOX)]
        if captions_path:
            cmd += ["--captions", str(captions_path)]
        cmd += ["-o", str(work)]
        cli_str = subprocess.list2cmdline(cmd)

        expected = work / f"{source.stem}_short.mp4"
        if expected.exists():
            expected.unlink()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(self.cfg.REPO_DIR),
                                  creationflags=_NO_WINDOW, timeout=3600)
        except Exception as exc:
            raise StageError("render", f"could not run the CLI: {exc}", cli_str)
        stdout = proc.stdout or ""
        if proc.returncode != 0 or not expected.exists():
            tail = (stdout + "\n" + (proc.stderr or "")).strip()[-600:]
            raise StageError("render", f"render failed (exit {proc.returncode}): {tail}", cli_str)

        self.cfg.READY.mkdir(parents=True, exist_ok=True)
        length_s = int(round(duration))
        final = self.cfg.READY / f"{meta['original_stem']}_clip{rank}_{length_s}s.mp4"
        if final.exists():
            final.unlink()
        shutil.move(str(expected), str(final))

        encoder = _encoder_from_output_file(final) or _encoder_from_cli_output(stdout)
        return {"output": str(final), "cli": cli_str, "encoder": encoder, "layout": layout,
                "captions": str(captions_path) if captions_path else None, "duration": round(duration, 3)}

    # ---------------------------------------------------------------- POST /package
    def package(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_dir = self.job_dir(str(body.get("job_id", "")))
        meta = self.meta(job_dir)
        output = Path(str(body.get("output", "")))
        if not output.is_file():
            raise StageError("package", f"rendered output not found: {output}", "run /render first")
        candidate = body.get("candidate") or {}
        rank = int(body.get("rank", candidate.get("rank", 1)))
        tj = job_dir / "transcript.json"
        words = transcribe_mod.load_words(tj) if tj.exists() else []
        try:
            result = capcut_package.build_package(self.cfg, job_dir, output, rank, candidate,
                                                 meta["original_stem"], words,
                                                 game=str(body.get("game") or self.cfg.GAME or "gameplay"),
                                                 channel=str(body.get("channel") or "SmarterGamers"))
        except Exception as exc:
            raise StageError("package", f"package build failed: {exc}", None)
        # Review preview for Telegram (bot uploads are capped at 50 MB): the full render when it fits,
        # otherwise a 720x1280 re-encode next to it in the package dir.
        try:
            result["preview"] = str(self._preview_for(output, Path(result["package_dir"])))
        except Exception as exc:  # a missing preview must never fail the package
            result["preview"] = None
            result["preview_error"] = str(exc)
        return result

    def _preview_for(self, output: Path, pkg_dir: Path) -> Path:
        limit = self.cfg.PREVIEW_MAX_MB * 1024 * 1024
        if output.stat().st_size <= limit:
            return output
        preview = pkg_dir / f"{output.stem}_preview.mp4"
        if preview.exists() and preview.stat().st_size <= limit:
            return preview
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(output), "-vf", "scale=720:-2", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", str(preview)]
        subprocess.run(cmd, capture_output=True, timeout=600, check=True)
        return preview

    SLOT_CLASH_MIN = 45         # two Shorts closer than this are a clash

    def _free_slot(self, publish_at: str) -> str:
        """The first configured publish slot at/after ``publish_at`` that is free and on a day that still has room.

        n8n picks slots from the clock only, per run; this is where all runs are reconciled against
        ``runs.jsonl``: no two Shorts within SLOT_CLASH_MIN of each other, at most ``MAX_PER_DAY`` per local
        day (``PUBLISH_TZ``), only on ``PUBLISH_SLOTS`` times. Blank stays blank (post now).
        """
        if not publish_at:
            return publish_at
        from datetime import datetime, timedelta, timezone
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.cfg.PUBLISH_TZ)       # needs the `tzdata` package on Windows (requirements.txt)
        except Exception:
            sys.stderr.write(f"[runner] WARNING: zoneinfo has no {self.cfg.PUBLISH_TZ} (pip install tzdata); "
                             "publish slots computed in UTC\n")
            tz = timezone.utc

        def parse(v):
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
            except (ValueError, TypeError):
                return None
        want = parse(publish_at)
        if want is None:
            return publish_at
        taken = []
        try:
            for line in self.cfg.RUN_LOG.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    t = parse((json.loads(line).get("posted") or {}).get("publish_at"))
                    if t:
                        taken.append(t)
        except (OSError, ValueError):
            pass
        # Uploads from the same run reach /upload before any of them is logged, so remember what this
        # process already handed out (a reservation lives for 6 h, long enough for the batch to be logged).
        now = datetime.now(timezone.utc)
        self._reserved = [t for t in getattr(self, "_reserved", []) if (now - t[1]) < timedelta(hours=6)]
        taken += [t for t, _ in self._reserved]
        slots = []
        for part in str(self.cfg.PUBLISH_SLOTS).split(","):
            try:
                hh, mm = part.strip().split(":")
                slots.append((int(hh), int(mm)))
            except ValueError:
                continue
        slots = sorted(slots) or [(10, 0), (13, 0), (16, 0), (19, 0)]
        clash = timedelta(minutes=self.SLOT_CLASH_MIN)
        day = want.astimezone(tz).date()
        for _ in range(60):                                   # up to two months out
            on_day = sum(1 for t in taken if t.astimezone(tz).date() == day)
            if on_day < int(self.cfg.MAX_PER_DAY):
                for hh, mm in slots:
                    cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
                    if cand < want:
                        continue
                    if any(abs(cand - t) < clash for t in taken):
                        continue
                    self._reserved.append((cand, now))
                    return cand.strftime("%Y-%m-%dT%H:%M:%SZ")
            day += timedelta(days=1)
        return want.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---------------------------------------------------------------- POST /upload
    def upload(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Posts one rendered clip to YouTube Shorts. Only called after an explicit "post" reply.

        Runs ``scripts/yt_post.py`` inside the Shorts pipeline's own venv so its OAuth token and uploader
        are reused as-is (no second credential, no copied code). Returns ``{video_id, url, privacy, title}``.
        """
        output = Path(str(body.get("output", "")))
        if not output.is_file():
            raise StageError("upload", f"clip not found: {output}", "render it first")
        if not self.cfg.YT_PYTHON.is_file():
            raise StageError("upload", f"YouTube pipeline python not found: {self.cfg.YT_PYTHON}",
                             "set SG_CLIPPER_YT_PYTHON / SG_CLIPPER_YT_PIPELINE_DIR")
        candidate = body.get("candidate") or {}
        privacy = str(body.get("privacy") or self.cfg.POST_PRIVACY or "public").lower()
        if privacy not in ("public", "unlisted", "private"):
            privacy = "public"
        job_dir = self.job_dir(str(body.get("job_id", "")))
        tj = job_dir / "transcript.json"
        words = transcribe_mod.load_words(tj) if tj.exists() else []
        start, end = float(candidate.get("start", 0)), float(candidate.get("end", 0))
        spoken = " ".join(w["word"] for w in score_moments.strip_voice_commands(
            [w for w in words if start <= w["start"] < end]))[:300]
        req = {
            "clip": str(output), "privacy": privacy,
            "title": str(body.get("title") or "").strip(),
            "hook": str((candidate.get("llm") or {}).get("hook_line") or ""),
            "llm_title": str((candidate.get("llm") or {}).get("title") or ""),
            "reason": str(candidate.get("reason") or ""),
            "spoken": spoken,
            "game": str(body.get("game") or self.cfg.GAME or ""),
            "original": self.meta(job_dir).get("original", output.stem),
            "publish_at": self._free_slot(str(body.get("publish_at") or "")),   # ISO UTC → scheduled
        }
        script = self.cfg.REPO_DIR / "scripts" / "yt_post.py"
        try:
            proc = subprocess.run([str(self.cfg.YT_PYTHON), "-X", "utf8", str(script)],
                                  input=json.dumps(req), capture_output=True, text=True, encoding="utf-8",
                                  cwd=str(self.cfg.YT_PIPELINE_DIR), timeout=900, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise StageError("upload", "YouTube upload timed out after 15 min", None)
        if proc.returncode != 0:
            raise StageError("upload", f"uploader failed: {(proc.stderr or proc.stdout).strip()[-600:]}", None)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise StageError("upload", f"uploader returned no JSON: {proc.stdout[-300:]}", None)
        return out

    # ---------------------------------------------------------------- POST /restart
    RESTART_EXIT_CODE = 3

    def restart(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Restarts the runner so it picks up new code — without anyone touching Task Scheduler.

        The scheduled task that hosts the runner is set to "restart on failure" (RestartCount 999, every
        minute), so the process simply answers this request, optionally ``git pull --ff-only``s the repo
        (``{"pull": true}``), and exits non-zero a moment later; Task Scheduler relaunches it under the same
        account within ~1 min. Deploy scripts call this after pushing; Brian never has to run an elevated
        Stop/Start-ScheduledTask again.
        """
        pulled = None
        if body.get("pull"):
            p = subprocess.run(["git", "pull", "--ff-only"], cwd=str(self.cfg.REPO_DIR), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=_NO_WINDOW)
            pulled = (p.stdout or p.stderr).strip()[-300:]
            if p.returncode != 0:
                raise StageError("restart", f"git pull failed: {pulled}", "fix the checkout, then POST /restart again")
        head = self._git_head()

        def _die() -> None:
            time.sleep(1.5)                       # let the response flush
            sys.stderr.write("[runner] restart requested -> exiting for Task Scheduler to relaunch\n")
            os._exit(self.RESTART_EXIT_CODE)
        threading.Thread(target=_die, daemon=True).start()
        return {"restarting": True, "head": head, "pulled": pulled,
                "note": "Task Scheduler relaunches the runner within ~1 min; poll GET /health"}

    # ---------------------------------------------------------------- POST /log
    def log(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(body.get("job_id", ""))
        record = {k: body.get(k) for k in ("job_id", "input", "candidates", "approved", "output",
                                           "package_dir", "status", "posted")}
        append_run_log(self.cfg.RUN_LOG, record)
        digest = None
        try:
            meta = self.meta(self.job_dir(job_id))
            digest = meta.get("hash")
        except StageError:
            inp = body.get("input")
            if inp and Path(str(inp)).is_file():
                digest = file_hash(Path(str(inp)))
        if digest:
            self.ledger.mark_processed(digest, job_id=job_id, input=body.get("input"),
                                       output=body.get("output"), status=body.get("status"))
        return {"hash": digest}

    # ---------------------------------------------------------------- POST /twitch_sync
    def twitch_sync(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Starts scripts/twitch_vods.py detached (downloads can take 30+ min) and returns at once.

        New VODs land in Inbox when complete; the Inbox watcher does the rest. Progress: twitch_sync.log.
        """
        script = self.cfg.REPO_DIR / "scripts" / "twitch_vods.py"
        args = [str(self.cfg.PYTHON_EXE), "-X", "utf8", str(script),
                "--channel", str(body.get("channel") or "smartergamers"),
                "--max", str(int(body.get("max", 1)))]
        if body.get("dry_run"):
            args.append("--dry-run")
        if body.get("rapid"):
            args += ["--rapid"]            # name the file live_*.mp4 so Auto Select posts the top picks at once
        if body.get("kick", True):
            args += ["--kick"]             # also Kick-only streams (Kick VODs with no matching Twitch archive)
        log_path = self.cfg.LEDGER.parent / "twitch_sync.log"
        with log_path.open("a", encoding="utf-8") as lf:
            subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT, cwd=str(self.cfg.REPO_DIR),
                             creationflags=_NO_WINDOW)
        return {"started": True, "log": str(log_path)}

    def candidate_count(self, duration_sec: float, override: Any = None) -> int:
        """How many moments to put up for the visual check + Ollama: CANDIDATES_PER_HOUR of footage, clamped.

        Five was too few for long streams: when the loudest five were downed/waiting screens the footage check
        rightly rejected them all and the VOD produced nothing. More candidates = the gate has real fights
        to choose from."""
        if override:
            return max(1, int(override))
        per_hour = float(self.cfg.CANDIDATES_PER_HOUR or 6)
        n = round(per_hour * max(0.0, duration_sec) / 3600.0)
        return max(int(self.cfg.CANDIDATES_MIN), min(int(self.cfg.CANDIDATES_MAX), n))

    @staticmethod
    def _no_cam(meta: Dict[str, Any]) -> bool:
        """Sources named ``*_nocam*`` (older uploads recorded without the webcam overlay) keep the classic
        blur layout: the stream layout would crop a slice of gameplay into the 'facecam' panel."""
        return "_nocam" in str(meta.get("original_stem") or meta.get("original") or "").lower()

    # ---------------------------------------------------------------- POST /stream_watch
    STREAM_END_GRACE_MIN = 10      # offline this long before we call the stream over (covers reconnects)
    TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # Twitch's public web client id, not a secret

    def _twitch_live(self, channel: str) -> Optional[Dict[str, Any]]:
        """``{"id", "started_at"}`` while ``channel`` is live on Twitch, ``None`` when offline.

        Raises StageError when Twitch can't be reached, so a network blip never reads as "stream ended".
        """
        import urllib.request
        q = json.dumps({"query": '{ user(login: "%s") { stream { id createdAt } } }' % channel}).encode()
        req = urllib.request.Request("https://gql.twitch.tv/gql", data=q, method="POST",
                                     headers={"Client-Id": self.TWITCH_WEB_CLIENT_ID,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            user = (data.get("data") or {}).get("user")
        except Exception as exc:  # noqa: BLE001
            raise StageError("stream_watch", f"Twitch status check failed: {exc}", "retries on the next poll")
        if user is None:
            raise StageError("stream_watch", f"Twitch user not found: {channel}", "check the channel name")
        s = user.get("stream")
        return {"id": s.get("id"), "started_at": s.get("createdAt")} if s else None

    def stream_watch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Polled every 5 min by n8n. When a Twitch stream has been over for STREAM_END_GRACE_MIN, starts
        ``/twitch_sync`` for that VOD in rapid mode: the file arrives in Inbox as ``live_twitch_*.mp4`` and the
        clipper's Auto Select posts its top ``AUTO_RAPID`` (3) picks immediately, the rest on the normal slots.

        State in ``<LEDGER dir>/stream_state.json`` so each stream triggers exactly once, across restarts.
        """
        from datetime import datetime, timezone
        channel = str(body.get("channel") or "smartergamers")
        path = self.cfg.LEDGER.parent / "stream_state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            state = {}
        now = datetime.now(timezone.utc)
        live = self._twitch_live(channel)
        result: Dict[str, Any] = {"live": bool(live), "ended": False, "sync_started": False}
        if live:
            if state.get("stream_id") != live["id"]:
                state = {"stream_id": live["id"], "started_at": live["started_at"]}
            state.update(live=True, last_seen=now.isoformat(), offline_since=None)
        elif state.get("live"):
            off = state.get("offline_since")
            if not off:
                state["offline_since"] = now.isoformat()
            elif (now - datetime.fromisoformat(off)).total_seconds() >= self.STREAM_END_GRACE_MIN * 60:
                state.update(live=False, ended_at=state.get("last_seen"), synced_at=now.isoformat())
                result["ended"] = True
                result["stream_id"] = state.get("stream_id")
                result["started_at"] = state.get("started_at")
                if not body.get("dry_run"):
                    self.twitch_sync({"channel": channel, "max": 1, "rapid": True})
                    result["sync_started"] = True
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        result["state"] = state
        return result

    # ---------------------------------------------------------------- POST /live_check
    def live_check(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Returns posted clips whose scheduled publish time has passed and that were not announced yet.

        Source: runs.jsonl ``posted`` blocks (``publish_at`` ISO UTC, or none = published immediately).
        Marks what it returns in ``<LEDGER dir>/live_notified.json`` so each clip is announced once.
        """
        path = self.cfg.LEDGER.parent / "live_notified.json"
        try:
            done = set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()
        except (OSError, ValueError):
            done = set()
        now = time.time()
        out: List[Dict[str, Any]] = []
        seen = set()
        try:
            lines = self.cfg.RUN_LOG.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            posted = rec.get("posted") or {}
            vid = posted.get("video_id")
            if not vid or vid in done or vid in seen:
                continue
            # pulled / private Shorts never "go live": no announcement, no TikTok/Facebook cross-post
            # (a scheduled Short is also 'private' until its publish_at: only skip private ones with no schedule)
            if rec.get("status") == "pulled" or (str(posted.get("privacy") or "").lower() == "private"
                                                 and not posted.get("publish_at")):
                continue
            pub = posted.get("publish_at")
            if pub:
                try:
                    ts = time.mktime(time.strptime(pub.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")) - time.timezone
                except ValueError:
                    ts = now
                if ts > now:
                    continue
            seen.add(vid)
            out.append({"video_id": vid, "url": posted.get("url"), "title": posted.get("title"),
                        "publish_at": pub, "job_id": rec.get("job_id"), "rank": (rec.get("approved") or {}).get("rank"),
                        "output": rec.get("output"), "source": os.path.splitext(str(rec.get("input") or "").replace("\\", "/").rsplit("/", 1)[-1])[0]})
        queued = 0
        if out and not body.get("dry_run"):
            done |= {o["video_id"] for o in out}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(done)), encoding="utf-8")
            os.replace(tmp, path)
            if self.cfg.SOCIAL_CROSSPOST:
                queued = self._queue_social(out)
        return {"live": out, "social_queued": queued}

    def _queue_social(self, items: List[Dict[str, Any]]) -> int:
        """Hands just-live Shorts to scripts/social_post.py (TikTok + Facebook) via <Clips>/social_queue.json."""
        qpath = self.cfg.INBOX.parent / "social_queue.json"
        try:
            q = json.loads(qpath.read_text(encoding="utf-8-sig")) if qpath.exists() else []
        except (OSError, ValueError):
            q = []
        have = {x.get("video_id") for x in q}
        added = 0
        for it in items:
            if it.get("video_id") not in have and it.get("output"):
                q.append({k: it.get(k) for k in ("video_id", "output", "title", "source")})
                have.add(it["video_id"])
                added += 1
        if added:
            tmp = qpath.with_suffix(".tmp")
            tmp.write_text(json.dumps(q, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, qpath)
        return added

    # ---------------------------------------------------------------- POST /sweep
    def sweep(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Day-after sweep: privatize under-performing Shorts the clipper posted (scripts/yt_sweep.py).

        Candidates come from runs.jsonl records with a ``posted`` block. Never deletes; ``dry_run`` reports only.
        """
        if not self.cfg.YT_PYTHON.is_file():
            raise StageError("sweep", f"YouTube pipeline python not found: {self.cfg.YT_PYTHON}", None)
        cands: List[Dict[str, Any]] = []
        seen = set()
        try:
            for line in self.cfg.RUN_LOG.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                posted = rec.get("posted") or {}
                vid = posted.get("video_id")
                if vid and vid not in seen:
                    seen.add(vid)
                    cands.append({"video_id": vid, "url": posted.get("url"), "title": posted.get("title"),
                                  "posted_at": rec.get("logged_at")})
        except OSError:
            pass
        req = {"candidates": cands, "dry_run": bool(body.get("dry_run", False)),
               "min_age_hours": float(body.get("min_age_hours", 20)), "max_age_hours": float(body.get("max_age_hours", 48)),
               "floor_fraction": float(body.get("floor_fraction", 0.25)), "baseline_n": int(body.get("baseline_n", 20))}
        script = self.cfg.REPO_DIR / "scripts" / "yt_sweep.py"
        proc = subprocess.run([str(self.cfg.YT_PYTHON), "-X", "utf8", str(script)], input=json.dumps(req),
                              capture_output=True, text=True, encoding="utf-8", cwd=str(self.cfg.YT_PIPELINE_DIR),
                              timeout=600, creationflags=_NO_WINDOW)
        if proc.returncode != 0:
            raise StageError("sweep", f"sweep failed: {(proc.stderr or proc.stdout).strip()[-600:]}", None)
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise StageError("sweep", f"sweep returned no JSON: {proc.stdout[-300:]}", None)
        out["candidates_considered"] = len(cands)
        return out

    # ---------------------------------------------------------------- POST /claim_reply
    _claim_lock = __import__("threading").Lock()

    def claim_reply(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """First-come claim of one Telegram update for one review job.

        Several reviews can be waiting in the same chat; every poll loop sees every message. A message that
        is not a Telegram reply-to a specific review is given to whichever job claims it first; the others
        are told ``claimed: false`` and ignore it. Claims live in ``<LEDGER dir>/reply_claims.json``, pruned
        after 48 h. Idempotent for the owner (re-claiming your own update returns ``claimed: true``).
        """
        update_id = str(body.get("update_id", "")).strip()
        job_id = str(body.get("job_id", "")).strip()
        if not update_id or not job_id:
            raise StageError("claim_reply", "update_id and job_id are required", None)
        path = self.cfg.LEDGER.parent / "reply_claims.json"
        now = time.time()
        with self._claim_lock:
            try:
                claims = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, ValueError):
                claims = {}
            claims = {k: v for k, v in claims.items() if now - float(v.get("at", 0)) < 48 * 3600}
            owner = claims.get(update_id, {}).get("job_id")
            if owner is None:
                claims[update_id] = {"job_id": job_id, "at": now}
                owner = job_id
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(claims, indent=1), encoding="utf-8")
                os.replace(tmp, path)
        return {"claimed": owner == job_id, "owner": owner}


def _encoder_from_output_file(path: Path) -> Optional[str]:
    """Reads the real encoder id from the rendered file's video stream tag (e.g. 'Lavc60 h264_nvenc').

    This is authoritative: the CLI silently falls back from GPU to libx264 on encoder errors.
    """
    ffprobe = audio_mod.find_ffprobe()
    if not ffprobe:
        return None
    try:
        res = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
                             capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=30)
        for st in json.loads(res.stdout).get("streams", []):
            if st.get("codec_type") == "video":
                tag = str((st.get("tags") or {}).get("encoder", "")).strip()
                if tag:
                    return tag.split()[-1]
    except Exception:
        pass
    return None


def _encoder_from_cli_output(stdout: str) -> str:
    """Derives the encoder id from the CLI banner ('Best Encoder: NVIDIA NVENC (H.264) ...')."""
    if "falling back to CPU" in stdout:
        return "libx264"
    for line in stdout.splitlines():
        if "Best Encoder:" in line:
            name = line.split("Best Encoder:", 1)[1].strip()
            table = {"NVIDIA NVENC (H.264)": "h264_nvenc", "NVIDIA NVENC (HEVC": "hevc_nvenc",
                     "NVIDIA NVENC (AV1)": "av1_nvenc", "Intel QuickSync (H.264)": "h264_qsv",
                     "Intel QuickSync (HEVC)": "hevc_qsv", "AMD AMF (H.264)": "h264_amf",
                     "AMD AMF (HEVC)": "hevc_amf"}
            for prefix, enc in table.items():
                if name.startswith(prefix):
                    return enc
            return "libx264"
    return "unknown"


# ======================================================================================
# HTTP layer
# ======================================================================================
def make_handler(stages: Stages) -> type:
    routes: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
        "/guard": stages.guard, "/ingest": stages.ingest, "/transcribe": stages.transcribe,
        "/score": stages.score, "/predict": stages.predict, "/ollama_verdict": stages.ollama_verdict,
        "/render": stages.render, "/package": stages.package, "/log": stages.log,
        "/claim_reply": stages.claim_reply, "/upload": stages.upload, "/sweep": stages.sweep, "/live_check": stages.live_check, "/stream_watch": stages.stream_watch, "/twitch_sync": stages.twitch_sync,
        "/restart": stages.restart,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "SGClipperRunner/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # compact access log
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _send(self, code: int, payload: Dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, stage: str, msg: str, fix: Optional[str]) -> None:
            self._send(500, {"ok": False, "stage": stage, "error": msg, "fix": fix})

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                try:
                    self._send(200, {"ok": True, **stages.health()})
                except Exception as exc:
                    self._error("health", str(exc), None)
            else:
                self._error("http", f"unknown GET route {path}", "use GET /health or POST a stage route")

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            fn = routes.get(path)
            if fn is None:
                self._error("http", f"unknown route {path}", f"one of: {', '.join(routes)}")
                return
            stage = path.strip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except Exception as exc:
                self._error(stage, f"bad JSON body: {exc}", None)
                return
            try:
                result = fn(body)
                self._send(200, {"ok": True, **result})
            except StageError as exc:
                self._error(exc.stage, str(exc), exc.fix)
            except Exception as exc:
                traceback.print_exc()
                self._error(stage, f"{type(exc).__name__}: {exc}", None)

    return Handler


def serve(cfg: ClipperConfig, host: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer:
    host = host or cfg.HOST
    port = port or cfg.PORT
    for d in (cfg.INBOX, cfg.WORKING, cfg.READY, cfg.LEDGER.parent):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    server = ThreadingHTTPServer((host, port), make_handler(Stages(cfg)))
    server.daemon_threads = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SmarterGamers Personal Clipper runner")
    parser.add_argument("--host", default=None, help="bind host (default from config: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default from config: 5690)")
    parser.add_argument("--log", default=None,
                        help="append stdout/stderr to this file (default: <LEDGER dir>\\runner.log; "
                             "'-' keeps the console). The scheduled task runs hidden, so without this "
                             "every access-log line and traceback is lost.")
    args = parser.parse_args(argv)
    cfg = load_config()
    log_path = args.log if args.log is not None else str(Path(cfg.LEDGER).parent / "runner.log")
    if log_path != "-":
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            fh = open(log_path, "a", encoding="utf-8", buffering=1)
            sys.stdout = fh
            sys.stderr = fh
        except OSError as exc:
            print(f"[runner] could not open log {log_path}: {exc}; logging to console", flush=True)
    server = serve(cfg, args.host, args.port)
    print(f"[runner] listening on http://{server.server_address[0]}:{server.server_address[1]} "
          f"(repo={cfg.REPO_DIR}, python={cfg.PYTHON_EXE})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
