"""Post ONE rendered clip to YouTube Shorts using the Shorts pipeline's own uploader + OAuth token.

Run by the clipper runner (``POST /upload``) inside the pipeline's venv with cwd = the pipeline dir, so
``youtube_token.json`` / ``client_secret.json`` resolve exactly as they do for the daily uploader. Reads one
JSON request on stdin::

    {"clip": "C:\\...\\x_clip5_30s.mp4", "privacy": "public", "title": "", "hook": "...", "reason": "...",
     "spoken": "...", "game": "", "original": "Replay_2026-10-29...", "publish_at": "2026-11-06T15:00:00Z" | null}

``publish_at`` (ISO 8601 UTC) schedules the Short: YouTube requires privacyStatus=private with publishAt and
flips it to public itself at that time. Until then it is visible only in Studio (where it can be pulled).

and prints one JSON line: ``{"video_id", "url", "title", "privacy"}``. Never deletes or edits anything else.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from smartergamers_pipeline import youtube_upload as yu  # noqa: E402  (the existing uploader)

LIVE_CTA = getattr(yu, "LIVE_CTA_URL", "https://www.tiktok.com/@SmarterGamers/live")


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from clipper.captions import mask_profanity  # noqa: E402  (same word list as the burned captions)

_TITLE_PREFIXES = re.compile(r"^(live stream|stream highlight|clip|short)s?\s*[:\-–—]\s*", re.I)


def clean_title(t: str) -> str:
    """Advertiser-safe, hooky title: no profanity (the word is dropped, not starred), no 'Live Stream:' prefix."""
    words = []
    for w in t.split():
        masked = mask_profanity(w)
        if masked != w:          # profane -> drop the word entirely (a starred word reads badly in a title)
            continue
        words.append(w)
    t = " ".join(words)
    t = _TITLE_PREFIXES.sub("", t).strip(" -:–—")
    return re.sub(r"\s+", " ", t).strip()


def make_title(req: dict) -> str:
    t = req.get("title") or req.get("llm_title") or ""
    if not t:
        hook = (req.get("hook") or "").strip().strip('"').rstrip(".!")
        reason = (req.get("reason") or "").split(" + ")[0].strip()
        t = hook if 8 <= len(hook) <= 70 else (reason[:1].upper() + reason[1:] if reason else "Stream highlight")
    t = clean_title(t) or "Stream highlight"
    if "#shorts" not in t.lower():
        t = f"{t} #Shorts"
    return t[:100]


def make_description(req: dict) -> str:
    lines = []
    spoken = " ".join(mask_profanity(w) for w in (req.get("spoken") or "").split()).strip()
    if spoken:
        lines.append(f'"{spoken[:200]}"')
    if req.get("game"):
        lines.append(f"{req['game']} highlight from the live stream.")
    lines.append(f"Watch SmarterGamers LIVE daily - {LIVE_CTA}")
    lines.append("#Shorts #SmarterGamers #gaming")
    return "\n\n".join(lines)[:4900]


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    clip = Path(req["clip"])
    tags = list(getattr(yu, "BASE_TAGS", ["gaming", "gaming shorts", "SmarterGamers"]))
    if req.get("game"):
        tags.append(req["game"])
    metadata = {
        "snippet": {"title": make_title(req), "description": make_description(req),
                    "tags": yu.sanitize_tags(tags), "categoryId": getattr(yu, "GAMING_CATEGORY_ID", "20")},
        "status": {"privacyStatus": req.get("privacy", "public"), "selfDeclaredMadeForKids": False},
    }
    publish_at = (req.get("publish_at") or "").strip()
    if publish_at:
        metadata["status"]["privacyStatus"] = "private"      # YouTube's rule for scheduled videos
        metadata["status"]["publishAt"] = publish_at
    youtube = yu.get_authenticated_service()          # cached upload token; same as the daily task
    video_id = yu._upload_single(youtube, clip, metadata)
    print(json.dumps({"video_id": video_id, "url": f"https://youtu.be/{video_id}",
                      "title": metadata["snippet"]["title"], "privacy": metadata["status"]["privacyStatus"],
                      "publish_at": metadata["status"].get("publishAt"),
                      "studio_url": f"https://studio.youtube.com/video/{video_id}/edit"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
