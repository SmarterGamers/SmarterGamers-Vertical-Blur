"""SmarterGamers Vertical Blur Editor - Classic GUI Module.

Lightweight, instant-launch standalone GUI built with standard library tkinter.
Upgraded with the v1.2.0 dual-stage fast BoxBlur pipeline, studio contrast dimming,
silent video crash protection, and automatic multi-track audio mixing.
"""

import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BLUR_DEFAULT = 20


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def detect_encoder(ffmpeg: str) -> tuple[str, str]:
    """Detects available hardware-accelerated video encoder."""
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        ).stdout
    except Exception:
        return "libx264", "CPU (libx264)"
    if "h264_nvenc" in out:
        return "h264_nvenc", "NVIDIA GPU (NVENC)"
    if "h264_qsv" in out:
        return "h264_qsv", "Intel GPU (QSV)"
    if "h264_amf" in out:
        return "h264_amf", "AMD GPU (AMF)"
    return "libx264", "CPU (libx264)"


def probe_audio_streams(ffmpeg: str, src: Path) -> tuple[bool, int]:
    """Probes if video has audio and counts audio tracks."""
    try:
        res = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(src)],
            capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=5
        )
        audio_matches = re.findall(r"Stream #\d+:\d+.*Audio:", res.stderr)
        return (len(audio_matches) > 0), len(audio_matches)
    except Exception:
        return True, 1


