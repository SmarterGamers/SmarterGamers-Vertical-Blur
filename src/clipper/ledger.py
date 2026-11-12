"""Idempotency ledger for the Personal Clipper.

A source file is identified by ``sha1(size, mtime, first 1 MB)``. The ledger is a JSON
object ``{"<hash>": {"path", "job_id", "output", "processed_at", ...}}`` written atomically
(temp file + ``os.replace``) so a crash mid-write can never corrupt it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

FIRST_MB = 1024 * 1024


def file_hash(path: Path) -> str:
    """sha1 over (size, mtime, first 1 MB) — cheap, stable, and good enough to spot duplicates."""
    st = path.stat()
    h = hashlib.sha1()
    h.update(f"{st.st_size}|{int(st.st_mtime)}|".encode("utf-8"))
    with open(path, "rb") as fh:
        h.update(fh.read(FIRST_MB))
    return h.hexdigest()


class Ledger:
    """Thread-safe JSON ledger of processed source hashes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- I/O -------------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)  # atomic on Windows and POSIX

    # -- API -------------------------------------------------------------
    def is_processed(self, digest: str) -> bool:
        with self._lock:
            return digest in self._read()

    def get(self, digest: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read().get(digest)

    def mark_processed(self, digest: str, **info: Any) -> None:
        """Records ``digest`` with any metadata (path, job_id, output, status ...)."""
        with self._lock:
            data = self._read()
            entry = dict(info)
            entry.setdefault("processed_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
            data[digest] = entry
            self._write(data)

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return self._read()


_RUN_LOG_LOCK = threading.Lock()


def append_run_log(path: Path, record: Dict[str, Any]) -> None:
    """Appends one JSON line to RUN_LOG (creating parent folders as needed).

    Serialised with a lock and written as one bytes call: n8n logs all of a job's Shorts at the same moment,
    and six concurrent text-mode appends of ~60 KB lines interleaved into a corrupt line (Nov 12)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(record)
    rec.setdefault("logged_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    data = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    with _RUN_LOG_LOCK:
        with open(path, "ab") as fh:
            fh.write(data)
            fh.flush()
