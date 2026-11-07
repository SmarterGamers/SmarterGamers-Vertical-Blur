"""Clipper Drop - the logic behind the desktop app (no tkinter here, so it is unit-testable).

The app does not clip anything itself. It feeds the pipeline that already runs on this PC:

    file / URL  ->  Clips\\Inbox  ->  n8n folder watcher  ->  runner (127.0.0.1:5690) + local Ollama
                ->  rendered Shorts in Clips\\Ready  ->  scheduled YouTube uploads (n8n auto mode)

Rules it follows (same as scripts/twitch_vods.py and vod_queue.ps1):
* Downloads land in ``<Clips>\\.app_tmp`` (outside Inbox) and are moved in only when complete, so the
  watcher never fires on a partial file.
* Items go in ONE AT A TIME: the next one waits until the clipper has picked up the previous item and the
  runner has been quiet for ``IDLE_MINUTES``. A long VOD otherwise competes with itself for the GPU.
* The queue survives restarts (``<Clips>\\app_queue.json``). Nothing is ever deleted: copied files stay
  where the user had them.
Free and local only: yt-dlp for URLs, ffmpeg for merging, the existing runner + Ollama for the clipping.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

IDLE_MINUTES = 3            # runner quiet this long after picking up the item -> clipper run finished
PICKUP_TIMEOUT_MIN = 30     # the watcher should create the Working folder within this
RUN_TIMEOUT_MIN = 240       # give up waiting on one item after this (long VODs take a while)
QUEUE_FILE = "app_queue.json"
TMP_DIR = ".app_tmp"

PENDING, DOWNLOADING, IN_INBOX, CLIPPING, DONE, FAILED = (
    "pending", "downloading", "in inbox", "clipping", "done", "failed")

_URL_RE = re.compile(r"^https?://\S+$", re.I)
_LOG_LINE_RE = re.compile(r'^\[(\d\d:\d\d:\d\d)\] "(?:POST|GET) (/\S*)')


# ----------------------------------------------------------------------------------------- pure helpers
def is_url(text: str) -> bool:
    """True for an http(s) link (Twitch/Kick/YouTube/anything yt-dlp understands)."""
    return bool(_URL_RE.match((text or "").strip()))


def safe_stem(text: str, limit: int = 40) -> str:
    """File-system-safe slug for Inbox names (ASCII letters, digits, dot, dash, underscore)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._")
    return s[:limit].strip("._") or "video"


def inbox_name(title: str, ext: str, now: Optional[datetime] = None) -> str:
    """``app_<YYYYmmdd-HHMMSS>_<slug><ext>`` - unique, sortable, and recognisable as app-fed."""
    now = now or datetime.now()
    ext = ext if ext.startswith(".") else "." + ext
    return f"app_{now:%Y%m%d-%H%M%S}_{safe_stem(title)}{ext.lower()}"


def parse_drop_data(data: str) -> List[str]:
    """Splits a Tk drag-and-drop payload: paths with spaces arrive wrapped in {braces}."""
    out = re.findall(r"\{([^}]*)\}|(\S+)", data or "")
    return [a or b for a, b in out if (a or b)]


def last_runner_activity(lines: Iterable[str], now: datetime) -> Optional[datetime]:
    """Time of the latest runner request that is real work (not the /health or /live_check polls).

    The runner's access log only stamps HH:MM:SS, so a time later than ``now`` belongs to yesterday.
    """
    last = None
    for line in lines:
        m = _LOG_LINE_RE.match(line)
        if m and not m.group(2).startswith(("/health", "/live_check")):
            last = m.group(1)
    if last is None:
        return None
    t = datetime.combine(now.date(), datetime.strptime(last, "%H:%M:%S").time())
    if t > now + timedelta(minutes=1):
        t -= timedelta(days=1)
    return t


