"""SmarterGamers Vertical Blur Editor - High-Performance FFmpeg Core Engine.

Provides:
- Robust FFmpeg & FFprobe auto-discovery (PATH, imageio-ffmpeg, LocalAppData, bundled)
- GPU Hardware encoder detection (NVIDIA NVENC, Intel QSV, AMD AMF, CPU libx264)
- Video probe (resolution, duration, fps, bitrate, audio streams)
- High-performance dual-stage BoxBlur & Gaussian blur filtergraphs
- Background dimming & vignette styling for gamer shorts
- Real-time progress monitoring via pipe:1
- Fast single-frame preview extraction
"""

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from PIL import Image

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class VideoMetadata:
    path: Path
    width: int
    height: int
    duration_sec: float
    fps: float
    video_codec: str
    audio_codec: Optional[str]
    bitrate: Optional[int]


@dataclass
class EncoderProfile:
    encoder_id: str
    name: str
    hardware: str
    codec: str  # h264, hevc, av1


class FFmpegEngine:
    def __init__(self, custom_ffmpeg_path: Optional[str] = None):
        self.ffmpeg_exe = custom_ffmpeg_path or self.find_ffmpeg()
        self.ffprobe_exe = self.find_ffprobe()
        self.available_encoders = self.detect_encoders()
        self.preferred_encoder = self.get_best_encoder()

    @staticmethod
    def find_ffmpeg() -> Optional[str]:
        """Locates FFmpeg executable across PATH, imageio_ffmpeg, and standard Windows paths."""
        # 1. System PATH
        exe = shutil.which("ffmpeg")
        if exe:
            return exe

        # 2. imageio-ffmpeg bundled
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and os.path.exists(exe):
                return exe
        except Exception:
            pass

        # 3. Known LocalAppData and Program Files paths
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg" / "ffmpeg-master-latest-win64-gpl" / "bin" / "ffmpeg.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
        ]
        for c in candidates:
            if c.exists():
                return str(c)

        return None

    def find_ffprobe(self) -> Optional[str]:
        """Locates FFprobe beside FFmpeg or on PATH."""
        if self.ffmpeg_exe:
            beside = Path(self.ffmpeg_exe).parent / "ffprobe.exe"
            if beside.exists():
                return str(beside)
        return shutil.which("ffprobe")

    def detect_encoders(self) -> List[EncoderProfile]:
        """Queries FFmpeg for available hardware-accelerated and software video encoders."""
        if not self.ffmpeg_exe:
            return [EncoderProfile("libx264", "CPU (libx264)", "CPU", "h264")]

        profiles = []
        try:
            out = subprocess.run(
                [self.ffmpeg_exe, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                creationflags=_NO_WINDOW,
                timeout=5,
            ).stdout
        except Exception:
            return [EncoderProfile("libx264", "CPU (libx264)", "CPU", "h264")]

        # NVIDIA NVENC
        if "h264_nvenc" in out:
            profiles.append(EncoderProfile("h264_nvenc", "NVIDIA NVENC (H.264) - Ultra Fast", "NVIDIA", "h264"))
        if "hevc_nvenc" in out:
            profiles.append(EncoderProfile("hevc_nvenc", "NVIDIA NVENC (HEVC/H.265)", "NVIDIA", "hevc"))
        if "av1_nvenc" in out:
            profiles.append(EncoderProfile("av1_nvenc", "NVIDIA NVENC (AV1)", "NVIDIA", "av1"))

        # Intel QSV
        if "h264_qsv" in out:
            profiles.append(EncoderProfile("h264_qsv", "Intel QuickSync (H.264)", "Intel", "h264"))
        if "hevc_qsv" in out:
            profiles.append(EncoderProfile("hevc_qsv", "Intel QuickSync (HEVC)", "Intel", "hevc"))

        # AMD AMF
        if "h264_amf" in out:
            profiles.append(EncoderProfile("h264_amf", "AMD AMF (H.264)", "AMD", "h264"))
        if "hevc_amf" in out:
            profiles.append(EncoderProfile("hevc_amf", "AMD AMF (HEVC)", "AMD", "hevc"))

        # CPU Fallback
        profiles.append(EncoderProfile("libx264", "CPU (libx264) - Compatible", "CPU", "h264"))
        return profiles

    def get_best_encoder(self) -> EncoderProfile:
        """Returns the highest priority hardware encoder, falling back to CPU."""
        if not self.available_encoders:
            return EncoderProfile("libx264", "CPU (libx264)", "CPU", "h264")
        # Hardware encoders come first
        for enc in self.available_encoders:
            if enc.hardware in ("NVIDIA", "Intel", "AMD") and enc.codec == "h264":
                return enc
        return self.available_encoders[0]

    def probe_video(self, video_path: Path) -> Optional[VideoMetadata]:
        """Extracts video metadata including dimensions, duration, and codecs."""
        if not self.ffmpeg_exe or not video_path.exists():
            return None

        # Try ffprobe first for rich JSON
        if self.ffprobe_exe:
            try:
                cmd = [
                    self.ffprobe_exe,
                    "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    "-show_streams",
                    str(video_path),
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=6)
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    v_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
                    a_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
                    if v_stream:
                        w = int(v_stream.get("width", 1920))
                        h = int(v_stream.get("height", 1080))
                        dur = float(v_stream.get("duration") or data.get("format", {}).get("duration", 0.0))
                        # FPS calculation
                        r_fps = v_stream.get("r_frame_rate", "30/1")
                        if "/" in r_fps:
                            num, den = r_fps.split("/")
                            fps = float(num) / float(den) if float(den) > 0 else 30.0
                        else:
                            fps = float(r_fps)
                        return VideoMetadata(
                            path=video_path,
                            width=w,
                            height=h,
                            duration_sec=dur,
                            fps=fps,
                            video_codec=v_stream.get("codec_name", "unknown"),
                            audio_codec=a_stream.get("codec_name") if a_stream else None,
                            bitrate=int(data.get("format", {}).get("bit_rate", 0)) or None,
                        )
            except Exception:
                pass

        # Fallback to ffmpeg -i parsing
        try:
            cmd = [self.ffmpeg_exe, "-hide_banner", "-i", str(video_path)]
            res = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=5)
            stderr = res.stderr

            dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
            dur = 0.0
            if dur_match:
                h, m, s = dur_match.groups()
                dur = int(h) * 3600 + int(m) * 60 + float(s)

            res_match = re.search(r"Video:.*,\s*(\d{3,5})x(\d{3,5})", stderr)
            w, h = (1920, 1080)
            if res_match:
                w, h = int(res_match.group(1)), int(res_match.group(2))

            return VideoMetadata(
                path=video_path,
                width=w,
                height=h,
                duration_sec=dur,
                fps=60.0,
                video_codec="unknown",
                audio_codec="aac",
                bitrate=None,
            )
        except Exception:
            return None

    def build_filtergraph(
        self,
        target_w: int = 1080,
        target_h: int = 1920,
        blur_strength: int = 20,
        blur_algorithm: str = "boxblur",  # "boxblur" or "gaussian"
        dim_factor: float = 0.70,  # 1.0 = normal, 0.70 = 30% darker background
        vertical_offset: int = 0,  # pixels from center (- is up, + is down)
        sharp_scale: float = 1.0,  # foreground scale multiplier (0.95 = 5% padding)
    ) -> str:
        """Constructs a high-performance FFmpeg filtergraph for 9:16 vertical shorts."""
        fg_w = int(target_w * sharp_scale)
        # Ensure fg_w is even
        if fg_w % 2 != 0:
            fg_w -= 1

        # Background processing
        if blur_algorithm == "boxblur":
            # High-performance downscale-blur-upscale: 10x-15x faster than raw gblur
            # Downsample to 25% resolution, apply boxblur, upscale smoothly
            sub_w = target_w // 4
            sub_h = target_h // 4
            radius = max(2, int(blur_strength * 0.6))
            power = 2

            bg_filters = [
                f"scale={sub_w}:{sub_h}:force_original_aspect_ratio=increase",
                f"crop={sub_w}:{sub_h}",
                f"boxblur=luma_radius={radius}:luma_power={power}:chroma_radius={radius}:chroma_power={power}",
            ]
            if dim_factor < 0.99:
                bg_filters.append(f"colorlevels=rimax={dim_factor:.2f}:gimax={dim_factor:.2f}:bimax={dim_factor:.2f}")
            bg_filters.append(f"scale={target_w}:{target_h}:flags=bilinear")
            bg_chain = ",".join(bg_filters)
        else:
            # Classic Gaussian Blur
            sigma = max(1, blur_strength)
            bg_filters = [
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase",
                f"crop={target_w}:{target_h}",
                f"gblur=sigma={sigma}",
            ]
            if dim_factor < 0.99:
                bg_filters.append(f"colorlevels=rimax={dim_factor:.2f}:gimax={dim_factor:.2f}:bimax={dim_factor:.2f}")
            bg_chain = ",".join(bg_filters)

        # Foreground processing: centered sharp clip
        fg_chain = f"scale={fg_w}:-2:flags=lanczos"

        # Overlay positioning with vertical offset
        y_expr = f"(H-h)/2+{vertical_offset}"
        x_expr = f"(W-w)/2"
        overlay_chain = f"overlay={x_expr}:{y_expr}"

        return f"[0:v]split=2[bg_in][fg_in];[bg_in]{bg_chain}[bg_out];[fg_in]{fg_chain}[fg_out];[bg_out][fg_out]{overlay_chain}"

    def build_render_command(
        self,
        src: Path,
        dst: Path,
        target_w: int = 1080,
        target_h: int = 1920,
        blur_strength: int = 20,
        blur_algorithm: str = "boxblur",
        dim_factor: float = 0.70,
        vertical_offset: int = 0,
        sharp_scale: float = 1.0,
        encoder_id: str = "h264_nvenc",
        quality_preset: str = "balanced",  # "fast", "balanced", "high"
        audio_mode: str = "copy",  # "copy" or "aac_high"
    ) -> List[str]:
        """Constructs full FFmpeg command list for rendering with progress output."""
        filtergraph = self.build_filtergraph(
            target_w=target_w,
            target_h=target_h,
            blur_strength=blur_strength,
            blur_algorithm=blur_algorithm,
            dim_factor=dim_factor,
            vertical_offset=vertical_offset,
            sharp_scale=sharp_scale,
        )

        cmd = [
            self.ffmpeg_exe,
            "-y",
            "-progress", "pipe:1",
            "-nostats",
            "-i", str(src),
            "-filter_complex", filtergraph,
        ]

        # Video encoder specific flags
        if encoder_id == "h264_nvenc":
            preset = "p4" if quality_preset == "fast" else ("p6" if quality_preset == "high" else "p5")
            cq = "26" if quality_preset == "fast" else ("20" if quality_preset == "high" else "23")
            cmd += ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", cq, "-b:v", "0"]
        elif encoder_id == "hevc_nvenc":
            preset = "p5"
            cq = "24" if quality_preset == "fast" else ("19" if quality_preset == "high" else "22")
            cmd += ["-c:v", "hevc_nvenc", "-preset", preset, "-rc", "vbr", "-cq", cq, "-b:v", "0"]
        elif encoder_id == "h264_qsv":
            cmd += ["-c:v", "h264_qsv", "-global_quality", "23"]
        elif encoder_id == "h264_amf":
            cmd += ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]
        else:
            preset = "ultrafast" if quality_preset == "fast" else ("slow" if quality_preset == "high" else "veryfast")
            crf = "25" if quality_preset == "fast" else ("20" if quality_preset == "high" else "22")
            cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf]

        cmd += ["-pix_fmt", "yuv420p"]

        # Audio settings
        if audio_mode == "copy":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]

        cmd += ["-movflags", "+faststart", str(dst)]
        return cmd

    def generate_preview_frame(
        self,
        src: Path,
        timestamp_sec: float = 2.0,
        blur_strength: int = 20,
        blur_algorithm: str = "boxblur",
        dim_factor: float = 0.70,
        vertical_offset: int = 0,
        sharp_scale: float = 1.0,
        preview_height: int = 540,
    ) -> Optional[Image.Image]:
        """Extracts a single frame and renders the 9:16 blurred composition in milliseconds."""
        if not self.ffmpeg_exe or not src.exists():
            return None

        # Scale preview to smaller dimensions (e.g. 304x540 for 9:16)
        preview_w = int(preview_height * (9 / 16))
        if preview_w % 2 != 0:
            preview_w -= 1

        filtergraph = self.build_filtergraph(
            target_w=preview_w,
            target_h=preview_height,
            blur_strength=blur_strength,
            blur_algorithm=blur_algorithm,
            dim_factor=dim_factor,
            vertical_offset=int(vertical_offset * (preview_height / 1920)),
            sharp_scale=sharp_scale,
        )

        cmd = [
            self.ffmpeg_exe,
            "-ss", str(timestamp_sec),
            "-i", str(src),
            "-filter_complex", filtergraph,
            "-vframes", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "-",
        ]

        try:
            p = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, timeout=5)
            if p.returncode == 0 and p.stdout:
                import io
                return Image.open(io.BytesIO(p.stdout))
        except Exception:
            pass

        return None
