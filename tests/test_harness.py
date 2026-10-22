"""Automated Test & Benchmark Harness for SmarterGamers Vertical Blur Editor.
Generates synthetic video edge-cases via FFmpeg and benchmarks rendering pipelines.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Add src to path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from core.ffmpeg_engine import FFmpegEngine
from core.batch_processor import BatchProcessor, JobConfig

TEST_DIR = Path(__file__).resolve().parent / "fixtures"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def create_synthetic_videos(ffmpeg_exe: str):
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cases = {
        # 1. Standard 1080p 60fps with audio
        "std_1080p.mp4": [
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p"
        ],
        # 2. Silent video without any audio stream (common OBS / Medal clip edge case)
        "silent_no_audio.mp4": [
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
            "-t", "2", "-c:v", "libx264", "-an", "-pix_fmt", "yuv420p"
        ],
        # 3. Ultrawide 21:9 video (2560x1080)
        "ultrawide_21x9.mp4": [
            "-f", "lavfi", "-i", "testsrc2=size=2560x1080:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p"
        ],
        # 4. Odd resolution (odd width/height that breaks naive scale/crop)
        "odd_dimensions.mp4": [
            "-f", "lavfi", "-i", "testsrc2=size=1365x767:rate=30",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p"
        ],
        # 5. 10-bit Color (HDR / yuv420p10le)
        "hdr_10bit.mp4": [
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p10le"
        ],
        # 6. Ultra-short video (0.4s)
        "micro_clip.mp4": [
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
            "-t", "0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p"
        ]
    }

    print("[*] Generating synthetic test clips...")
    for name, args in cases.items():
        out_path = TEST_DIR / name
        if not out_path.exists():
            cmd = [ffmpeg_exe, "-y"] + args + [str(out_path)]
            p = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
            if p.returncode == 0:
                print(f"  + Generated: {name}")
            else:
                print(f"  - Failed to generate: {name}")


def run_tests():
    engine = FFmpegEngine()
    if not engine.ffmpeg_exe:
        print("[FAIL] FFmpeg not found!")
        return False

    create_synthetic_videos(engine.ffmpeg_exe)

    batch = BatchProcessor(engine)
    test_files = list(TEST_DIR.glob("*.mp4"))
    print(f"\n[*] Testing {len(test_files)} edge-case clips...")

    results = {}

    for f in test_files:
        print(f"\n>>> TESTING: {f.name}")
        cfg = JobConfig(
            target_w=1080,
            target_h=1920,
            blur_strength=20,
            blur_algorithm="boxblur",
            dim_factor=0.70,
            encoder_id=engine.preferred_encoder.encoder_id,
            output_dir=OUTPUT_DIR,
        )

        meta = engine.probe_video(f)
        has_audio = meta.audio_codec is not None if meta else False
        print(f"    Metadata: {meta.width}x{meta.height}, {meta.duration_sec:.1f}s, Audio: {meta.audio_codec}")

        dst = OUTPUT_DIR / f"{f.stem}_vertical.mp4"
        if dst.exists():
            dst.unlink()

        job_list = batch.add_files([f], cfg)
        job = job_list[0]

        start_t = time.perf_counter()
        success = batch._process_job(job)
        elapsed = time.perf_counter() - start_t

        if success and dst.exists() and dst.stat().st_size > 1000:
            print(f"    [PASS] Rendered in {elapsed:.2f}s ({dst.stat().st_size / 1024:.1f} KB)")
            results[f.name] = {"status": "PASS", "time": elapsed, "error": None}
        else:
            print(f"    [FAIL] Error: {job.error_msg}")
            results[f.name] = {"status": "FAIL", "time": elapsed, "error": job.error_msg}

    # Test preview frame generator
    print("\n[*] Testing Instant Preview Frame Generation...")
    p_img = engine.generate_preview_frame(TEST_DIR / "std_1080p.mp4", timestamp_sec=1.0)
    if p_img:
        print(f"    [PASS] Generated preview frame size: {p_img.size}")
        results["preview_generation"] = {"status": "PASS"}
    else:
        print("    [FAIL] Preview frame generation failed!")
        results["preview_generation"] = {"status": "FAIL"}

    print("\n" + "=" * 50)
    print("TEST SUMMARY:")
    all_passed = True
    for k, v in results.items():
        st = v["status"]
        if st != "PASS":
            all_passed = False
        print(f"  {k:25} : {st} {('('+str(v.get('error'))+')') if v.get('error') else ''}")
    print("=" * 50)
    return all_passed


if __name__ == "__main__":
    passed = run_tests()
    sys.exit(0 if passed else 1)
