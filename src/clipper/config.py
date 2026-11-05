"""Runner-side configuration for the Personal Clipper (single source of truth).

Every default mirrors the table in ``n8n/CONTRACT.md``. Each field can be overridden with an
environment variable named ``SG_CLIPPER_<FIELD>`` (upper-case field name), e.g.::

    set SG_CLIPPER_INBOX=D:\\Clips\\Inbox
    set SG_CLIPPER_TARGET_LEN_SEC=60

No other module may hardcode a path; everything goes through :func:`load_config`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

ENV_PREFIX = "SG_CLIPPER_"

# Repo root = two levels up from this file (src/clipper/config.py -> repo).
_REPO_DIR_DEFAULT = Path(__file__).resolve().parent.parent.parent


@dataclass
class ClipperConfig:
    """All runner settings. Field names (upper-cased) double as the env-var suffix."""

    # Folders / files
    INBOX: Path = Path(r"C:\SmarterGamers\Clips\Inbox")
    WORKING: Path = Path(r"C:\SmarterGamers\Clips\Working")
    READY: Path = Path(r"C:\SmarterGamers\Clips\Ready")
    LEDGER: Path = Path(r"C:\SmarterGamers\Clips\processed.json")
    RUN_LOG: Path = Path(r"C:\SmarterGamers\Clips\runs.jsonl")

    # Services
    RUNNER_URL: str = "http://host.docker.internal:5690"
    OLLAMA_URL: str = "http://host.docker.internal:11434"
    OLLAMA_MODEL: str = "llama3.2:latest"
    SOCIAL_WEBHOOK_URL: str = "http://localhost:5678/webhook/smartergamers-clip"

    # Posting (only ever on an explicit "post" reply in the review chat). Reuses the existing YouTube
    # uploader + OAuth token of the Shorts pipeline; nothing is duplicated and no new credential is created.
    YT_PIPELINE_DIR: Path = Path(r"C:\Users\ultim\Desktop\Projects\SmarterGamers\smartergamers_antigravity")
    YT_PYTHON: Path = Path(r"C:\Users\ultim\Desktop\Projects\SmarterGamers\smartergamers_antigravity\.venv\Scripts\python.exe")
    POST_PRIVACY: str = "public"       # public | unlisted | private — what "post" publishes as
    PREVIEW_MAX_MB: int = 45           # Telegram bot uploads are capped at 50 MB; previews are re-encoded under this

    # Behaviour
    TARGET_LEN_SEC: int = 30          # 15 / 30 / 60
    REVIEW_CHANNEL: str = "telegram"
    REVIEW_TIMEOUT_HOURS: int = 24
    MENU_REFS: Path = Path(r"C:\SmarterGamers\menu_refs.json")  # known menu-screen dHashes (optional; from the upload gate)
    GAME: str = ""                     # name used in the social copy; blank = "gameplay" (streams rotate games)
    MIN_SOURCE_SEC: float = 20.0       # /guard: shorter sources are skipped as too_short
    WHISPER_MODEL: str = "small.en"
    WHISPER_DEVICE: str = "auto"       # auto (cuda then cpu, cached) | cuda | cuda:N (pin to GPU N) | cpu
    WHISPER_DEVICE_CACHE: Path = Path(r"C:\SmarterGamers\Clips\.whisper_device")

    # Runner binding (the runner itself; n8n reaches it through RUNNER_URL)
    HOST: str = "127.0.0.1"
    PORT: int = 5690

    # Optional external moment finder (v1)
    BF6_CLIPPER_PATH: Path = Path(r"C:\Users\ultim\Desktop\Projects\HistoryView.org\bf6_clipper.py")

    # Auto-resolved. PYTHON_EXE is the interpreter running the runner; the production venv is
    # C:\SmarterGamers\clipper-venv314 (system Python 3.14 — the miniconda 3.11 venv crashes
    # on ctranslate2/onnxruntime import).
    REPO_DIR: Path = field(default_factory=lambda: _REPO_DIR_DEFAULT)
    PYTHON_EXE: Path = field(default_factory=lambda: Path(sys.executable))

    # Ollama used by /ollama_verdict and the package builder
    OLLAMA_TIMEOUT_SEC: int = 60
    SOCIAL_TIMEOUT_SEC: int = 90

    VIDEO_EXTENSIONS: tuple = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts")

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly view (Paths become strings)."""
        out: Dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = str(v) if isinstance(v, Path) else (list(v) if isinstance(v, tuple) else v)
        return out


def _coerce(default: Any, raw: str) -> Any:
    """Converts an env-var string to the type of the dataclass default."""
    if isinstance(default, Path):
        return Path(raw)
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, tuple):
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return raw


def load_config(env: Optional[Dict[str, str]] = None) -> ClipperConfig:
    """Builds the config from defaults, then applies ``SG_CLIPPER_*`` overrides from ``env``."""
    env = os.environ if env is None else env
    cfg = ClipperConfig()
    for f in fields(cfg):
        key = ENV_PREFIX + f.name.upper()
        if key in env and env[key] != "":
            setattr(cfg, f.name, _coerce(getattr(cfg, f.name), env[key]))
    return cfg
