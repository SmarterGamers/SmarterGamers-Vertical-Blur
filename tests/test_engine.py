"""Unit and Integration Tests for SmarterGamers Vertical Blur Editor."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.ffmpeg_engine import FFmpegEngine
from core.batch_processor import BatchProcessor, JobConfig, RenderJob


def test_engine_discovery():
    engine = FFmpegEngine()
    assert engine.ffmpeg_exe is not None, "FFmpeg should be discovered"
    assert len(engine.available_encoders) > 0, "At least one encoder should be detected"
    assert engine.preferred_encoder is not None, "Preferred encoder should be identified"
    print(f"[OK] Engine discovery passed. Found: {engine.preferred_encoder.name}")


def test_filtergraph_boxblur():
    engine = FFmpegEngine()
    fg = engine.build_filtergraph(
        target_w=1080,
        target_h=1920,
        blur_strength=25,
        blur_algorithm="boxblur",
        dim_factor=0.65,
        vertical_offset=-100,
        sharp_scale=1.0,
    )
    assert "boxblur" in fg
    assert "colorlevels" in fg
    assert "overlay=(W-w)/2:(H-h)/2+-100" in fg
    print("[OK] Boxblur filtergraph generation passed")


def test_filtergraph_gaussian():
    engine = FFmpegEngine()
    fg = engine.build_filtergraph(
        target_w=1080,
        target_h=1920,
        blur_strength=15,
        blur_algorithm="gaussian",
        dim_factor=1.0,
        vertical_offset=0,
    )
    assert "gblur=sigma=15" in fg
    assert "overlay=(W-w)/2:(H-h)/2+0" in fg
    print("[OK] Gaussian filtergraph generation passed")


def test_build_render_command():
    engine = FFmpegEngine()
    cmd = engine.build_render_command(
        src=Path("test_input.mp4"),
        dst=Path("test_output.mp4"),
        encoder_id="h264_nvenc",
        quality_preset="high",
        audio_mode="copy",
    )
    assert "-c:v" in cmd
    assert "h264_nvenc" in cmd
    assert "-c:a" in cmd
    assert "copy" in cmd
    assert "-progress" in cmd
    print("[OK] Render command assembly passed")


def test_batch_processor_queue():
    engine = FFmpegEngine()
    batch = BatchProcessor(engine)
    cfg = JobConfig()
    
    # Create temporary dummy file for queue testing
    tmp = Path("tests/dummy.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"0" * 1024)
    try:
        jobs = batch.add_files([tmp], cfg)
        assert len(jobs) == 1
        assert jobs[0].status == "pending"
        assert jobs[0].dst_path.name == "dummy_vertical.mp4"
        batch.clear_completed()
        assert len(batch.queue) == 1
        batch.remove_job(jobs[0].id)
        assert len(batch.queue) == 0
        print("[OK] Batch processor queue management passed")
    finally:
        if tmp.exists():
            tmp.unlink()


if __name__ == "__main__":
    test_engine_discovery()
    test_filtergraph_boxblur()
    test_filtergraph_gaussian()
    test_build_render_command()
    test_batch_processor_queue()
    print("\nALL TESTS PASSED SUCCESSFULLY!")