def recent_uploads(lines: Iterable[str], limit: int = 20) -> List[Dict[str, Any]]:
    """Newest-first YouTube uploads from runs.jsonl: ``{title, url, publish_at, status}``."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        posted = rec.get("posted") or {}
        vid = posted.get("video_id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append({"title": posted.get("title") or "(untitled)",
                    "url": posted.get("url") or f"https://youtu.be/{vid}",
                    "publish_at": posted.get("publish_at") or "",
                    "status": rec.get("status") or posted.get("privacy") or ""})
    return list(reversed(out))[:limit]


# ------------------------------------------------------------------------------------------- the queue
class JobQueue:
    """Thread-safe list of jobs persisted as JSON (written atomically, UTF-8 without BOM)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.jobs: List[Dict[str, Any]] = []
        try:
            self.jobs = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            self.jobs = []
        # an app crash mid-item leaves it half-done: put it back in line (an Inbox copy is reused)
        for j in self.jobs:
            if j.get("status") in (DOWNLOADING, CLIPPING):
                j["status"] = IN_INBOX if j.get("inbox") else PENDING

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.jobs, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, source: str) -> Dict[str, Any]:
        job = {"id": uuid.uuid4().hex[:8], "kind": "url" if is_url(source) else "file",
               "source": source.strip(), "status": PENDING, "detail": "",
               "added": datetime.now().isoformat(timespec="seconds"), "inbox": None}
        with self._lock:
            self.jobs.append(job)
            self._save()
        return job

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            for j in self.jobs:
                if j["id"] == job_id:
                    j.update(fields)
            self._save()

    def next_job(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            for j in self.jobs:
                if j["status"] in (PENDING, IN_INBOX):
                    return dict(j)
        return None

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self.jobs]

    def retry(self, job_id: str) -> None:
        with self._lock:
            for j in self.jobs:
                if j["id"] == job_id and j["status"] == FAILED:
                    j.update(status=IN_INBOX if j.get("inbox") and Path(j["inbox"]).exists() else PENDING,
                             detail="retry")
            self._save()

    def clear_finished(self) -> None:
        """Drops done rows from the list only (files and uploads are untouched)."""
        with self._lock:
            self.jobs = [j for j in self.jobs if j["status"] != DONE]
            self._save()


