"""SmarterGamers Vertical Blur Editor - Desktop Application GUI.
Engineered with CustomTkinter for high-DPI Windows 10/11 dark aesthetic.
"""

import os
import subprocess
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Dict, List, Optional

import customtkinter as ctk
from PIL import Image, ImageTk

from core.batch_processor import BatchProcessor, JobConfig, RenderJob
from core.ffmpeg_engine import FFmpegEngine, VideoMetadata
from ui import theme

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("green")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window properties
        self.title("SmarterGamers — 9:16 Vertical Blur Editor (GPU Accelerated)")
        self.geometry("1100x720")
        self.minsize(980, 640)
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

        # Build UI
        self._build_header()
        self._build_main_layout()
        self._build_footer()

        # Connect Batch Callbacks
        self.batch.on_queue_updated = self._on_queue_updated_threadsafe
        self.batch.on_job_progress = self._on_job_progress_threadsafe
        self.batch.on_job_finished = self._on_job_finished_threadsafe
        self.batch.on_all_finished = self._on_all_finished_threadsafe

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=theme.BG_SURFACE, corner_radius=0, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        # Left title & branding
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
            text="9:16 Vertical Blur Studio",
            font=("Segoe UI", 16, "bold"),
            text_color=theme.TEXT_PRIMARY,
        )
        sub_lbl.pack(anchor="w")

        # Right Hardware encoder pill
        best_enc = self.engine.preferred_encoder
        hw_pill = ctk.CTkFrame(header, fg_color=theme.BG_SURFACE_ALT, border_width=1, border_color=theme.BORDER_COLOR, corner_radius=20)
        hw_pill.pack(side="right", padx=20, pady=12)

        hw_text = f"⚡ {best_enc.name}" if self.engine.ffmpeg_exe else "❌ FFmpeg Not Found"
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
        body.pack(fill="both", expand=True, padx=16, pady=12)

        # Split into Left Panel (Preview & Filters) and Right Panel (Queue & Settings)
        body.grid_columnconfigure(0, weight=4, minsize=380)
        body.grid_columnconfigure(1, weight=6, minsize=540)
        body.grid_rowconfigure(0, weight=1)

        self._build_left_panel(body)
        self._build_right_panel(body)

    def _build_left_panel(self, parent):
        left = ctk.CTkFrame(parent, fg_color=theme.BG_SURFACE, corner_radius=12, border_width=1, border_color=theme.BORDER_COLOR)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)

        # Header
        p_hdr = ctk.CTkLabel(left, text="PREVIEW & TUNING", font=theme.FONT_HEADING, text_color=theme.TEXT_PRIMARY)
        p_hdr.pack(anchor="w", padx=16, pady=(14, 8))

        # 9:16 Canvas Container
        self.preview_container = ctk.CTkFrame(left, fg_color=theme.BG_CARD, corner_radius=8, width=225, height=400, border_width=1, border_color=theme.BORDER_HIGHLIGHT)
        self.preview_container.pack(pady=4)
        self.preview_container.pack_propagate(False)

        self.preview_label = ctk.CTkLabel(self.preview_container, text="No Video Loaded\n\nAdd clips to preview\nblur & alignment", text_color=theme.TEXT_MUTED, font=theme.FONT_BODY)
        self.preview_label.pack(expand=True, fill="both")

        # Controls Container (Scrollable or standard frame)
        ctrls = ctk.CTkFrame(left, fg_color="transparent")
        ctrls.pack(fill="both", expand=True, padx=16, pady=(10, 10))

        # 1. Blur Strength Slider
        blur_box = ctk.CTkFrame(ctrls, fg_color="transparent")
        blur_box.pack(fill="x", pady=2)
        ctk.CTkLabel(blur_box, text="Blur Radius:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.blur_val_lbl = ctk.CTkLabel(blur_box, text="20", font=theme.FONT_SMALL, text_color=theme.ACCENT_GREEN)
        self.blur_val_lbl.pack(side="right")

        self.blur_slider = ctk.CTkSlider(
            ctrls,
            from_=5,
            to=50,
            number_of_steps=45,
            command=self._on_slider_changed,
            button_color=theme.ACCENT_GREEN,
            button_hover_color=theme.ACCENT_GREEN_HOVER,
            progress_color=theme.ACCENT_GREEN,
        )
        self.blur_slider.set(20)
        self.blur_slider.pack(fill="x", pady=(0, 6))

        # 2. Blur Algorithm Mode
        mode_box = ctk.CTkFrame(ctrls, fg_color="transparent")
        mode_box.pack(fill="x", pady=2)
        ctk.CTkLabel(mode_box, text="Blur Engine:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.algo_segment = ctk.CTkSegmentedButton(
            mode_box,
            values=["⚡ Fast BoxBlur (15x)", "Gaussian Blur"],
            command=self._on_filter_changed,
            selected_color=theme.ACCENT_GREEN,
            selected_hover_color=theme.ACCENT_GREEN_HOVER,
            unselected_color=theme.BG_SURFACE_ALT,
            text_color="#ffffff",
        )
        self.algo_segment.set("⚡ Fast BoxBlur (15x)")
        self.algo_segment.pack(side="right")

        # 3. Background Dimming Slider
        dim_box = ctk.CTkFrame(ctrls, fg_color="transparent")
        dim_box.pack(fill="x", pady=(6, 2))
        ctk.CTkLabel(dim_box, text="Background Contrast Dim:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.dim_val_lbl = ctk.CTkLabel(dim_box, text="30% Darker", font=theme.FONT_SMALL, text_color=theme.ACCENT_CYAN)
        self.dim_val_lbl.pack(side="right")

        self.dim_slider = ctk.CTkSlider(
            ctrls,
            from_=0,
            to=60,
            number_of_steps=60,
            command=self._on_slider_changed,
            button_color=theme.ACCENT_CYAN,
            progress_color=theme.ACCENT_CYAN,
        )
        self.dim_slider.set(30)
        self.dim_slider.pack(fill="x", pady=(0, 6))

        # 4. Vertical Alignment (HUD Offset)
        pos_box = ctk.CTkFrame(ctrls, fg_color="transparent")
        pos_box.pack(fill="x", pady=(6, 2))
        ctk.CTkLabel(pos_box, text="Vertical Framing:", font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY).pack(side="left")
        self.align_segment = ctk.CTkSegmentedButton(
            pos_box,
            values=["Top (HUD Bias)", "Centered", "Bottom Bias"],
            command=self._on_filter_changed,
            selected_color=theme.ACCENT_BLUE,
            unselected_color=theme.BG_SURFACE_ALT,
        )
        self.align_segment.set("Centered")
        self.align_segment.pack(side="right")

    def _build_right_panel(self, parent):
        right = ctk.CTkFrame(parent, fg_color=theme.BG_SURFACE, corner_radius=12, border_width=1, border_color=theme.BORDER_COLOR)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)

        # Toolbar
        tbar = ctk.CTkFrame(right, fg_color="transparent")
        tbar.pack(fill="x", padx=16, pady=(14, 8))

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

        # Scrollable Queue List
        self.queue_frame = ctk.CTkScrollableFrame(right, fg_color=theme.BG_CARD, corner_radius=8, border_width=1, border_color=theme.BORDER_COLOR)
        self.queue_frame.pack(fill="both", expand=True, padx=16, pady=4)

        self.empty_label = ctk.CTkLabel(
            self.queue_frame,
            text="No videos in queue.\nClick '+ Add Video Clips…' to select clips to convert.",
            text_color=theme.TEXT_MUTED,
            font=theme.FONT_BODY,
        )
        self.empty_label.pack(pady=40)

        # Queue Items container dictionary: job_id -> widget elements
        self.queue_widgets: Dict[str, Dict] = {}

        # Export & Encoder Settings
        settings_box = ctk.CTkFrame(right, fg_color=theme.BG_SURFACE_ALT, corner_radius=8, border_width=1, border_color=theme.BORDER_COLOR)
        settings_box.pack(fill="x", padx=16, pady=(10, 8))

        # Resolution & Encoder row
        s_row1 = ctk.CTkFrame(settings_box, fg_color="transparent")
        s_row1.pack(fill="x", padx=12, pady=(10, 4))

        ctk.CTkLabel(s_row1, text="Format:", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY).pack(side="left", padx=(0, 6))
        self.res_menu = ctk.CTkOptionMenu(
            s_row1,
            values=["1080 × 1920 (9:16 Shorts / TikTok)", "720 × 1280 (Fast Draft)", "2160 × 3840 (4K UHD)"],
            font=theme.FONT_SMALL,
            width=240,
        )
        self.res_menu.pack(side="left")

        ctk.CTkLabel(s_row1, text="Hardware:", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY).pack(side="left", padx=(14, 6))
        enc_names = [f"{e.name}" for e in self.engine.available_encoders]
        self.enc_menu = ctk.CTkOptionMenu(
            s_row1,
            values=enc_names,
            font=theme.FONT_SMALL,
            width=220,
        )
        self.enc_menu.pack(side="left")

        # Destination Folder row
        s_row2 = ctk.CTkFrame(settings_box, fg_color="transparent")
        s_row2.pack(fill="x", padx=12, pady=(4, 10))

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

        # Status & Progress Area
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

        # Action Buttons
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

        # Selected Encoder
        enc_choice = self.enc_menu.get()
        selected_enc = self.engine.preferred_encoder.encoder_id
        for e in self.engine.available_encoders:
            if e.name in enc_choice:
                selected_enc = e.encoder_id
                break

        return JobConfig(
            target_w=target_w,
            target_h=target_h,
            blur_strength=blur,
            blur_algorithm=algo,
            dim_factor=dim_factor,
            vertical_offset=offset,
            encoder_id=selected_enc,
            output_dir=self.selected_output_folder,
        )

    def _on_slider_changed(self, value):
        self.blur_val_lbl.configure(text=f"{int(self.blur_slider.get())}")
        self.dim_val_lbl.configure(text=f"{int(self.dim_slider.get())}% Darker")
        self._trigger_preview_update()

    def _on_filter_changed(self, value):
        self._trigger_preview_update()

    def _trigger_preview_update(self):
        if not self.preview_job:
            return
        if self._preview_timer:
            self.after_cancel(self._preview_timer)
        self._preview_timer = self.after(300, self._render_preview)

    def _render_preview(self):
        if not self.preview_job:
            return
        cfg = self._get_current_job_config()

        def _worker():
            img = self.engine.generate_preview_frame(
                src=self.preview_job.src_path,
                timestamp_sec=2.0,
                blur_strength=cfg.blur_strength,
                blur_algorithm=cfg.blur_algorithm,
                dim_factor=cfg.dim_factor,
                vertical_offset=cfg.vertical_offset,
                sharp_scale=cfg.sharp_scale,
                preview_height=380,
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
        if not paths:
            return

        cfg = self._get_current_job_config()
        new_jobs = self.batch.add_files([Path(p) for p in paths], cfg)

        # Set first job as preview target if none
        if not self.preview_job and new_jobs:
            self.preview_job = new_jobs[0]
            self._render_preview()

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

        # Update configs on pending jobs
        cfg = self._get_current_job_config()
        for j in self.batch.queue:
            if j.status == "pending":
                j.config = cfg
                if self.selected_output_folder:
                    j.dst_path = self.selected_output_folder / f"{j.src_path.stem}_vertical.mp4"

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

    # Threadsafe callbacks
    def _on_queue_updated_threadsafe(self):
        self.after(0, self._render_queue_ui)

    def _on_job_progress_threadsafe(self, job: RenderJob):
        self.after(0, self._update_job_progress_ui, job)

    def _on_job_finished_threadsafe(self, job: RenderJob):
        self.after(0, self._update_job_progress_ui, job)

    def _on_all_finished_threadsafe(self, success_count: int, error_count: int):
        self.after(0, self._on_batch_complete_ui, success_count, error_count)

    def _render_queue_ui(self):
        # Clean up existing widgets
        for wid in self.queue_frame.winfo_children():
            wid.destroy()
        self.queue_widgets.clear()

        if not self.batch.queue:
            self.empty_label = ctk.CTkLabel(
                self.queue_frame,
                text="No videos in queue.\nClick '+ Add Video Clips…' to select clips to convert.",
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

            name_lbl = ctk.CTkLabel(top_row, text=job.src_path.name, font=theme.FONT_BODY_BOLD, text_color=theme.TEXT_PRIMARY)
            name_lbl.pack(side="left")

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
            meta_lbl = ctk.CTkLabel(info_row, text=f"{res_txt} • {dur_txt} → 9:16 Short", font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED)
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

        # Update overall footer progress
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
