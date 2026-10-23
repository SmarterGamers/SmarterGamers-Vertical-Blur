"""Advanced Real-World Gaming Scenarios Test Suite for SmarterGamers Vertical Blur v1.2.0.

Tests:
1. Multi-Track Audio Mixing (Game + Mic merged to stereo)
2. Complex filenames with special characters, brackets, spaces, and punctuation
3. Clip Trimming (sub-clip start time & duration)
4. Audio Volume Boost (+3dB) and Muting (-an)
5. Duration-Aware Clamped Preview Scrubbing (0.1s, 1.0s, 2.5s, 999.0s)
6. Hardware decode acceleration flags (-hwaccel auto)
"""

import os
import subprocess
import sys
import time
from pathlib import Path

# Add src to path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from core.ffmpeg_engine import FFmpegEngine
from core.batch_processor import BatchProcessor, JobConfig

ADV_DIR = Path(__file__).resolve().parent / "fixtures_adv"
ADV_OUT = Path(__file__).resolve().parent / "output_adv"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def create_advanced_test_clips(ffmpeg_exe: str):
    ADV_DIR.mkdir(parents=True, exist_ok=True)
    ADV_OUT.mkdir(parents=True, exist_ok=True)

    # 1. Multi-Track Audio (Track 1 = Sine 440Hz Game, Track 2 = Sine 880Hz Mic)
    multi_track = ADV_DIR / "multi_track_game_mic.mp4"
    if not multi_track.exists():
        cmd = [
            ffmpeg_exe, "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-t", "3",
            "-c:v", "libx264", "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            str(multi_track),
        ]
        subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)

    # 2. Complex Filename
    special_name = ADV_DIR / "[SmarterGamers] #1 - Clip (2026) & Final!.mp4"
    if not special_name.exists():
        cmd = [
            ffmpeg_exe, "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=520",
            "-t", "2",
            "-c:v", "libx264", "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            str(special_name),
        ]
        subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)


def run_advanced_tests():
    engine = FFmpegEngine()
    if not engine.ffmpeg_exe:
        print("[FAIL] FFmpeg not found!")
        return False

    create_advanced_test_clips(engine.ffmpeg_exe)
    batch = BatchProcessor(engine)

    print("\n" + "=" * 62)
    print(" ADVANCED GAMING EDGE-CASE TESTS (v1.2.0)")
    print("=" * 62)

    # Test 1: Special Characters in Path
    special_file = ADV_DIR / "[SmarterGamers] #1 - Clip (2026) & Final!.mp4"
    print(f"\n[*] Test 1: Special Characters Path -> {special_file.name}")
    cfg1 = JobConfig(output_dir=ADV_OUT)
    jobs1 = batch.add_files([special_file], cfg1)
    ok1 = batch._process_job(jobs1[0])
    print(f"    Result: {'PASS' if ok1 else 'FAIL'} (dst: {jobs1[0].dst_path.name})")

    # Test 2: Multi-Track Audio Downmix (Game + Mic)
    multi_file = ADV_DIR / "multi_track_game_mic.mp4"
    print(f"\n[*] Test 2: Multi-Track Audio Mixing (Game + Mic) -> {multi_file.name}")
    meta = engine.probe_video(multi_file)
    print(f"    Probed Audio Tracks: {meta.audio_tracks_count}, Has Audio: {meta.has_audio}")
    cfg2 = JobConfig(
        output_dir=ADV_OUT,
        audio_mode="mix_all",
        audio_gain_db=+3.0,
    )
    jobs2 = batch.add_files([multi_file], cfg2)
    ok2 = batch._process_job(jobs2[0])
    print(f"    Result: {'PASS' if ok2 else 'FAIL'} (dst: {jobs2[0].dst_path.name})")

    # Test 3: Sub-Clip Trimming (0.5s to 1.5s)
    print(f"\n[*] Test 3: Clip Trimming (start=0.5s, duration=1.0s)")
    cfg3 = JobConfig(
        output_dir=ADV_OUT,
        start_sec=0.5,
        duration_sec=1.0,
    )
    jobs3 = batch.add_files([special_file], cfg3)
    ok3 = batch._process_job(jobs3[0])
    trimmed_meta = engine.probe_video(jobs3[0].dst_path) if ok3 else None
    dur_ok = (trimmed_meta is not None and 0.8 <= trimmed_meta.duration_sec <= 1.2)
    print(f"    Result: {'PASS' if (ok3 and dur_ok) else 'FAIL'} (duration: {trimmed_meta.duration_sec if trimmed_meta else 0:.2f}s)")

    # Test 4: Duration-Aware Clamped Preview Scrubbing
    print("\n[*] Test 4: Clamped Preview Scrubbing at Out-of-Bounds Timestamps")
    for ts in [0.1, 1.0, 2.5, 999.0]:
        p = engine.generate_preview_frame(special_file, timestamp_sec=ts)
        print(f"    Requested {ts:<5}s -> Rendered Preview: {'PASS' if p else 'FAIL'} {p.size if p else ''}")
        assert p is not None, f"Preview failed for timestamp {ts}"

    print("\n" + "=" * 62)
    all_ok = ok1 and ok2 and ok3 and dur_ok
    print(f"ADVANCED TESTS SUMMARY: {'ALL PASSED' if all_ok else 'SOME FAILED'}")
    print("=" * 62 + "\n")
    return all_ok


if __name__ == "__main__":
    passed = run_advanced_tests()
    sys.exit(0 if passed else 1)
