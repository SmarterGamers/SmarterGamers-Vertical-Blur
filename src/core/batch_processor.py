"""SmarterGamers Vertical Blur Editor - Batch Rendering Queue & Worker Engine.

Handles multi-file batch queues, real-time progress parsing, cancellation,
and resilient automatic fallback from GPU to CPU on encoder exceptions.
"""

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional
from core.ffmpeg_engine import FFmpegEngine, VideoMetadata

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class JobConfig:
    target_w: int = 1080
    target_h: int = 1920
    blur_strength: int = 20
    blur_algorithm: str = "boxblur"  # "boxblur" or "gaussian"
    dim_factor: float = 0.70
    vertical_offset: int = 0
    sharp_scale: float = 1.0
    encoder_id: str = "h264_nvenc"
    quality_preset: str = "balanced"
    audio_mode: str = "copy"
    output_dir: Optional[Path] = None


@dataclass
class RenderJob:
    id: str
    src_path: Path
    dst_path: Path
    config: JobConfig
    metadata: Optional[VideoMetadata] = None
    status: str = "pending"  # pending, processing, completed, error, cancelled
    progress: float = 0.0  # 0.0 to 100.0
    current_fps: float = 0.0
    speed_factor: str = "0.0x"
    eta_sec: float = 0.0
    error_msg: str = ""
    fallback_used: bool = False


class BatchProcessor:
    def __init__(self, engine: FFmpegEngine):
        self.engine = engine
        self.queue: List[RenderJob] = []
        self._lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._current_process: Optional[subprocess.Popen] = None
        self._is_running = False
        self._stop_requested = False

        # Callbacks
        self.on_queue_updated: Optional[Callable[[], None]] = None
        self.on_job_progress: Optional[Callable[[RenderJob], None]] = None
        self.on_job_finished: Optional[Callable[[RenderJob], None]] = None
        self.on_all_finished: Optional[Callable[[int, int], None]] = None  # (success_count, error_count)

    def add_files(self, paths: List[Path], config: JobConfig) -> List[RenderJob]:
        """Adds video files to the queue, probing metadata for each."""
        new_jobs = []
        with self._lock:
            for p in paths:
                if not p.is_file():
                    continue
                # Determine output path
                out_dir = config.output_dir if config.output_dir else p.parent
                dst = out_dir / f"{p.stem}_vertical.mp4"

                # Deduplicate if dst already queued or exists
                counter = 1
                while dst.exists() and dst == p:
                    dst = out_dir / f"{p.stem}_vertical_{counter}.mp4"
                    counter += 1

                job_id = f"{p.name}_{time.time()}_{len(self.queue)}"
                meta = self.engine.probe_video(p)
                job = RenderJob(
                    id=job_id,
                    src_path=p,
                    dst_path=dst,
                    config=config,
                    metadata=meta,
                )
                self.queue.append(job)
                new_jobs.append(job)

        if self.on_queue_updated:
            self.on_queue_updated()
        return new_jobs

    def remove_job(self, job_id: str):
        with self._lock:
            self.queue = [j for j in self.queue if j.id != job_id]
        if self.on_queue_updated:
            self.on_queue_updated()

    def clear_completed(self):
        with self._lock:
            self.queue = [j for j in self.queue if j.status not in ("completed", "cancelled")]
        if self.on_queue_updated:
            self.on_queue_updated()

    def start(self):
        """Starts batch processing in a background worker thread."""
        if self._is_running:
            return
        self._is_running = True
        self._stop_requested = False
        self._worker_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._worker_thread.start()

    def stop(self):
        """Cancels current running job and stops remaining queue."""
        self._stop_requested = True
        self._is_running = False
        with self._lock:
            if self._current_process:
                try:
                    self._current_process.kill()
                except Exception:
                    pass

    def _run_loop(self):
        success_count = 0
        error_count = 0

        while self._is_running and not self._stop_requested:
            # Find next pending job
            job: Optional[RenderJob] = None
            with self._lock:
                for j in self.queue:
                    if j.status == "pending":
                        job = j
                        job.status = "processing"
                        break

            if not job:
                break  # Queue finished

            if self.on_queue_updated:
                self.on_queue_updated()

            # Execute job
            ok = self._process_job(job)
            if ok:
                success_count += 1
            else:
                error_count += 1

            if self.on_job_finished:
                self.on_job_finished(job)
            if self.on_queue_updated:
                self.on_queue_updated()

        self._is_running = False
        if self.on_all_finished and not self._stop_requested:
            self.on_all_finished(success_count, error_count)

    def _process_job(self, job: RenderJob) -> bool:
        """Renders one job using FFmpeg, streaming progress lines."""
        duration = job.metadata.duration_sec if job.metadata and job.metadata.duration_sec > 0 else 60.0

        # Attempt primary encoder
        success = self._execute_ffmpeg(job, job.config.encoder_id, duration)
        if success:
            job.status = "completed"
            job.progress = 100.0
            return True

        if self._stop_requested:
            job.status = "cancelled"
            return False

        # If primary GPU encoder failed, auto-fallback to CPU libx264
        if job.config.encoder_id != "libx264":
            job.fallback_used = True
            job.progress = 0.0
            job.error_msg = "GPU encoder failed, falling back to CPU..."
            if self.on_job_progress:
                self.on_job_progress(job)

            fallback_success = self._execute_ffmpeg(job, "libx264", duration)
            if fallback_success:
                job.status = "completed"
                job.progress = 100.0
                return True

        job.status = "error"
        return False

    def _execute_ffmpeg(self, job: RenderJob, encoder_id: str, duration: float) -> bool:
        cmd = self.engine.build_render_command(
            src=job.src_path,
            dst=job.dst_path,
            target_w=job.config.target_w,
            target_h=job.config.target_h,
            blur_strength=job.config.blur_strength,
            blur_algorithm=job.config.blur_algorithm,
            dim_factor=job.config.dim_factor,
            vertical_offset=job.config.vertical_offset,
            sharp_scale=job.config.sharp_scale,
            encoder_id=encoder_id,
            quality_preset=job.config.quality_preset,
            audio_mode=job.config.audio_mode,
        )

        try:
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=_NO_WINDOW,
            )
            self._current_process = p

            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    if key == "out_time_us":
                        try:
                            cur_sec = int(val) / 1_000_000.0
                            if duration > 0:
                                pct = min(99.0, max(0.0, (cur_sec / duration) * 100.0))
                                job.progress = round(pct, 1)
                        except Exception:
                            pass
                    elif key == "fps":
                        try:
                            job.current_fps = float(val)
                        except Exception:
                            pass
                    elif key == "speed":
                        job.speed_factor = val.strip()

                if self.on_job_progress:
                    self.on_job_progress(job)

            p.wait()
            self._current_process = None

            if p.returncode == 0 and job.dst_path.exists() and job.dst_path.stat().st_size > 1000:
                return True
            else:
                err = p.stderr.read() if p.stderr else ""
                job.error_msg = err[-400:] if err else f"FFmpeg exited with code {p.returncode}"
                return False
        except Exception as exc:
            job.error_msg = str(exc)
            return False
