"""v1.2.0 Optimization & Decoding Throughput Benchmarks."""

import sys
import time
from pathlib import Path

# Add src to path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from core.ffmpeg_engine import FFmpegEngine
from core.batch_processor import BatchProcessor, JobConfig

SRC_VIDEO = Path(__file__).resolve().parent / "fixtures" / "std_1080p.mp4"
BENCH_OUT = Path(__file__).resolve().parent / "output" / "bench_v12"
BENCH_OUT.mkdir(parents=True, exist_ok=True)


def run_benchmark():
    engine = FFmpegEngine()
    batch = BatchProcessor(engine)

    print("\n" + "=" * 65)
    print(" SMARTERGAMERS v1.2.0 DECODE & ENCODE ACCELERATION BENCHMARK")
    print("=" * 65)

    # 1. With HW Decode Acceleration
    cfg_hw = JobConfig(
        output_dir=BENCH_OUT,
        encoder_id="h264_nvenc",
        enable_hwaccel=True,
    )
    j_hw = batch.add_files([SRC_VIDEO], cfg_hw)[0]
    j_hw.dst_path = BENCH_OUT / "bench_hwaccel.mp4"

    t0 = time.perf_counter()
    ok_hw = batch._process_job(j_hw)
    dur_hw = time.perf_counter() - t0

    # 2. Software Decode Baseline
    cfg_sw = JobConfig(
        output_dir=BENCH_OUT,
        encoder_id="h264_nvenc",
        enable_hwaccel=False,
    )
    j_sw = batch.add_files([SRC_VIDEO], cfg_sw)[0]
    j_sw.dst_path = BENCH_OUT / "bench_sw.mp4"

    t1 = time.perf_counter()
    ok_sw = batch._process_job(j_sw)
    dur_sw = time.perf_counter() - t1

    fps_hw = 120 / dur_hw if dur_hw > 0 else 0
    fps_sw = 120 / dur_sw if dur_sw > 0 else 0

    print(f"  Hardware Decode + NVENC : {dur_hw:.2f}s ({fps_hw:.1f} FPS)")
    print(f"  Software Decode + NVENC : {dur_sw:.2f}s ({fps_sw:.1f} FPS)")
    print(f"  Acceleration Benefit     : {((dur_sw - dur_hw) / dur_sw * 100):.1f}% faster")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    run_benchmark()
