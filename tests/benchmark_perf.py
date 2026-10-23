"""Performance & Encoder Benchmark Suite for SmarterGamers Vertical Blur Editor."""

import subprocess
import sys
import time
from pathlib import Path

# Add src to path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from core.ffmpeg_engine import FFmpegEngine
from core.batch_processor import BatchProcessor, JobConfig

SRC_VIDEO = Path(__file__).resolve().parent / "fixtures" / "std_1080p.mp4"
BENCH_OUT = Path(__file__).resolve().parent / "output" / "bench"
BENCH_OUT.mkdir(parents=True, exist_ok=True)
SRC_VIDEO.parent.mkdir(parents=True, exist_ok=True)


def ensure_fixture(ffmpeg_exe: str):
    if not SRC_VIDEO.exists():
        print(f"[*] Fixture video missing. Generating {SRC_VIDEO.name}...")
        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cmd = [
            ffmpeg_exe, "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
            str(SRC_VIDEO)
        ]
        subprocess.run(cmd, check=True, capture_output=True, creationflags=_NO_WINDOW)


def benchmark():
    engine = FFmpegEngine()
    if not engine.ffmpeg_exe:
        print("[ERROR] FFmpeg not found.")
        return
    ensure_fixture(engine.ffmpeg_exe)
    batch = BatchProcessor(engine)

    benchmarks = [
        ("BoxBlur (Fast) + NVENC", "boxblur", "h264_nvenc", 20),
        ("BoxBlur (Heavy Blur=40) + NVENC", "boxblur", "h264_nvenc", 40),
        ("Gaussian (Classic) + NVENC", "gaussian", "h264_nvenc", 20),
        ("BoxBlur (Fast) + CPU", "boxblur", "libx264", 20),
        ("Gaussian (Classic) + CPU", "gaussian", "libx264", 20),
    ]

    print("\n" + "=" * 68)
    print(" SMARTERGAMERS VERTICAL BLUR — PERFORMANCE BENCHMARK")
    print("=" * 68)
    print(f"{'Configuration':<34} | {'Time (s)':<10} | {'FPS':<10} | {'Speedup'}")
    print("-" * 68)

    baseline_time = None

    for name, algo, enc, blur in benchmarks:
        dst = BENCH_OUT / f"bench_{algo}_{enc}_{blur}.mp4"
        if dst.exists():
            dst.unlink()

        cfg = JobConfig(
            target_w=1080,
            target_h=1920,
            blur_strength=blur,
            blur_algorithm=algo,
            dim_factor=0.70,
            encoder_id=enc,
            output_dir=BENCH_OUT,
        )

        jobs = batch.add_files([SRC_VIDEO], cfg)
        if not jobs:
            continue
        job = jobs[0]
        job.dst_path = dst

        t0 = time.perf_counter()
        ok = batch._process_job(job)
        elapsed = time.perf_counter() - t0

        fps = (120 / elapsed) if elapsed > 0 else 0  # 2s @ 60fps = 120 frames

        if baseline_time is None and "Gaussian (Classic) + CPU" in name:
            baseline_time = elapsed

        speedup_str = ""
        if baseline_time:
            speedup = baseline_time / elapsed
            speedup_str = f"{speedup:.1f}x vs CPU"

        print(f"{name:<34} | {elapsed:<10.2f} | {fps:<10.1f} | {speedup_str}")

    print("=" * 68 + "\n")


if __name__ == "__main__":
    benchmark()
