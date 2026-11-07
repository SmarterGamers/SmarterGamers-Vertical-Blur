"""Tests for the Clipper Drop app logic (no Tk needed)."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from clipper_app.core import (DONE, DOWNLOADING, FAILED, IN_INBOX, PENDING, JobQueue,  # noqa: E402
                              inbox_name, is_url, last_runner_activity, parse_drop_data, recent_uploads)


def test_is_url():
    assert is_url("https://www.twitch.tv/videos/2892109899")
    assert is_url(" https://kick.com/smartergamers/videos/abc ")
    assert not is_url(r"C:\clips\a.mp4")
    assert not is_url("twitch.tv/videos/1")


def test_inbox_name_is_safe_and_keeps_ext():
    n = inbox_name("Hell Let Loose: TANK 💥 chase!", ".MKV", datetime(2026, 11, 6, 21, 5, 9))
    assert n == "app_20261106-210509_Hell_Let_Loose_TANK_chase.mkv"


def test_parse_drop_data_braces():
    assert parse_drop_data(r"{C:\My Clips\a b.mp4} C:\x.mp4") == [r"C:\My Clips\a b.mp4", r"C:\x.mp4"]


def test_last_runner_activity_ignores_polls_and_wraps_midnight():
    lines = ['[23:58:00] "POST /render HTTP/1.1" 200 -', '[00:01:00] "GET /health HTTP/1.1" 200 -',
             '[00:02:00] "POST /live_check HTTP/1.1" 200 -']
    t = last_runner_activity(lines, datetime(2026, 11, 7, 0, 3))
    assert t == datetime(2026, 11, 6, 23, 58)
    assert last_runner_activity(['[00:01:00] "GET /health HTTP/1.1"'], datetime(2026, 11, 7)) is None


def test_recent_uploads_newest_first_dedup():
    rows = [{"status": "scheduled", "posted": {"video_id": "a", "title": "A", "publish_at": "2026-11-07T15:00:00Z"}},
            {"status": "skipped"},
            {"posted": {"video_id": "b", "title": "B", "url": "https://youtu.be/b"}},
            {"posted": {"video_id": "a", "title": "A again"}}]
    ups = recent_uploads([json.dumps(r) for r in rows] + ["not json"])
    assert [u["title"] for u in ups] == ["B", "A"]
    assert ups[1]["url"] == "https://youtu.be/a"


def test_queue_persists_and_recovers(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a = q.add("https://www.twitch.tv/videos/1")
    b = q.add(str(tmp_path / "x.mp4"))
    assert a["kind"] == "url" and b["kind"] == "file"
    q.update(a["id"], status=DOWNLOADING)
    q.update(b["id"], status=FAILED, inbox=None)
    q2 = JobQueue(tmp_path / "q.json")          # app restarted mid-download
    assert q2.next_job()["id"] == a["id"] and q2.next_job()["status"] == PENDING
    q2.update(a["id"], status=DONE)
    assert q2.next_job() is None
    q2.retry(b["id"])
    assert q2.next_job()["id"] == b["id"]
    q2.clear_finished()
    assert [j["id"] for j in q2.snapshot()] == [b["id"]]
    assert not (tmp_path / "q.json").read_bytes().startswith(b"\xef\xbb\xbf")


def test_queue_resumes_inbox_item(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a = q.add("https://youtu.be/x")
    q.update(a["id"], status="clipping", inbox=str(tmp_path / "app_x.mp4"))
    assert JobQueue(tmp_path / "q.json").next_job()["status"] == IN_INBOX