def build_classic_cmd(ffmpeg: str, src: Path, dst: Path, blur_strength: int, encoder: str, use_boxblur: bool = True) -> list[str]:
    target_w = 1080
    target_h = 1920
    has_audio, audio_count = probe_audio_streams(ffmpeg, src)

    if use_boxblur:
        sub_w = 270
        sub_h = 480
        radius = max(2, int(blur_strength * 0.6))
        filtergraph = (
            "[0:v]split=2[raw_bg][raw_fg];"
            f"[raw_bg]scale={sub_w}:{sub_h}:force_original_aspect_ratio=increase,crop={sub_w}:{sub_h},"
            f"boxblur=luma_radius={radius}:luma_power=2:chroma_radius={radius}:chroma_power=2,"
            f"scale={target_w}:{target_h},"
            f"colorlevels=rimin=0:gimin=0:bimin=0:rimax=0.72:gimax=0.72:bimax=0.72[bgb];"
            f"[raw_fg]scale={target_w}:-2:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )
    else:
        filtergraph = (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h},gblur=sigma={blur_strength}[bgb];"
            f"[fg]scale={target_w}:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )

    audio_flags = []
    if not has_audio:
        audio_flags = ["-an"]
    elif audio_count > 1:
        filtergraph += ";[0:a:0][0:a:1]amix=inputs=2:duration=longest:dropout_transition=2[aout]"
        audio_flags = ["-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    else:
        audio_flags = ["-c:a", "aac", "-b:a", "128k"]

    cmd = [ffmpeg, "-y", "-i", str(src), "-filter_complex", filtergraph]

    if encoder == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    elif encoder == "h264_qsv":
        cmd += ["-c:v", "h264_qsv", "-global_quality", "23"]
    elif encoder == "h264_amf":
        cmd += ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

    cmd += ["-pix_fmt", "yuv420p"] + audio_flags + ["-movflags", "+faststart", str(dst)]
    return cmd


class ClassicApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.src = None
        self.ffmpeg = find_ffmpeg()
        self.encoder, self.enc_label = (
            detect_encoder(self.ffmpeg) if self.ffmpeg else ("libx264", "unknown")
        )

        root.title("SmarterGamers — 9:16 Blur Editor (Classic Enhanced)")
        root.geometry("580x370")
        root.configure(bg="#15161a")

        tk.Label(root, text="Landscape clip  →  9:16 Short",
                 font=("Segoe UI", 15, "bold"), fg="#ffffff", bg="#15161a").pack(pady=(16, 2))
        tk.Label(root, text="Ultra-fast BoxBlur + Studio Dimming (v1.2.0 Engine)",
                 font=("Segoe UI", 10), fg="#9aa0aa", bg="#15161a").pack()
        tk.Label(root, text=f"⚡ Encoder: {self.enc_label}",
                 font=("Segoe UI", 9), fg="#4ade80", bg="#15161a").pack(pady=(4, 0))

        self.btn = tk.Button(root, text="Select video…", font=("Segoe UI", 11),
                             command=self.pick, width=22)
        self.btn.pack(pady=(12, 4))
        self.lbl = tk.Label(root, text="No file selected", fg="#9aa0aa", bg="#15161a")
        self.lbl.pack()

        frame = tk.Frame(root, bg="#15161a")
        frame.pack(pady=6)
        tk.Label(frame, text="Blur strength:", fg="#ffffff", bg="#15161a").pack(side=tk.LEFT)
        self.blur = tk.IntVar(value=BLUR_DEFAULT)
        tk.Scale(frame, from_=5, to=40, orient=tk.HORIZONTAL, variable=self.blur,
                 length=220, bg="#15161a", fg="#ffffff", highlightthickness=0,
                 troughcolor="#2a2c33").pack(side=tk.LEFT, padx=6)

        self.fast_mode = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(root, text="Fast BoxBlur (15x speedup + Studio Dim)",
                            variable=self.fast_mode, bg="#15161a", fg="#60a5fa",
                            selectcolor="#1e2028", activebackground="#15161a", activeforeground="#60a5fa")
        cb.pack(pady=2)

        self.go = tk.Button(root, text="Convert to 9:16", font=("Segoe UI", 12, "bold"),
                            command=self.convert, state=tk.DISABLED, width=22,
                            bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8")
        self.go.pack(pady=8)
        self.status = tk.Label(root, text="", fg="#4ade80", bg="#15161a")
        self.status.pack()

    def pick(self):
        p = filedialog.askopenfilename(
            title="Select a video",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"), ("All files", "*.*")],
        )
        if p:
            self.src = Path(p)
            self.lbl.config(text=self.src.name, fg="#ffffff")
            self.go.config(state=tk.NORMAL)

    def convert(self):
        if not self.src:
            return
        if not self.ffmpeg:
            messagebox.showerror("FFmpeg missing", "FFmpeg wasn't found on your system.")
            return

        dst = self.src.with_name(self.src.stem + "_vertical.mp4")
        self.go.config(state=tk.DISABLED)
        self.btn.config(state=tk.DISABLED)
        algo_name = "Fast BoxBlur" if self.fast_mode.get() else "Gaussian"
        self.status.config(text=f"Converting with {algo_name} on {self.enc_label}…", fg="#60a5fa")
        threading.Thread(target=self._run, args=(dst,), daemon=True).start()

    def _run(self, dst: Path):
        try:
            cmd = build_classic_cmd(self.ffmpeg, self.src, dst, self.blur.get(), self.encoder, self.fast_mode.get())
            subprocess.run(cmd, check=True, capture_output=True, text=True, creationflags=_NO_WINDOW)
            self.root.after(0, self._done, dst)
        except subprocess.CalledProcessError as exc:
            if self.encoder != "libx264":
                try:
                    cmd = build_classic_cmd(self.ffmpeg, self.src, dst, self.blur.get(), "libx264", self.fast_mode.get())
                    subprocess.run(cmd, check=True, capture_output=True, text=True, creationflags=_NO_WINDOW)
                    self.root.after(0, self._done, dst, True)
                    return
                except Exception as exc2:
                    self.root.after(0, self._err, str(exc2))
                    return
            self.root.after(0, self._err, exc.stderr or str(exc))
        except Exception as exc:
            self.root.after(0, self._err, str(exc))

    def _done(self, dst: Path, cpu_fallback: bool = False):
        note = " (CPU fallback)" if cpu_fallback else ""
        self.status.config(text=f"Done → {dst.name}{note}", fg="#4ade80")
        self.go.config(state=tk.NORMAL)
        self.btn.config(state=tk.NORMAL)
        if messagebox.askyesno("Done", f"Successfully saved:\n{dst}\n\nOpen folder?"):
            try:
                os.startfile(dst.parent)
            except Exception:
                pass

    def _err(self, msg: str):
        self.status.config(text="Conversion failed.", fg="#f87171")
        self.go.config(state=tk.NORMAL)
        self.btn.config(state=tk.NORMAL)
        messagebox.showerror("Error", str(msg)[-800:])
