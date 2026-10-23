"""SmarterGamers Vertical Blur Editor - Desktop Application GUI v1.2.0.

Engineered with CustomTkinter for high-DPI Windows 10/11 dark aesthetic.
Features:
- Native Windows Drag & Drop (windnd)
- Live interactive 9:16 preview frame generation with real-time scrub slider
- Sub-clip trimming controls (Start time & Duration presets: 15s, 30s, 60s, Full)
- Multi-track audio downmixing (Game + Mic) and volume gain tuning (-6dB to +6dB)
- Universal preset selector (9:16 Shorts, 4K, 720p, 1:1 Square, 4:5 Portrait)
- Queue card selection for instant preview switching and individual item actions
"""

import os
import subprocess
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Dict, List, Optional

import customtkinter as ctk
from PIL import Image

from core.batch_processor import BatchProcessor, JobConfig, RenderJob
from core.ffmpeg_engine import FFmpegEngine, VideoMetadata
from ui import theme

try:
    import windnd
    HAS_WINDND = True
except ImportError:
    HAS_WINDND = False

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("green")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window properties
        self.title("SmarterGamers — Vertical Blur Studio v1.2.0 (GPU Accelerated)")
        self.geometry("1180x780")
        self.minsize(1060, 700)
        self.configure(fg_color=theme.BG_DARK)

        # Set App Icon
        icon_path = Path(__file__).resolve().parent.parent.parent / "assets" / "app_icon.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        # Core Engines
        self.engine = FFmpegEngine()
        self.batch = BatchProcessor(self.engine)

        # State
        self.preview_job: Optional[RenderJob] = None
        self._preview_timer = None
        self._preview_image_ref = None
        self.selected_output_folder: Optional[Path] = None
        self.scrub_timestamp: float = 1.0

        # Build UI
        self._build_header()
        self._build_main_layout()
        self._build_footer()

        # Connect Batch Callbacks
        self.batch.on_queue_updated = self._on_queue_updated_threadsafe
        self.batch.on_job_progress = self._on_job_progress_threadsafe
        self.batch.on_job_finished = self._on_job_finished_threadsafe
        self.batch.on_all_finished = self._on_all_finished_threadsafe

        # Hook Drag and Drop
        if HAS_WINDND:
            try:
                windnd.hook_dropfiles(self, func=self._on_files_dropped)
            except Exception:
                pass

    def _on_files_dropped(self, files):
        valid_paths = []
        for f in files:
            p_str = f.decode("utf-8", errors="ignore") if isinstance(f, bytes) else str(f)
            p = Path(p_str)
            if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv"):
                valid_paths.append(p)

        if valid_paths:
            self._queue_paths(valid_paths)

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=theme.BG_SURFACE, corner_radius=0, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", padx=20, pady=8)

        title_lbl = ctk.CTkLabel(
            title_box,
            text="SMARTERGAMERS",
            font=("Segoe UI", 11, "bold"),
            text_color=theme.ACCENT_GREEN,
        )
        title_lbl.pack(anchor="w")

        sub_lbl = ctk.CTkLabel(
            title_box,
            text="Vertical Blur Studio v1.2.0",
            font=("Segoe UI", 16, "bold"),
            text_color=theme.TEXT_PRIMARY,
        )
        sub_lbl.pack(anchor="w")

        best_enc = self.engine.preferred_encoder
        hw_pill = ctk.CTkFrame(header, fg_color=theme.BG_SURFACE_ALT, border_width=1, border_color=theme.BORDER_COLOR, corner_radius=20)
        hw_pill.pack(side="right", padx=20, pady=12)

        hw_text = f"⚡ {best_enc.name}" if self.engine.ffmpeg_exe else "❌ FFmpeg Missing"
        hw_color = theme.ACCENT_GREEN if "NVENC" in best_enc.name or "GPU" in best_enc.name else theme.ACCENT_CYAN
        if not self.engine.ffmpeg_exe:
            hw_color = theme.STATUS_ERROR

        hw_lbl = ctk.CTkLabel(
            hw_pill,
            text=hw_text,
            font=("Segoe UI", 10, "bold"),
            text_color=hw_color,
            padx=14,
            pady=4,
        )
        hw_lbl.pack()

    def _build_main_layout(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=10)

        body.grid_columnconfigure(0, weight=4, minsize=420)
        body.grid_columnconfigure(1, weight=6, minsize=580)
        body.grid_rowconfigure(0, weight=1)

        self._build_left_panel(body)
        self._build_right_panel(body)

    def _build_left_panel(self, parent):
        left = ctk.CTkFrame(parent, fg_color=theme.BG_SURFACE, corner_radius=12, border_width=1, border_color=theme.BORDER_COLOR)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)

        p_hdr = ctk.CTkLabel(left, text="PREVIEW & TUNING", font=theme.FONT_HEADING, text_color=theme.TEXT_PRIMARY)
        p_hdr.pack(anchor="w", padx=16, pady=(12, 6))

        # 9:16 Canvas Container
        self.preview_container = ctk.CTkFrame(left, fg_color=theme.BG_CARD, corner_radius=8, width=210, height=340, border_width=1, border_color=theme.BORDER_HIGHLIGHT)
        self.preview_container.pack(pady=2)
        self.preview_container.pack_propagate(False)

        dnd_hint = "\n(Drag & Drop videos anywhere)" if HAS_WINDND else ""
        self.preview_label = ctk.CTkLabel(
            self.preview_container,
            text=f"No Video Loaded\n\nAdd clips to preview\nblur & alignment{dnd_hint}",
            text_color=theme.TEXT_MUTED,
            font=theme.FONT_BODY,
        )
        self.preview_label.pack(expand=True, fill="both")

        # Preview Scrubber Bar
        scrub_box = ctk.CTkFrame(left, fg_color="transparent")
        scrub_box.pack(fill="x", padx=16, pady=(4, 6))

        ctk.CTkLabel(scrub_box, text="Scrub:", font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED).pack(side="left")
        self.scrub_lbl = ctk.CTkLabel(scrub_box, text="0.0s", font=theme.FONT_SMALL, text_color=theme.ACCENT_CYAN)
        self.scrub_lbl.pack(side="right")

        self.scrub_slider = ctk.CTkSlider(
            scrub_box,
            from_=0.0,
            to=30.0,
            number_of_steps=60,
            command=self._on_scrub_changed,
            button_color=theme.ACCENT_CYAN,
            progress_color=theme.ACCENT_CYAN,
            height=14,
        )
        self.scrub_slider.set(1.0)
        self.scrub_slider.pack(fill="x", padx=(6, 6))

        # Tabbed Tuning Section (Blur / Trimming / Audio)
        tabview = ctk.CTkTabview(left, fg_color=theme.BG_SURFACE_ALT, corner_radius=8)
        tabview.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        t_blur = tabview.add("Blur & Frame")
        t_trim = tabview.add("Trim Highlight")
        t_audio = tabview.add("Audio")

        # --- TAB 1: BLUR & FRAME ---
        blur_box = ctk.CTkFrame(t_blur, fg_color="transparent")
        blur_box.pack(fill="x", pady=1)
        ctk.CTkLabel(blur_box, text="Blur Radius:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.blur_val_lbl = ctk.CTkLabel(blur_box, text="20", font=theme.FONT_SMALL, text_color=theme.ACCENT_GREEN)
        self.blur_val_lbl.pack(side="right")

        self.blur_slider = ctk.CTkSlider(
            t_blur,
            from_=5,
            to=50,
            number_of_steps=45,
            command=self._on_slider_changed,
            button_color=theme.ACCENT_GREEN,
            button_hover_color=theme.ACCENT_GREEN_HOVER,
            progress_color=theme.ACCENT_GREEN,
        )
        self.blur_slider.set(20)
        self.blur_slider.pack(fill="x", pady=(0, 4))

        mode_box = ctk.CTkFrame(t_blur, fg_color="transparent")
        mode_box.pack(fill="x", pady=2)
        ctk.CTkLabel(mode_box, text="Engine:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.algo_segment = ctk.CTkSegmentedButton(
            mode_box,
            values=["⚡ Fast BoxBlur (15x)", "Gaussian Blur"],
            command=self._on_filter_changed,
            selected_color=theme.ACCENT_GREEN,
            unselected_color=theme.BG_SURFACE,
        )
        self.algo_segment.set("⚡ Fast BoxBlur (15x)")
        self.algo_segment.pack(side="right")

        dim_box = ctk.CTkFrame(t_blur, fg_color="transparent")
        dim_box.pack(fill="x", pady=(4, 1))
        ctk.CTkLabel(dim_box, text="Contrast Dim:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.dim_val_lbl = ctk.CTkLabel(dim_box, text="30% Darker", font=theme.FONT_SMALL, text_color=theme.ACCENT_CYAN)
        self.dim_val_lbl.pack(side="right")

        self.dim_slider = ctk.CTkSlider(
            t_blur,
            from_=0,
            to=60,
            number_of_steps=60,
            command=self._on_slider_changed,
            button_color=theme.ACCENT_CYAN,
            progress_color=theme.ACCENT_CYAN,
        )
        self.dim_slider.set(30)
        self.dim_slider.pack(fill="x", pady=(0, 4))

        pos_box = ctk.CTkFrame(t_blur, fg_color="transparent")
        pos_box.pack(fill="x", pady=2)
        ctk.CTkLabel(pos_box, text="Framing:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.align_segment = ctk.CTkSegmentedButton(
            pos_box,
            values=["Top (HUD)", "Centered", "Bottom"],
            command=self._on_filter_changed,
            selected_color=theme.ACCENT_BLUE,
            unselected_color=theme.BG_SURFACE,
        )
        self.align_segment.set("Centered")
        self.align_segment.pack(side="right")

        # --- TAB 2: TRIM HIGHLIGHT ---
        trim_toggle_row = ctk.CTkFrame(t_trim, fg_color="transparent")
        trim_toggle_row.pack(fill="x", pady=2)
        self.trim_switch = ctk.CTkSwitch(
            trim_toggle_row,
            text="Enable Sub-Clip Trimming",
            font=theme.FONT_BODY_BOLD,
            progress_color=theme.ACCENT_GREEN,
            command=self._on_trim_switch_toggle,
        )
        self.trim_switch.pack(side="left")

        self.trim_controls_frame = ctk.CTkFrame(t_trim, fg_color="transparent")
        self.trim_controls_frame.pack(fill="x", pady=4)

        start_row = ctk.CTkFrame(self.trim_controls_frame, fg_color="transparent")
        start_row.pack(fill="x", pady=2)
        ctk.CTkLabel(start_row, text="Start Time (s):", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY).pack(side="left")
        self.start_time_entry = ctk.CTkEntry(start_row, width=80, height=26, font=theme.FONT_SMALL)
        self.start_time_entry.insert(0, "0.0")
        self.start_time_entry.pack(side="right")

        dur_row = ctk.CTkFrame(self.trim_controls_frame, fg_color="transparent")
        dur_row.pack(fill="x", pady=2)
        ctk.CTkLabel(dur_row, text="Duration (s):", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY).pack(side="left")
        self.duration_entry = ctk.CTkEntry(dur_row, width=80, height=26, font=theme.FONT_SMALL)
        self.duration_entry.insert(0, "15.0")
        self.duration_entry.pack(side="right")

        quick_dur_row = ctk.CTkFrame(self.trim_controls_frame, fg_color="transparent")
        quick_dur_row.pack(fill="x", pady=4)
        ctk.CTkLabel(quick_dur_row, text="Quick Presets:", font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED).pack(side="left")
        for q_dur in ["15s", "30s", "60s"]:
            ctk.CTkButton(
                quick_dur_row,
                text=q_dur,
                width=45,
                height=22,
                font=theme.FONT_SMALL,
                fg_color=theme.BG_SURFACE,
                hover_color=theme.BORDER_COLOR,
                command=lambda d=q_dur[:-1]: self._set_quick_duration(d),
            ).pack(side="left", padx=4)

        # --- TAB 3: AUDIO ---
        a_row1 = ctk.CTkFrame(t_audio, fg_color="transparent")
        a_row1.pack(fill="x", pady=2)
        ctk.CTkLabel(a_row1, text="Audio Track Mode:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.audio_mode_menu = ctk.CTkOptionMenu(
            a_row1,
            values=[
                "Direct Stream Copy (Lossless)",
                "Mix All Tracks (Game + Mic)",
                "High Quality AAC (192k)",
                "Mute Audio",
            ],
            width=220,
            font=theme.FONT_SMALL,
        )
        self.audio_mode_menu.pack(side="right")

        a_row2 = ctk.CTkFrame(t_audio, fg_color="transparent")
        a_row2.pack(fill="x", pady=(8, 2))
        ctk.CTkLabel(a_row2, text="Volume Boost:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.vol_lbl = ctk.CTkLabel(a_row2, text="0.0 dB", font=theme.FONT_SMALL, text_color=theme.ACCENT_CYAN)
        self.vol_lbl.pack(side="right")

        self.vol_slider = ctk.CTkSlider(
            t_audio,
            from_=-6.0,
            to=6.0,
            number_of_steps=24,
            command=self._on_vol_slider_changed,
            button_color=theme.ACCENT_CYAN,
            progress_color=theme.ACCENT_CYAN,
        )
        self.vol_slider.set(0.0)
        self.vol_slider.pack(fill="x", pady=(0, 4))

    def _build_right_panel(self, parent):
        right = ctk.CTkFrame(parent, fg_color=theme.BG_SURFACE, corner_radius=12, border_width=1, border_color=theme.BORDER_COLOR)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)

        tbar = ctk.CTkFrame(right, fg_color="transparent")
        tbar.pack(fill="x", padx=16, pady=(12, 6))

        ctk.CTkLabel(tbar, text="BATCH QUEUE", font=theme.FONT_HEADING, text_color=theme.TEXT_PRIMARY).pack(side="left")

        self.btn_clear = ctk.CTkButton(
            tbar,
            text="Clear Done",
            font=theme.FONT_SMALL,
            width=80,
            height=28,
            fg_color=theme.BG_SURFACE_ALT,
            hover_color=theme.BORDER_COLOR,
            command=self._clear_completed,
        )
        self.btn_clear.pack(side="right", padx=(8, 0))

        self.btn_add = ctk.CTkButton(
            tbar,
            text="+ Add Video Clips…",
            font=theme.FONT_BODY_BOLD,
            height=32,
            fg_color=theme.ACCENT_GREEN,
            hover_color=theme.ACCENT_GREEN_HOVER,
            text_color="#0d1117",
            command=self._add_videos_dialog,
        )
        self.btn_add.pack(side="right")

        self.queue_frame = ctk.CTkScrollableFrame(right, fg_color=theme.BG_CARD, corner_radius=8, border_width=1, border_color=theme.BORDER_COLOR)
        self.queue_frame.pack(fill="both", expand=True, padx=16, pady=4)

        self.empty_label = ctk.CTkLabel(
            self.queue_frame,
            text="No videos in queue.\nClick '+ Add Video Clips…' or drag and drop videos here.",
            text_color=theme.TEXT_MUTED,
            font=theme.FONT_BODY,
        )
        self.empty_label.pack(pady=40)

        self.queue_widgets: Dict[str, Dict] = {}

        # Export & Encoder Settings Card
        settings_box = ctk.CTkFrame(right, fg_color=theme.BG_SURFACE_ALT, corner_radius=8, border_width=1, border_color=theme.BORDER_COLOR)
        settings_box.pack(fill="x", padx=16, pady=(8, 6))

        s_row1 = ctk.CTkFrame(settings_box, fg_color="transparent")
        s_row1.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(s_row1, text="Preset:", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY).pack(side="left", padx=(0, 6))
        self.res_menu = ctk.CTkOptionMenu(
            s_row1,
            values=[
                "9:16 Shorts / TikTok (1080 × 1920)",
                "9:16 4K Ultra HD (2160 × 3840)",
                "9:16 720p Fast Draft (720 × 1280)",
                "1:1 Square Feed (1080 × 1080)",
                "4:5 Portrait Feed (1080 × 1350)",
            ],
            font=theme.FONT_SMALL,
            width=250,
        )
        self.res_menu.pack(side="left")

        ctk.CTkLabel(s_row1, text="Hardware:", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY).pack(side="left", padx=(12, 6))
        enc_names = [f"{e.name}" for e in self.engine.available_encoders]
        self.enc_menu = ctk.CTkOptionMenu(
            s_row1,
            values=enc_names,
            font=theme.FONT_SMALL,
            width=210,
        )
        self.enc_menu.pack(side="left")

        s_row2 = ctk.CTkFrame(settings_box, fg_color="transparent")
        s_row2.pack(fill="x", padx=12, pady=(4, 8))

        ctk.CTkLabel(s_row2, text="Save to:", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY).pack(side="left", padx=(0, 6))
        self.out_label = ctk.CTkLabel(
            s_row2,
            text="Same folder as source video",
            font=theme.FONT_SMALL,
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        self.out_label.pack(side="left", fill="x", expand=True)

        self.btn_browse_out = ctk.CTkButton(
            s_row2,
            text="Change Folder…",
            font=theme.FONT_SMALL,
            height=26,
            width=110,
            fg_color=theme.BORDER_COLOR,
            hover_color=theme.BORDER_HIGHLIGHT,
            command=self._choose_output_folder,
        )
        self.btn_browse_out.pack(side="right")

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color=theme.BG_SURFACE, corner_radius=0, height=72)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        inner = ctk.CTkFrame(footer, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=20, pady=10)

        left_status = ctk.CTkFrame(inner, fg_color="transparent")
        left_status.pack(side="left", fill="x", expand=True)

        self.status_lbl = ctk.CTkLabel(
            left_status,
            text="Ready. Add gameplay clips to start batch convert.",
            font=theme.FONT_BODY,
            text_color=theme.TEXT_SECONDARY,
            anchor="w",
        )
        self.status_lbl.pack(anchor="w")

        self.progress_bar = ctk.CTkProgressBar(left_status, height=8, progress_color=theme.ACCENT_GREEN)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", pady=(4, 0))

        self.btn_open_folder = ctk.CTkButton(
            inner,
            text="Open Folder",
            font=theme.FONT_BODY,
            height=38,
            width=110,
            fg_color=theme.BG_SURFACE_ALT,
            hover_color=theme.BORDER_HIGHLIGHT,
            command=self._open_output_folder,
            state="disabled",
        )
        self.btn_open_folder.pack(side="right", padx=(8, 0))

        self.btn_convert = ctk.CTkButton(
            inner,
            text="CONVERT ALL TO 9:16",
            font=theme.FONT_BODY_BOLD,
            height=38,
            width=220,
            fg_color=theme.ACCENT_GREEN,
            hover_color=theme.ACCENT_GREEN_HOVER,
            text_color="#0b1202",
            command=self._on_convert_click,
            state="disabled",
        )
        self.btn_convert.pack(side="right")

    def _get_current_job_config(self) -> JobConfig:
        blur = int(self.blur_slider.get())
        is_box = "BoxBlur" in self.algo_segment.get()
        algo = "boxblur" if is_box else "gaussian"

        dim_pct = self.dim_slider.get()
        dim_factor = max(0.4, 1.0 - (dim_pct / 100.0))

        align = self.align_segment.get()
        offset = 0
        if "Top" in align:
            offset = -140
        elif "Bottom" in align:
            offset = 140

        res_str = self.res_menu.get()
        target_w, target_h = 1080, 1920
        if "720" in res_str:
            target_w, target_h = 720, 1280
        elif "2160" in res_str:
            target_w, target_h = 2160, 3840
        elif "Square" in res_str:
            target_w, target_h = 1080, 1080
        elif "Portrait" in res_str:
            target_w, target_h = 1080, 1350

        enc_choice = self.enc_menu.get()
        selected_enc = self.engine.preferred_encoder.encoder_id
        for e in self.engine.available_encoders:
            if e.name in enc_choice:
                selected_enc = e.encoder_id
                break

        # Trimming
        start_sec = None
        duration_sec = None
        if self.trim_switch.get() == 1:
            try:
                start_sec = float(self.start_time_entry.get())
            except Exception:
                start_sec = 0.0
            try:
                duration_sec = float(self.duration_entry.get())
            except Exception:
                duration_sec = 15.0

        # Audio mode
        a_mode_str = self.audio_mode_menu.get()
        audio_mode = "copy"
        if "Mix" in a_mode_str:
            audio_mode = "mix_all"
        elif "AAC" in a_mode_str:
            audio_mode = "aac_high"
        elif "Mute" in a_mode_str:
            audio_mode = "mute"

        vol_db = float(self.vol_slider.get())

        return JobConfig(
            target_w=target_w,
            target_h=target_h,
            blur_strength=blur,
            blur_algorithm=algo,
            dim_factor=dim_factor,
            vertical_offset=offset,
            encoder_id=selected_enc,
            audio_mode=audio_mode,
            audio_gain_db=vol_db,
            start_sec=start_sec,
            duration_sec=duration_sec,
            output_dir=self.selected_output_folder,
        )

    def _on_slider_changed(self, value):
        self.blur_val_lbl.configure(text=f"{int(self.blur_slider.get())}")
        self.dim_val_lbl.configure(text=f"{int(self.dim_slider.get())}% Darker")
        self._trigger_preview_update()

    def _on_vol_slider_changed(self, value):
        self.vol_lbl.configure(text=f"{float(value):+.1f} dB")

    def _on_scrub_changed(self, value):
        self.scrub_timestamp = float(value)
        self.scrub_lbl.configure(text=f"{self.scrub_timestamp:.1f}s")
        self._trigger_preview_update()

    def _on_filter_changed(self, value):
        self._trigger_preview_update()

    def _on_trim_switch_toggle(self):
        # Update UI if trim is enabled
        pass

    def _set_quick_duration(self, seconds_str: str):
        self.duration_entry.delete(0, "end")
        self.duration_entry.insert(0, f"{float(seconds_str):.1f}")
        if self.trim_switch.get() == 0:
            self.trim_switch.select()

    def _trigger_preview_update(self):
        if not self.preview_job:
            return
        if self._preview_timer:
            self.after_cancel(self._preview_timer)
        self._preview_timer = self.after(200, self._render_preview)

    def _render_preview(self):
        if not self.preview_job:
            return
        cfg = self._get_current_job_config()
        ts = self.scrub_timestamp
        dur = self.preview_job.metadata.duration_sec if self.preview_job.metadata else None

        def _worker():
            img = self.engine.generate_preview_frame(
                src=self.preview_job.src_path,
                timestamp_sec=ts,
                blur_strength=cfg.blur_strength,
                blur_algorithm=cfg.blur_algorithm,
                dim_factor=cfg.dim_factor,
                vertical_offset=cfg.vertical_offset,
                sharp_scale=cfg.sharp_scale,
                preview_height=340,
                video_duration=dur,
            )
            if img:
                self.after(0, self._display_preview_image, img)

        threading.Thread(target=_worker, daemon=True).start()

    def _display_preview_image(self, pil_img: Image.Image):
        ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=pil_img.size)
        self.preview_label.configure(image=ctk_img, text="")
        self._preview_image_ref = ctk_img

    def _add_videos_dialog(self):
        paths = filedialog.askopenfilenames(
            title="Select Gameplay Videos to Convert",
            filetypes=[
                ("Video Files", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"),
                ("All Files", "*.*"),
            ],
        )
        if paths:
            self._queue_paths([Path(p) for p in paths])

    def _queue_paths(self, paths: List[Path]):
        cfg = self._get_current_job_config()
        new_jobs = self.batch.add_files(paths, cfg)

        if not self.preview_job and new_jobs:
            self._select_preview_job(new_jobs[0])

        self.btn_convert.configure(state="normal")

    def _choose_output_folder(self):
        folder = filedialog.askdirectory(title="Select Output Folder")
        if folder:
            self.selected_output_folder = Path(folder)
            self.out_label.configure(text=str(self.selected_output_folder), text_color=theme.TEXT_PRIMARY)
        else:
            self.selected_output_folder = None
            self.out_label.configure(text="Same folder as source video", text_color=theme.TEXT_MUTED)

    def _clear_completed(self):
        self.batch.clear_completed()

    def _on_convert_click(self):
        if self.batch._is_running:
            self.batch.stop()
            self.btn_convert.configure(text="Stopping...", state="disabled")
            return

        cfg = self._get_current_job_config()
        for j in self.batch.queue:
            if j.status == "pending":
                j.config = cfg
                if self.selected_output_folder:
                    suffix = "_short.mp4" if cfg.duration_sec else "_vertical.mp4"
                    j.dst_path = self.selected_output_folder / f"{j.src_path.stem}{suffix}"

        self.btn_convert.configure(text="STOP CONVERSION", fg_color=theme.STATUS_ERROR, hover_color="#dc2626")
        self.btn_add.configure(state="disabled")
        self.batch.start()

    def _open_output_folder(self):
        target = self.selected_output_folder
        if not target and self.batch.queue:
            target = self.batch.queue[0].dst_path.parent
        if target and target.exists():
            try:
                os.startfile(str(target))
            except Exception:
                pass

    def _on_queue_updated_threadsafe(self):
        self.after(0, self._render_queue_ui)

    def _on_job_progress_threadsafe(self, job: RenderJob):
        self.after(0, self._update_job_progress_ui, job)

    def _on_job_finished_threadsafe(self, job: RenderJob):
        self.after(0, self._update_job_progress_ui, job)

    def _on_all_finished_threadsafe(self, success_count: int, error_count: int):
        self.after(0, self._on_batch_complete_ui, success_count, error_count)

    def _render_queue_ui(self):
        for wid in self.queue_frame.winfo_children():
            wid.destroy()
        self.queue_widgets.clear()

        if not self.batch.queue:
            dnd_hint = "\n(Drag and Drop video files directly here)" if HAS_WINDND else ""
            self.empty_label = ctk.CTkLabel(
                self.queue_frame,
                text=f"No videos in queue.\nClick '+ Add Video Clips…'{dnd_hint}",
                text_color=theme.TEXT_MUTED,
                font=theme.FONT_BODY,
            )
            self.empty_label.pack(pady=40)
            self.btn_convert.configure(state="disabled")
            return

        for job in self.batch.queue:
            card = ctk.CTkFrame(self.queue_frame, fg_color=theme.BG_SURFACE_ALT, corner_radius=8, border_width=1, border_color=theme.BORDER_COLOR)
            card.pack(fill="x", pady=3, padx=2)

            top_row = ctk.CTkFrame(card, fg_color="transparent")
            top_row.pack(fill="x", padx=10, pady=(6, 2))

            name_lbl = ctk.CTkButton(
                top_row,
                text=job.src_path.name,
                font=theme.FONT_BODY_BOLD,
                text_color=theme.TEXT_PRIMARY,
                fg_color="transparent",
                hover_color=theme.BG_SURFACE,
                anchor="w",
                command=lambda j=job: self._select_preview_job(j),
            )
            name_lbl.pack(side="left", fill="x", expand=True)

            del_btn = ctk.CTkButton(
                top_row,
                text="✕",
                font=("Segoe UI", 10, "bold"),
                width=24,
                height=22,
                fg_color="transparent",
                hover_color=theme.STATUS_ERROR,
                text_color=theme.TEXT_MUTED,
                command=lambda jid=job.id: self.batch.remove_job(jid),
            )
            del_btn.pack(side="right", padx=(6, 0))

            status_pill = ctk.CTkLabel(
                top_row,
                text=self._format_status_badge(job),
                font=theme.FONT_SMALL,
                text_color=self._get_status_color(job),
            )
            status_pill.pack(side="right")

            info_row = ctk.CTkFrame(card, fg_color="transparent")
            info_row.pack(fill="x", padx=10, pady=(0, 4))

            res_txt = f"{job.metadata.width}×{job.metadata.height}" if job.metadata else "Unknown"
            dur_txt = f"{int(job.metadata.duration_sec)}s" if job.metadata else ""
            tracks_txt = f" • {job.metadata.audio_tracks_count} aud" if (job.metadata and job.metadata.audio_tracks_count > 1) else ""
            meta_lbl = ctk.CTkLabel(info_row, text=f"{res_txt} • {dur_txt}{tracks_txt}", font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED)
            meta_lbl.pack(side="left")

            prog_lbl = ctk.CTkLabel(info_row, text=f"{job.progress:.0f}%", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY)
            prog_lbl.pack(side="right")

            bar = ctk.CTkProgressBar(card, height=4, progress_color=theme.ACCENT_GREEN)
            bar.set(job.progress / 100.0)
            bar.pack(fill="x", padx=10, pady=(0, 6))

            self.queue_widgets[job.id] = {
                "status_lbl": status_pill,
                "prog_lbl": prog_lbl,
                "bar": bar,
            }

    def _select_preview_job(self, job: RenderJob):
        self.preview_job = job
        if job.metadata and job.metadata.duration_sec > 0:
            dur = job.metadata.duration_sec
            self.scrub_slider.configure(to=max(1.0, dur))
            self.scrub_slider.set(min(1.0, dur / 2.0))
            self.scrub_timestamp = self.scrub_slider.get()
            self.scrub_lbl.configure(text=f"{self.scrub_timestamp:.1f}s / {dur:.1f}s")
        self._render_preview()

    def _format_status_badge(self, job: RenderJob) -> str:
        if job.status == "pending":
            return "Pending"
        if job.status == "processing":
            speed = f"({job.speed_factor})" if job.speed_factor else ""
            return f"Converting {speed}"
        if job.status == "completed":
            return "✓ Done"
        if job.status == "error":
            return "✖ Error"
        if job.status == "cancelled":
            return "Cancelled"
        return job.status

    def _get_status_color(self, job: RenderJob) -> str:
        if job.status == "completed":
            return theme.STATUS_SUCCESS
        if job.status == "processing":
            return theme.ACCENT_CYAN
        if job.status == "error":
            return theme.STATUS_ERROR
        return theme.STATUS_PENDING

    def _update_job_progress_ui(self, job: RenderJob):
        if job.id in self.queue_widgets:
            w = self.queue_widgets[job.id]
            w["status_lbl"].configure(
                text=self._format_status_badge(job),
                text_color=self._get_status_color(job),
            )
            w["prog_lbl"].configure(text=f"{job.progress:.0f}%")
            w["bar"].set(job.progress / 100.0)

        total = len(self.batch.queue)
        completed = sum(1 for j in self.batch.queue if j.status == "completed")
        current_active = next((j for j in self.batch.queue if j.status == "processing"), None)

        if current_active:
            self.status_lbl.configure(
                text=f"Rendering: {current_active.src_path.name} ({current_active.speed_factor} speed)",
                text_color=theme.ACCENT_CYAN,
            )
            overall_pct = (completed * 100.0 + current_active.progress) / (total * 100.0)
            self.progress_bar.set(overall_pct)

    def _on_batch_complete_ui(self, success_count: int, error_count: int):
        self.btn_convert.configure(
            text="CONVERT ALL TO 9:16",
            fg_color=theme.ACCENT_GREEN,
            hover_color=theme.ACCENT_GREEN_HOVER,
            state="normal",
        )
        self.btn_add.configure(state="normal")
        self.btn_open_folder.configure(state="normal")
        self.progress_bar.set(1.0)

        msg = f"Batch finished: {success_count} converted successfully."
        if error_count > 0:
            msg += f" ({error_count} failed)"
        self.status_lbl.configure(text=msg, text_color=theme.STATUS_SUCCESS if error_count == 0 else theme.STATUS_WARN)

        if messagebox.askyesno("Batch Finished", f"{msg}\n\nWould you like to open the output folder?"):
            self._open_output_folder()


if __name__ == "__main__":
    app = App()
    app.mainloop()
