"""Day-after performance sweep: privatize Shorts the clipper posted that are not performing.

Run by the clipper runner (``POST /sweep``) inside the Shorts pipeline's venv with cwd = the pipeline dir, so
``youtube_token_manage.json`` (youtube.force-ssl) is reused. Reads one JSON request on stdin::

    {"candidates": [{"video_id": "...", "url": "...", "posted_at": "ISO", "title": "..."}],
     "min_age_hours": 20, "max_age_hours": 48, "floor_fraction": 0.25, "baseline_n": 20, "dry_run": false}

Rules (never deletes):
  * only videos that are currently public and between min_age and max_age hours since publish,
  * baseline = median views of the channel's last ``baseline_n`` public Shorts older than max_age,
  * privatize when views < floor_fraction * baseline AND likes == 0 (a liked video is left alone),
  * ``dry_run`` reports without changing anything.
Prints one JSON line: {"checked": [...], "privatized": [...], "kept": [...], "baseline_views": n}.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

MANAGE_TOKEN = Path("youtube_token_manage.json")


def iso_dur(s: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    h, mi, se = (int(x) if x else 0 for x in m.groups()) if m else (0, 0, 0)
    return h * 3600 + mi * 60 + se


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    cands = req.get("candidates") or []
    min_age = float(req.get("min_age_hours", 20)) * 3600
    max_age = float(req.get("max_age_hours", 48)) * 3600
    floor = float(req.get("floor_fraction", 0.25))
    baseline_n = int(req.get("baseline_n", 20))
    dry = bool(req.get("dry_run", False))

    creds = Credentials.from_authorized_user_file(str(MANAGE_TOKEN))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    now = datetime.now(timezone.utc)

    # ---- baseline: channel's recent public Shorts (<= 60 s) older than max_age
    ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, tok = [], None
    while len(ids) < 120:
        r = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50, pageToken=tok).execute()
        ids += [i["contentDetails"]["videoId"] for i in r["items"]]
        tok = r.get("nextPageToken")
        if not tok:
            break
    base_views = []
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(ids[i:i + 50])).execute()
        for v in r["items"]:
            if v["status"].get("privacyStatus") != "public" or iso_dur(v["contentDetails"].get("duration")) > 60:
                continue
            pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
            if (now - pub).total_seconds() < max_age:
                continue
            base_views.append(int(v.get("statistics", {}).get("viewCount", 0)))
        if len(base_views) >= baseline_n:
            break
    base_views = sorted(base_views)
    baseline = base_views[len(base_views) // 2] if base_views else 0

    # ---- candidates
    out = {"baseline_views": baseline, "checked": [], "privatized": [], "kept": [], "skipped": []}
    cid = [c["video_id"] for c in cands if c.get("video_id")]
    stats = {}
    for i in range(0, len(cid), 50):
        r = yt.videos().list(part="snippet,statistics,status", id=",".join(cid[i:i + 50])).execute()
        for v in r["items"]:
            stats[v["id"]] = v
    for c in cands:
        v = stats.get(c.get("video_id"))
        if not v:
            out["skipped"].append({**c, "why": "not found"})
            continue
        pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
        age = (now - pub).total_seconds()
        st = v.get("statistics", {})
        views, likes = int(st.get("viewCount", 0)), int(st.get("likeCount", 0))
        row = {"video_id": v["id"], "url": f"https://youtu.be/{v['id']}", "title": v["snippet"]["title"],
               "views": views, "likes": likes, "age_hours": round(age / 3600, 1),
               "privacy": v["status"].get("privacyStatus")}
        if row["privacy"] != "public":
            out["skipped"].append({**row, "why": f"privacy is {row['privacy']} (not yet published or already pulled)"})
            continue
        if age < min_age:
            out["skipped"].append({**row, "why": "too new"})
            continue
        if age > max_age:
            out["skipped"].append({**row, "why": "older than the sweep window"})
            continue
        out["checked"].append(row)
        threshold = floor * baseline
        if views < threshold and likes == 0:
            if not dry:
                yt.videos().update(part="status", body={"id": v["id"], "status": {"privacyStatus": "private"}}).execute()
            out["privatized"].append({**row, "threshold": round(threshold, 1)})
        else:
            out["kept"].append({**row, "threshold": round(threshold, 1)})
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
