"""Unit tests for the predictor (src/predictor/score_clip.py). Run: python tests/test_predictor.py

Pure functions over synthetic transcripts + RMS arrays; no ffmpeg involved.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from predictor import score_clip as sc  # noqa: E402

HOP = 0.1


def words_from(text, start=0.2, step=0.35):
    out, t = [], start
    for w in text.split():
        out.append({"start": round(t, 3), "end": round(t + 0.25, 3), "word": w})
        t += step
    return out


def flat_rms(duration, level=0.2):
    return [level] * int(duration / HOP)


def test_hook_prefers_reaction_over_generic_intro():
    generic = words_from("hey guys welcome back to the channel today we are playing battlefield six again and it is fun")
    reaction = words_from("NO WAY! he just clutched that one versus five with ten hp left insane!")
    rms = flat_rms(30)
    rms[:20] = [0.5] * 20  # louder open for the reaction clip
    g_hook, g_notes = sc.score_hook(generic, flat_rms(30))
    r_hook, r_notes = sc.score_hook(reaction, rms)
    assert r_hook > g_hook, f"reaction hook {r_hook} should beat generic {g_hook}"
    assert g_notes.get("generic_intro"), g_notes
    assert r_notes["payoff"] >= 2 and r_notes["exclaim"] >= 1
    assert 0 <= g_hook <= 40 and 0 <= r_hook <= 40
    print(f"[OK] hook: 'NO WAY he just...' = {r_hook}/40 > 'hey guys welcome back' = {g_hook}/40")


def test_hook_penalises_silent_open():
    late = words_from("what just happened", start=3.0)
    h, notes = sc.score_hook(late, flat_rms(30))
    early = words_from("what just happened", start=0.2)
    h2, _ = sc.score_hook(early, flat_rms(30))
    assert notes.get("silent_open") and h < h2
    print(f"[OK] hook: silent first 2 s scores {h} < {h2}")


def test_retention_dead_air_reports_lull():
    talk = words_from(" ".join(["we push the point and rotate left watching the flank for pushes"] * 7), start=0.2)
    talk = [w for w in talk if w["end"] < 29.8]  # continuous talk for the whole 30 s clip
    # same words but a 3 s gap inserted after the 4th word (words pushed past the clip end are dropped)
    gapped = []
    for i, w in enumerate(talk):
        shift = 3.0 if i >= 4 else 0.0
        if w["end"] + shift < 29.8:
            gapped.append({"start": w["start"] + shift, "end": w["end"] + shift, "word": w["word"]})
    quiet = flat_rms(30, 0.02)
    r_tight, lulls_tight, _ = sc.score_retention(talk, quiet, 30.0)
    r_gap, lulls_gap, notes = sc.score_retention(gapped, quiet, 30.0)
    assert r_gap < r_tight, f"gapped {r_gap} should be < tight {r_tight}"
    gap_start = talk[3]["end"]
    gap_end = talk[4]["start"] + 3.0
    found = [l for l in lulls_gap if abs(l["start"] - gap_start) < 0.05 and abs(l["end"] - gap_end) < 0.05]
    assert found, f"lull {gap_start}-{gap_end} not reported in {lulls_gap}"
    assert found[0]["end"] - found[0]["start"] > 1.2
    print(f"[OK] retention: 3 s dead air -> {r_gap}/30 < {r_tight}/30, lull reported at {found[0]}")


def test_retention_fillers():
    clean = words_from("we take the flag and hold it until the end of the round")
    filler = words_from("um we uh take the flag and like hold it um until the end")
    rc, _, _ = sc.score_retention(clean, flat_rms(20), 20.0)
    rf, _, notes = sc.score_retention(filler, flat_rms(20), 20.0)
    assert rf < rc and notes["fillers"] == 4
    print(f"[OK] retention: fillers lower the score ({rf} < {rc})")


def test_clarity_and_length():
    words = words_from("we win the round with a clutch play on the last point")
    rms = flat_rms(20, 0.1)
    for w in words:  # loud voice while speaking
        for i in range(int(w["start"] / HOP), int(w["end"] / HOP) + 1):
            rms[i] = 0.4
    c_loud, n_loud = sc.score_clarity(words, rms, source_w=1920, source_h=1080)
    c_buried, n_buried = sc.score_clarity(words, flat_rms(20, 0.3), source_w=1080, source_h=1920)
    assert c_loud > c_buried and n_loud["subject_visible_916"] is True and n_buried["subject_visible_916"] is False
    assert 0 <= c_loud <= 20
    assert sc.score_length(30)[0] == 10 and sc.score_length(70)[0] < sc.score_length(30)[0]
    assert "flag" in sc.score_length(70, target_len=30)[1] and "flag" not in sc.score_length(70, target_len=60)[1]
    print(f"[OK] clarity {c_loud} > {c_buried}; length_fit 30 s = 10, 70 s flagged")


def test_predict_all_recommends_and_biggest_fix():
    base = words_from("hey guys welcome back so today we play and then nothing", start=120.2)
    strong = words_from("NO WAY! did that just happen he clutched it with one hp", start=10.2)
    words = base + strong
    rms = flat_rms(200, 0.15)
    cands = [{"rank": 1, "start": 120.0, "end": 150.0}, {"rank": 2, "start": 10.0, "end": 40.0}]
    out, rec = sc.predict_all(cands, words, rms, target_len=30)
    # #2 is the better clip but scores below MIN_RECOMMEND_TOTAL on flat audio -> nothing is starred
    assert out[1]["predict"]["total"] > out[0]["predict"]["total"]
    assert rec is None, f"below the floor nothing should be recommended, got {rec}"
    # lower the floor and the better clip is recommended; a non-strong candidate never is
    saved = sc.MIN_RECOMMEND_TOTAL
    try:
        sc.MIN_RECOMMEND_TOTAL = 50
        _, rec2 = sc.predict_all(cands, words, rms, target_len=30)
        assert rec2 == 2, f"recommended should be rank 2 above the floor, got {rec2}"
        weak = [dict(cands[0]), dict(cands[1], strong_moment=False)]
        _, rec3 = sc.predict_all(weak, words, rms, target_len=30)
        assert rec3 in (None, 1), f"a talk-only candidate must not be recommended, got {rec3}"
    finally:
        sc.MIN_RECOMMEND_TOTAL = saved
    rec = 2
    for c in out:
        p = c["predict"]
        assert set(p) >= {"total", "hook", "retention", "clarity", "length_fit", "lulls", "biggest_fix"}
        assert p["total"] == p["hook"] + p["retention"] + p["clarity"] + p["length_fit"] <= 100
        assert isinstance(p["biggest_fix"], str) and p["biggest_fix"]
    assert "intro" in out[0]["predict"]["biggest_fix"] or "hook" in out[0]["predict"]["biggest_fix"]
    print(f"[OK] predict_all: recommended={rec}; fix for #1: {out[0]['predict']['biggest_fix']!r}")


def test_gameplay_open_is_a_hook_and_gunfire_is_not_dead_air():
    """A fight that opens cold on gunfire with no commentary must still score a hook, and the
    gaps between call-outs filled by gunfire must not count as dead air."""
    words = words_from("get him get him", start=12.0) + words_from("nice", start=24.0)
    rms = flat_rms(40, 0.15)
    onsets = [0.3, 0.9, 1.4, 3.0, 5.2, 7.7, 9.1, 15.0, 18.4, 21.0, 27.5]  # clip-relative, inside 0-30
    with_action = sc.predict_candidate(words, rms, 0.0, 30.0, target_len=30,
                                       signals={"onset_times": onsets, "firefight_peak_abs": 5.0})
    without = sc.predict_candidate(words, rms, 0.0, 30.0, target_len=30)
    assert with_action["hook"] >= 20 > without["hook"], (with_action["hook"], without["hook"])
    assert with_action["retention"] > without["retention"], (with_action["retention"], without["retention"])
    assert not any(l["start"] < 10 for l in with_action["lulls"]), with_action["lulls"]
    print(f"[OK] gameplay open: hook {without['hook']} -> {with_action['hook']}, retention {without['retention']} -> {with_action['retention']}")


if __name__ == "__main__":
    test_hook_prefers_reaction_over_generic_intro()
    test_hook_penalises_silent_open()
    test_retention_dead_air_reports_lull()
    test_retention_fillers()
    test_clarity_and_length()
    test_predict_all_recommends_and_biggest_fix()
    print("\nALL PREDICTOR TESTS PASSED SUCCESSFULLY!")
    test_gameplay_open_is_a_hook_and_gunfire_is_not_dead_air()
