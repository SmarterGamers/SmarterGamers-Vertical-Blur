"""Unit tests for the Personal Clipper (src/clipper). Run: python tests/test_clipper.py

No ffmpeg needed: scoring, captions and the ledger are exercised with synthetic data.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipper import captions as cap  # noqa: E402
from clipper.config import load_config  # noqa: E402
from clipper.ledger import Ledger, file_hash  # noqa: E402
from clipper.score_moments import non_max_suppress, overlap_fraction, score_moments  # noqa: E402
from core.ffmpeg_engine import FFmpegEngine  # noqa: E402

HOP = 0.1


def synthetic_transcript(duration=180.0, burst_at=100.0):
    """Calm narration every 0.6 s plus a burst of exclamations around ``burst_at``."""
    words = []
    t = 0.5
    i = 0
    calm = ["so", "we", "push", "the", "objective", "and", "rotate", "left."]
    while t < duration:
        if burst_at <= t < burst_at + 8:
            burst = ["NO", "WAY!", "he", "just", "clutched", "that!", "insane!", "let's", "go!"]
            words.append({"start": t, "end": t + 0.25, "word": burst[i % len(burst)]})
            t += 0.3
        else:
            words.append({"start": t, "end": t + 0.3, "word": calm[i % len(calm)]})
            t += 0.6
        i += 1
    return words


def synthetic_rms(duration=180.0, burst_at=100.0):
    n = int(duration / HOP)
    rms = [0.05] * n
    for k in range(int(burst_at / HOP), int((burst_at + 10) / HOP)):
        rms[k] = 0.6
    return rms


def test_score_moments_ranks_burst_first():
    words = synthetic_transcript()
    rms = synthetic_rms()
    onsets = [100.4, 101.0, 101.9, 103.2, 104.0, 105.5]
    cands = score_moments(words, rms, 180.0, target_len=30, onsets=onsets)
    assert len(cands) == 5, f"expected 5 candidates, got {len(cands)}"
    top = cands[0]
    assert top["rank"] == 1
    assert top["start"] - 2.0 <= 100.0 < top["end"], f"#1 should open on the burst (within 2 s): {top}"
    assert "signals" in top and "reason" in top and "penalties" in top
    assert top["signals"]["v1_used"] is False
    assert "'" in top["reason"] or "loud" in top["reason"], top["reason"]
    # starts snap to a word boundary: 0.3 s before some word start
    starts = {round(w["start"] - 0.3, 2) for w in words}
    for c in cands:
        assert round(c["start"], 2) in starts or c["start"] == 0.0, f"start {c['start']} is not word-snapped"
    print(f"[OK] moment scoring ranks the burst window #1 ({top['start']}-{top['end']}: {top['reason']})")


def test_top5_do_not_overlap():
    words = synthetic_transcript()
    rms = synthetic_rms()
    cands = score_moments(words, rms, 180.0, target_len=30)
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            assert overlap_fraction(a, b) <= 0.25, f"{a['start']}-{a['end']} overlaps {b['start']}-{b['end']}"
    # NMS helper directly
    kept = non_max_suppress([{"start": 0, "end": 30, "score": 1}, {"start": 5, "end": 35, "score": .9},
                             {"start": 25, "end": 55, "score": .8}], max_overlap=0.25, top_n=5)
    assert [k["start"] for k in kept] == [0, 25]
    print("[OK] top-5 windows overlap <= 25%")


def test_penalties_recorded():
    # A: single 30 s window, speech only starts at 5 s and the open is quiet -> leading_silence
    words = [{"start": 5 + i * 0.5, "end": 5.3 + i * 0.5, "word": "word"} for i in range(40)]
    rms = [0.01] * 50 + [0.3] * 250
    cands = score_moments(words, rms, 30.0, target_len=30)
    assert cands and "leading_silence" in cands[0]["penalties"], cands[0]
    assert cands[0]["penalties"] == cands[0]["signals"]["penalties"]
    # B: continuous unpunctuated speech -> every window except the first starts mid-sentence
    words = [{"start": i * 0.5, "end": 0.3 + i * 0.5, "word": "word"} for i in range(120)]
    cands = score_moments(words, [0.2] * 600, 60.0, target_len=30)
    mid = [c for c in cands if "mid_sentence_start" in c["penalties"]]
    assert mid and all(c["start"] > 0 for c in mid), cands
    assert all("mid_sentence_start" not in c["penalties"] for c in cands if c["start"] == 0.0)
    assert all("(−" in c["reason"] for c in mid)
    # C: no speech and no onsets -> no_speech_no_onsets
    cands = score_moments([], [0.0] * 300, 30.0, target_len=30)
    assert "no_speech_no_onsets" in cands[0]["penalties"]
    print("[OK] penalties are reported: leading_silence / mid_sentence_start / no_speech_no_onsets")


def test_caption_safe_zone():
    words = synthetic_transcript()
    chunks = cap.caption_chunks(words, 95.0, 125.0)
    assert chunks, "no chunks"
    x0, y0, x1, y1 = cap.safe_zone_rect()
    assert x1 <= cap.PLAY_W - 120 and y1 <= int(cap.PLAY_H * 0.75)
    for c in chunks:
        assert 1 <= len(c.words) <= 3, c.text
        assert 0 <= c.start < c.end <= 30.0, (c.start, c.end)
        bx0, by0, bx1, by1 = cap.layout_chunk(c)["bbox"]
        assert x0 <= bx0 and bx1 <= x1 and y0 <= by0 and by1 <= y1, f"{c.text!r} bbox {(bx0, by0, bx1, by1)} outside zone"
    # a very long chunk still fits by shrinking the font
    long_chunk = cap.CaptionChunk(0, 1, [{"start": 0, "end": .3, "word": "Supercalifragilistic"},
                                         {"start": .3, "end": .6, "word": "expialidocious"},
                                         {"start": .6, "end": .9, "word": "extraordinary"}])
    lay = cap.layout_chunk(long_chunk)
    assert lay["bbox"][0] >= x0 and lay["bbox"][2] <= x1 and lay["font_size"] < cap.FONT_SIZE
    ass = cap.build_ass(chunks)
    assert "PlayResX: 1080" in ass and "\\pos(" in ass and "&H0000FFFF" in ass and "Outline" in ass
    srt = cap.build_srt(chunks)
    assert srt.startswith("1\n00:00:0")
    print(f"[OK] {len(chunks)} caption chunks all inside the safe zone {cap.safe_zone_rect()}")


def test_ledger_idempotent():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "clip.mp4"
        src.write_bytes(os.urandom(2048))
        ledger = Ledger(Path(td) / "sub" / "processed.json")
        h = file_hash(src)
        assert h == file_hash(src)
        assert not ledger.is_processed(h)
        ledger.mark_processed(h, job_id="j1", status="done")
        assert ledger.is_processed(h)  # second guard-equivalent -> processed
        ledger.mark_processed(h, job_id="j2")  # re-marking is harmless
        data = json.loads((Path(td) / "sub" / "processed.json").read_text())
        assert data[h]["job_id"] == "j2" and "processed_at" in data[h]
        assert not list((Path(td) / "sub").glob("*.tmp")), "temp file left behind"
    print("[OK] ledger hash is stable and second check returns processed")


def test_config_env_override():
    cfg = load_config({"SG_CLIPPER_INBOX": r"D:\X\Inbox", "SG_CLIPPER_TARGET_LEN_SEC": "60"})
    assert str(cfg.INBOX) == r"D:\X\Inbox" and cfg.TARGET_LEN_SEC == 60
    assert load_config({}).TARGET_LEN_SEC == 30
    assert str(load_config({}).LEDGER).endswith("processed.json")
    print("[OK] config env overrides (SG_CLIPPER_*) work")


def test_filtergraph_with_and_without_captions():
    engine = FFmpegEngine()
    kw = dict(target_w=1080, target_h=1920, blur_strength=25, blur_algorithm="boxblur",
              dim_factor=0.65, vertical_offset=-100, sharp_scale=1.0)
    plain = engine.build_filtergraph(**kw)
    # identical to what tests/test_engine.py expects, and no captions stage at all
    assert "boxblur" in plain and "colorlevels" in plain and "overlay=(W-w)/2:(H-h)/2+-100" in plain
    assert "subtitles=" not in plain and plain.endswith("[vout]") and "[vpre]" not in plain
    assert plain == engine.build_filtergraph(captions_path=None, **kw)

    with_caps = engine.build_filtergraph(captions_path=r"C:\SmarterGamers\Clips\Working\job\captions.ass", **kw)
    assert with_caps.startswith(plain[: plain.rfind("[vout]")])
    assert "[vpre];[vpre]subtitles='C\\:/SmarterGamers/Clips/Working/job/captions.ass'[vout]" in with_caps
    assert "\\" not in with_caps.split("subtitles='")[1].replace("\\:", "")

    cmd = engine.build_render_command(src=Path("in.mp4"), dst=Path("out.mp4"), encoder_id="libx264",
                                      captions_path="/tmp/c.ass")
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "subtitles='/tmp/c.ass'" in fc
    print("[OK] filtergraph: captions append subtitles=; without captions it is unchanged")


def test_talk_only_vod_is_not_a_strong_moment():
    """A stream-setup chat (speech, no action) must not be recommended: its onsets are speech."""
    from predictor.score_clip import predict_all
    words = synthetic_transcript(duration=150.0, burst_at=-100)    # calm talk, no burst
    rms = [0.08] * int(150.0 / HOP)                                # flat, quiet
    # onsets that all land ON words (plosives) — what a chat-only VOD produces
    onsets = [w["start"] + 0.05 for w in words[::3]]
    cands = score_moments(words, rms, 150.0, target_len=30, onsets=onsets)
    assert cands, "should still list candidates"
    assert all(not c["strong_moment"] for c in cands), [c["signals"] for c in cands]
    assert all("no_action" in c["penalties"] for c in cands)
    assert all(c["signals"]["onsets"] < 6 for c in cands), "speech onsets must not count as action"
    out, rec = predict_all(cands, words, rms, hop_s=HOP, target_len=30)
    assert rec is None, f"talk-only VOD must have no recommendation, got #{rec}"
    print("[OK] talk-only VOD: no strong moment, no recommendation, speech onsets discounted")


def test_v1_firefight_event_makes_window_strong():
    words = synthetic_transcript(duration=150.0, burst_at=-100)
    rms = [0.08] * int(150.0 / HOP)
    onsets = [60.2, 60.9, 61.5, 62.1, 63.0, 63.8, 64.4]            # between words → action
    events = [{"start": 60.0, "end": 65.0, "peak": 62.0, "onset_count": 7}]
    cands = score_moments(words, rms, 150.0, target_len=30, onsets=onsets, events=events)
    top = cands[0]
    assert top["start"] <= 60.0 < top["end"] and top["strong_moment"], top
    assert "firefight" in top["reason"], top["reason"]
    print("[OK] v1 firefight event marks the window as a strong moment")


def test_clip_it_voice_command():
    """Saying "clip it" makes the 30 s before it a strong, first-ranked candidate the predictor recommends."""
    from predictor.score_clip import predict_all
    from clipper.score_moments import find_voice_commands
    words = synthetic_transcript(duration=150.0, burst_at=-100)      # calm talk, no action anywhere
    # streamer says "clip it" at 100.0-100.6 s
    words = [w for w in words if not (99.5 <= w["start"] <= 101.0)]
    words += [{"start": 100.0, "end": 100.3, "word": "Clip"}, {"start": 100.35, "end": 100.6, "word": "it!"}]
    rms = [0.08] * int(150.0 / HOP)
    cmds = find_voice_commands(words)
    assert len(cmds) == 1 and cmds[0]["text"] == "clip it" and abs(cmds[0]["start"] - 100.0) < 1e-6
    cands = score_moments(words, rms, 150.0, target_len=30)
    top = cands[0]
    assert top["rank"] == 1 and top["strong_moment"] and top["signals"]["voice_command"]["text"] == "clip it"
    assert abs(top["end"] - 99.7) < 0.01 and abs(top["start"] - 69.7) < 0.01, top
    assert "you said 'clip it' at 1:40" in top["reason"], top["reason"]
    assert all(not c["strong_moment"] for c in cands[1:]), "plain chat windows stay non-strong"
    out, rec = predict_all(cands, words, rms, hop_s=HOP, target_len=30)
    assert rec == 1, f"voice command must be the recommendation, got {rec}"
    # Whisper variants and 'clip that' / 'clip this'
    for variant in (["clipit"], ["clip", "that"], ["Clip,", "this"]):
        ws = [{"start": 10 + 0.3 * i, "end": 10.25 + 0.3 * i, "word": v} for i, v in enumerate(variant)]
        assert find_voice_commands(ws), variant
    assert not find_voice_commands([{"start": 1, "end": 1.2, "word": "clip"}, {"start": 3.0, "end": 3.2, "word": "it"}]), "too far apart"
    print("[OK] 'clip it' -> the 30 s before it is candidate #1, strong, and the recommendation")


if __name__ == "__main__":
    test_score_moments_ranks_burst_first()
    test_top5_do_not_overlap()
    test_penalties_recorded()
    test_caption_safe_zone()
    test_ledger_idempotent()
    test_config_env_override()
    test_filtergraph_with_and_without_captions()
    test_talk_only_vod_is_not_a_strong_moment()
    test_v1_firefight_event_makes_window_strong()
    test_clip_it_voice_command()
    print("\nALL CLIPPER TESTS PASSED SUCCESSFULLY!")