# ------------------------------------------------------------------------------------------ the worker
def http_ok(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """GET ``url`` and return its JSON (``{}`` when not JSON), or None when unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return {}
    except Exception:  # noqa: BLE001 - any failure just means "down"
        return None


class Worker(threading.Thread):
    """Feeds the queue into the Inbox one item at a time. ``on_change`` is called after every update."""

    def __init__(self, cfg: Any, queue: JobQueue, on_change: Callable[[], None] = lambda: None):
        super().__init__(daemon=True, name="clipper-drop-worker")
        self.cfg = cfg
        self.queue = queue
        self.on_change = on_change
        self.paused = threading.Event()
        self.stop = threading.Event()
        self.clips = Path(cfg.INBOX).parent

    # -- helpers --------------------------------------------------------------------------------------
    def _set(self, job: Dict[str, Any], **fields: Any) -> None:
        self.queue.update(job["id"], **fields)
        job.update(fields)
        self.on_change()

    def _sleep(self, sec: float) -> bool:
        """Sleeps unless stopping; returns False when the app is closing."""
        return not self.stop.wait(sec)

    # -- stages ---------------------------------------------------------------------------------------
    def _bring_file(self, job: Dict[str, Any]) -> Path:
        src = Path(job["source"])
        if not src.is_file():
            raise RuntimeError(f"file not found: {src}")
        if src.suffix.lower() not in tuple(self.cfg.VIDEO_EXTENSIONS):
            raise RuntimeError(f"not a video type the clipper takes ({src.suffix})")
        tmp = self.clips / TMP_DIR
        tmp.mkdir(parents=True, exist_ok=True)
        staged = tmp / inbox_name(src.stem, src.suffix)
        self._set(job, status=DOWNLOADING, detail="copying")
        shutil.copy2(src, staged)          # copy, never move: the user's original stays put
        return staged

    def _bring_url(self, job: Dict[str, Any]) -> Path:
        import yt_dlp  # local, free; already a clipper dependency

        tmp = self.clips / TMP_DIR
        tmp.mkdir(parents=True, exist_ok=True)
        key = job["id"]
        last = [0.0]

        def hook(d: Dict[str, Any]) -> None:
            if d.get("status") == "downloading" and time.time() - last[0] > 1:
                last[0] = time.time()
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = f"{100 * d.get('downloaded_bytes', 0) / total:.0f}%" if total else ""
                self._set(job, detail=f"downloading {pct}".strip())

        opts = {"format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                "merge_output_format": "mp4", "outtmpl": str(tmp / f"{key}.%(ext)s"),
                "concurrent_fragment_downloads": 4, "quiet": True, "no_warnings": True,
                "noprogress": True, "progress_hooks": [hook], "noplaylist": True}
        self._set(job, status=DOWNLOADING, detail="reading link")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["source"], download=True)
        got = sorted(tmp.glob(f"{key}.mp4")) or sorted(p for p in tmp.glob(f"{key}.*")
                                                       if p.suffix.lower() in self.cfg.VIDEO_EXTENSIONS)
        if not got:
            raise RuntimeError("download finished but no video file was produced")
        title = (info or {}).get("title") or (info or {}).get("id") or "video"
        site = safe_stem((info or {}).get("extractor_key") or "web", 12).lower()
        final = tmp / inbox_name(f"{site}_{title}", got[0].suffix)
        got[0].rename(final)
        self._set(job, title=title)
        return final

    def _into_inbox(self, job: Dict[str, Any], staged: Path) -> Path:
        dest = Path(self.cfg.INBOX) / staged.name
        Path(self.cfg.INBOX).mkdir(parents=True, exist_ok=True)
        for _ in range(30):                       # the file can be briefly locked by AV / indexer
            try:
                shutil.move(str(staged), str(dest))
                break
            except OSError:
                if not self._sleep(10):
                    raise RuntimeError("app closing")
        else:
            raise RuntimeError(f"could not move into Inbox: {staged}")
        self._set(job, status=IN_INBOX, inbox=str(dest), detail="waiting for the clipper")
        return dest

    def _wait_for_clipper(self, job: Dict[str, Any]) -> bool:
        stem = Path(job["inbox"]).stem
        working = Path(self.cfg.WORKING)
        start = datetime.now()
        while not any(p.is_dir() and p.name.startswith(stem) for p in working.glob(f"{stem}*")):
            if datetime.now() - start > timedelta(minutes=PICKUP_TIMEOUT_MIN):
                self._set(job, status=FAILED,
                          detail="not picked up: n8n is off, or this exact video was clipped before")
                return False
            if not self._sleep(15):
                return False
        picked = datetime.now()
        self._set(job, status=CLIPPING, detail="transcribing, scoring with Ollama, rendering")
        log = self.clips / "runner.log"
        while datetime.now() - picked < timedelta(minutes=RUN_TIMEOUT_MIN):
            if not self._sleep(30):
                return False
            try:
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
            except OSError:
                tail = []
            now = datetime.now()
            last = last_runner_activity(tail, now)
            if last and last > picked - timedelta(minutes=1) and now - last >= timedelta(minutes=IDLE_MINUTES):
                return True
        return True    # timed out waiting for quiet; move on rather than stall the queue forever

    def _shorts_for(self, job: Dict[str, Any]) -> int:
        stem = Path(job["inbox"]).stem
        try:
            lines = Path(self.cfg.RUN_LOG).read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        return sum(1 for ln in lines if stem in ln and '"video_id"' in ln)

    # -- loop -----------------------------------------------------------------------------------------
    def run(self) -> None:
        while not self.stop.is_set():
            job = None if self.paused.is_set() else self.queue.next_job()
            if not job:
                self._sleep(2)
                continue
            try:
                if job["status"] == PENDING:
                    staged = self._bring_url(job) if job["kind"] == "url" else self._bring_file(job)
                    self._into_inbox(job, staged)
                if self._wait_for_clipper(job):
                    n = self._shorts_for(job)
                    self._set(job, status=DONE,
                              detail=f"{n} Short(s) scheduled" if n else "finished - no moment cleared the bar")
            except Exception as exc:  # noqa: BLE001 - shown in the list; the queue keeps going
                if not self.stop.is_set():
                    self._set(job, status=FAILED, detail=str(exc)[:200])
