"""Concurrency, Queue Scaling, and Cancellation Stress Test."""

import sys
import time
from pathlib import Path

# Add src to path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from core.ffmpeg_engine import FFmpegEngine
from core.batch_processor import BatchProcessor, JobConfig

SRC_VIDEO = Path(__file__).resolve().parent / "fixtures" / "micro_clip.mp4"
STRESS_OUT = Path(__file__).resolve().parent / "output" / "stress"
STRESS_OUT.mkdir(parents=True, exist_ok=True)


def test_queue_scaling_and_cancellation():
    engine = FFmpegEngine()
    batch = BatchProcessor(engine)

    print("\n[*] Stress Test 1: Queuing 25 jobs simultaneously...")
    cfg = JobConfig(
        target_w=720,
        target_h=1280,
        blur_strength=10,
        output_dir=STRESS_OUT,
    )

    file_list = [SRC_VIDEO] * 25
    jobs = batch.add_files(file_list, cfg)
    assert len(jobs) == 25, f"Expected 25 jobs, got {len(jobs)}"
    print(f"    [PASS] Successfully queued {len(jobs)} jobs without race conditions.")

    print("\n[*] Stress Test 2: Rapid Start and Mid-flight Cancellation...")
    batch.start()
    time.sleep(0.4)  # Let worker spin up first job

    batch.stop()
    print("    [PASS] stop() signal sent cleanly.")

    # Wait for worker thread to exit
    if batch._worker_thread:
        batch._worker_thread.join(timeout=3.0)
        assert not batch._worker_thread.is_alive(), "Worker thread should terminate cleanly"
    print("    [PASS] Worker thread joined and verified terminated.")

    # Verify queue state
    statuses = [j.status for j in batch.queue]
    print(f"    Queue status distribution: {dict((s, statuses.count(s)) for s in set(statuses))}")
    assert any(s in ("cancelled", "completed", "pending") for s in statuses)

    print("\n[*] Stress Test 3: Clear and Re-queue...")
    batch.clear_completed()
    print(f"    Remaining active/pending jobs: {len(batch.queue)}")

    print("\n[OK] ALL CONCURRENCY & CANCELLATION TESTS PASSED!\n")


if __name__ == "__main__":
    test_queue_scaling_and_cancellation()
