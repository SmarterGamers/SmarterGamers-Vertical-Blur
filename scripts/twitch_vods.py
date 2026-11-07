"""Pull new Twitch past broadcasts (own channel) into the clipper Inbox.

Lists the channel's archive VODs with yt-dlp (no login needed for public VODs), downloads the ones not
seen before into ``INBOX`` as ``twitch_<vod id>_<title>.mp4`` (best <= 1080p, mp4), and records them in
``<LEDGER dir>/twitch_vods.json``. The Inbox watcher then runs the normal clipper flow (auto mode: clip it
moments + best picks → scheduled Shorts). Downloads go to ``<Clips root>/.twitch_tmp`` (a sibling of Inbox,
never inside it) and are moved in only when complete, so the watcher never sees fragments or a half-written file.

Usage (run by the runner's /twitch_sync, or by hand):
    python scripts/twitch_vods.py [--channel smartergamers] [--max 1] [--rapid] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from clipper.config import load_config  # noqa: E402


def run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def list_archives(python, channel, limit):
    url = f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"
    p = run([python, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(limit),
             "--print", "%(id)s\t%(title)s\t%(duration)s", url], timeout=300)
    vods = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("v"):
            try:
                dur = float(parts[2]) if len(parts) > 2 and parts[2] not in ("NA", "") else 0.0
            except ValueError:
                dur = 0.0
            vods.append({"id": parts[0], "title": parts[1].strip(), "duration": dur})
    return vods


def list_kick(python, channel, limit):
    """Newest Kick VODs (``id``, ``title``, ``duration``, ``url``). Used when Twitch kept no past broadcast."""
    p = run([python, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(limit), "--no-warnings",
             "--print", "%(id)s\t%(title)s\t%(duration)s", f"https://kick.com/{channel}/videos"], timeout=300)
    vods = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            try:
                dur = float(parts[2]) if len(parts) > 2 and parts[2] not in ("NA", "") else 0.0
            except ValueError:
                dur = 0.0
            vods.append({"id": "kick_" + parts[0], "title": parts[1].strip(), "duration": dur,
                         "url": f"https://kick.com/{channel}/videos/{parts[0]}"})
    return vods


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="smartergamers")
    ap.add_argument("--max", type=int, default=1, help="max new VODs to download per run")
    ap.add_argument("--list-limit", type=int, default=5)
    ap.add_argument("--min-minutes", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rapid", action="store_true",
                    help="just-ended stream: name it live_twitch_*.mp4 so the clipper posts its top picks at once")
    ap.add_argument("--kick-channel", default="smartergamers",
                    help="with --rapid: fall back to this Kick channel's newest VOD when Twitch has nothing new")
    ap.add_argument("--skip", default="", help="comma-separated VOD ids to mark as done without downloading")
    args = ap.parse_args()

    cfg = load_config()           # honours SG_CLIPPER_* overrides (e.g. SG_CLIPPER_INBOX for a holding folder)
    python = str(cfg.PYTHON_EXE)
    ledger_path = cfg.LEDGER.parent / "twitch_vods.json"
    log_path = cfg.LEDGER.parent / "twitch_sync.log"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    except (OSError, ValueError):
        ledger = {}

    def log(msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        # VOD titles carry emoji; a cp1252 console must not kill the sync
        print(line.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8"), flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    for vid in [x.strip() for x in args.skip.split(",") if x.strip()]:
        ledger.setdefault(vid, {"title": "(skipped)", "skipped_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if args.skip:
        ledger_path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    vods = list_archives(python, args.channel, args.list_limit)
    log(f"archives listed: {len(vods)} ({', '.join(v['id'] for v in vods)})")
    new = [v for v in vods if v["id"] not in ledger and v["duration"] >= args.min_minutes * 60]
    if not new and args.rapid and args.kick_channel:
        # A stream just ended but Twitch kept no past broadcast (storage off / multistream): use the Kick VOD.
        kick = list_kick(python, args.kick_channel, args.list_limit)
        log(f"no new Twitch archive; Kick VODs listed: {len(kick)}")
        new = [v for v in kick if v["id"] not in ledger and v["duration"] >= args.min_minutes * 60][:1]
    if not new:
        log("nothing new")
        return 0
    # NOT inside Inbox: the n8n folder watcher would fire on every download fragment.
    tmp = cfg.INBOX.parent / ".twitch_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    done = 0
    for v in new[: args.max]:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", v["title"])[:40].strip("_") or "vod"
        site = "" if v["id"].startswith("kick_") else "twitch_"
        final = cfg.INBOX / f"{'live_' if args.rapid else ''}{site}{v['id']}_{safe}.mp4"
        log(f"downloading {v['id']} '{v['title']}' ({v['duration'] / 3600:.1f} h) -> {final.name}")
        if args.dry_run:
            continue
        out_tmpl = str(tmp / f"{v['id']}.%(ext)s")
        p = run([python, "-m", "yt_dlp", "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                 "--merge-output-format", "mp4", "--no-overwrites",
                 "--concurrent-fragments", "4", "-o", out_tmpl,
                 v.get("url") or f"https://www.twitch.tv/videos/{v['id'][1:]}"],
                timeout=4 * 3600)
        got = next(iter(sorted(tmp.glob(f"{v['id']}.mp4"))), None)
        if p.returncode != 0 or not got:
            log(f"FAILED {v['id']}: {(p.stderr or p.stdout)[-400:]}")
            continue
        shutil.move(str(got), str(final))
        ledger[v["id"]] = {"title": v["title"], "duration": v["duration"], "file": str(final),
                           "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        ledger_path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
        log(f"ready in Inbox: {final.name}")
        done += 1
    return 0 if done or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
