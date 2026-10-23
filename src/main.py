"""SmarterGamers Vertical Blur Editor - Main Entrypoint v1.2.0.

Supports interactive GUI mode (Modern Studio or Lightweight Classic) and
headless CLI batch automation with sub-clip trimming and multi-track audio mixing.
"""

import argparse
import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.ffmpeg_engine import FFmpegEngine
from core.batch_processor import BatchProcessor, JobConfig

PRESETS = {
    "shorts": (1080, 1920),
    "4k": (2160, 3840),
    "720p": (720, 1280),
    "square": (1080, 1080),
    "portrait": (1080, 1350),
}


def run_cli(args):
    print("=" * 64)
    print(" SmarterGamers — Vertical Blur Studio v1.2.0 (CLI)")
    print("=" * 64)

    engine = FFmpegEngine()
    if not engine.ffmpeg_exe:
        print("[ERROR] FFmpeg was not found. Please install FFmpeg.")
        sys.exit(1)

    print(f"[*] Engine: {engine.ffmpeg_exe}")
    print(f"[*] Best Encoder: {engine.preferred_encoder.name}")

    inputs = [Path(p) for p in args.inputs]
    out_dir = Path(args.output_dir) if args.output_dir else None

    target_w = args.width
    target_h = args.height
    if args.preset and args.preset in PRESETS:
        target_w, target_h = PRESETS[args.preset]

    config = JobConfig(
        target_w=target_w,
        target_h=target_h,
        blur_strength=args.blur,
        blur_algorithm=args.algorithm,
        dim_factor=args.dim,
        vertical_offset=args.offset,
        encoder_id=args.encoder or engine.preferred_encoder.encoder_id,
        audio_mode=args.audio_mode,
        audio_gain_db=args.volume,
        start_sec=args.start,
        duration_sec=args.duration,
        enable_hwaccel=args.hwaccel,
        output_dir=out_dir,
    )

    batch = BatchProcessor(engine)
    jobs = batch.add_files(inputs, config)
    print(f"[*] Queued {len(jobs)} video(s) for rendering [{target_w}x{target_h}].")
    if args.start is not None or args.duration is not None:
        print(f"[*] Trimming applied: start={args.start or 0.0}s, duration={args.duration or 'Full'}s")
    if args.audio_mode != "copy":
        print(f"[*] Audio Mode: {args.audio_mode} (gain: {args.volume:+.1f}dB)")
    print()

    def on_progress(job):
        sys.stdout.write(f"\r -> {job.src_path.name}: {job.progress:.1f}% ({job.speed_factor})")
        sys.stdout.flush()

    def on_finished(job):
        if job.status == "completed":
            print(f"\n[OK] Rendered -> {job.dst_path}")
        else:
            print(f"\n[FAIL] {job.src_path.name}: {job.error_msg}")

    batch.on_job_progress = on_progress
    batch.on_job_finished = on_finished

    batch.start()
    if batch._worker_thread:
        batch._worker_thread.join()

    print("\n[DONE] All batch jobs finished.")


def run_gui(force_classic: bool = False):
    if not force_classic:
        try:
            from ui.app import App
            app = App()
            app.mainloop()
            return
        except Exception as exc:
            print(f"[WARN] Failed to launch Modern Studio GUI ({exc}). Falling back to Classic GUI...")

    import tkinter as tk
    from ui.classic_app import ClassicApp
    root = tk.Tk()
    app = ClassicApp(root)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="SmarterGamers Vertical Blur Editor v1.2.0")
    parser.add_argument("inputs", nargs="*", help="Video file paths to process (CLI mode)")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), default="shorts", help="Format preset")
    parser.add_argument("--blur", type=int, default=20, help="Blur radius (5-50)")
    parser.add_argument("--algorithm", choices=["boxblur", "gaussian"], default="boxblur", help="Blur filter algorithm")
    parser.add_argument("--dim", type=float, default=0.70, help="Background contrast factor (0.4 - 1.0)")
    parser.add_argument("--offset", type=int, default=0, help="Vertical offset from center in pixels")
    parser.add_argument("--width", type=int, default=1080, help="Target width")
    parser.add_argument("--height", type=int, default=1920, help="Target height")
    parser.add_argument("--encoder", type=str, default=None, help="Encoder id (e.g. h264_nvenc, libx264)")
    parser.add_argument("--start", type=float, default=None, help="Trim start time in seconds")
    parser.add_argument("--duration", type=float, default=None, help="Trim clip duration in seconds")
    parser.add_argument("--audio-mode", choices=["copy", "mix_all", "aac_high", "mute"], default="copy", help="Audio processing mode")
    parser.add_argument("--volume", type=float, default=0.0, help="Audio volume gain in dB (e.g. 3.0 or -3.0)")
    parser.add_argument("--hwaccel", action="store_true", help="Enable GPU hardware decoding")
    parser.add_argument("-o", "--output-dir", type=str, default=None, help="Output destination folder")
    parser.add_argument("--cli", action="store_true", help="Force headless CLI mode")
    parser.add_argument("--classic", action="store_true", help="Launch lightweight Classic single-file GUI")

    args = parser.parse_args()

    if args.cli or len(args.inputs) > 0:
        run_cli(args)
    else:
        run_gui(force_classic=args.classic)


if __name__ == "__main__":
    main()
