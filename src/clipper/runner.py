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
        }

    # ---------------------------------------------------------------- POST /guard
    def guard(self, body: Dict[str, Any]) -> Dict[str, Any]:
        path = Path(str(body.get("path", "")))
        if not path.is_file():
            raise StageError("guard", f"file not found: {path}", None)
        if path.suffix.lower() not in self.cfg.VIDEO_EXTENSIONS:
            return {"skip": True, "reason": "not_video"}
        digest = file_hash(path)
        if self.ledger.is_processed(digest):
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
                                                          sw, sh, refs=refs))
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
                spoken = " ".join(w["word"] for w in words if s0 <= float(w["start"]) < s1)[:400]
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
        captions_path: Optional[Path] = None
        if use_captions:
            tj = job_dir / "transcript.json"
            words = transcribe_mod.load_words(tj) if tj.exists() else []
            res = captions_mod.write_caption_files(words, start, end, job_dir)
            captions_path = Path(res["ass"])

        main_py = self.cfg.REPO_DIR / "src" / "main.py"
        cmd = [str(self.cfg.PYTHON_EXE), str(main_py), str(source), "--preset", "shorts",
               "--start", f"{start:.3f}", "--duration", f"{duration:.3f}",
               "--audio-mode", audio_mode, "--volume", f"{volume:.1f}", "--offset", str(offset),
               "--hwaccel"]
        if captions_path:
            cmd += ["--captions", str(captions_path)]
        cmd += ["-o", str(job_dir)]
        cli_str = subprocess.list2cmdline(cmd)

        expected = job_dir / f"{source.stem}_short.mp4"
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
        return {"output": str(final), "cli": cli_str, "encoder": encoder,
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
        spoken = " ".join(w["word"] for w in words if start <= w["start"] < end)[:300]
        req = {
            "clip": str(output), "privacy": privacy,
            "title": str(body.get("title") or "").strip(),
            "hook": str((candidate.get("llm") or {}).get("hook_line") or ""),
            "llm_title": str((candidate.get("llm") or {}).get("title") or ""),
            "reason": str(candidate.get("reason") or ""),
            "spoken": spoken,
            "game": str(body.get("game") or self.cfg.GAME or ""),
            "original": self.meta(job_dir).get("original", output.stem),
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

    # ---------------------------------------------------------------- POST /log
    def log(self, body: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(body.get("job_id", ""))
        record = {k: body.get(k) for k in ("job_id", "input", "candidates", "approved", "output",
                                           "package_dir", "status")}
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
        "/claim_reply": stages.claim_reply, "/upload": stages.upload,
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
