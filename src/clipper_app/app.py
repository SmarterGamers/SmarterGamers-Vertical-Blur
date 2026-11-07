"""Clipper Drop - drop videos or paste links; the local clipper pipeline does the rest.

Run with the clipper venv (no console window):
    C:\\SmarterGamers\\clipper-venv314\\Scripts\\pythonw.exe -m clipper_app.app        (cwd = <repo>\\src)
or double-click scripts\\ClipperDrop.bat. Drag-and-drop needs the free ``tkinterdnd2`` package; without it
the "Add videos" button and the link box still work.
"""
from __future__ import annotations

import os
import queue as _q
import subprocess
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipper.config import load_config  # noqa: E402
from clipper_app.core import (CLIPPING, DONE, DOWNLOADING, FAILED, IN_INBOX, PENDING,  # noqa: E402
                              QUEUE_FILE, JobQueue, Worker, http_ok, is_url, parse_drop_data,
                              recent_uploads)

try:
    from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD  # optional, free
except ImportError:  # pragma: no cover - depends on the PC
    TkinterDnD = None

BG, SURFACE, CARD, BORDER = "#0d0f15", "#151821", "#12151c", "#252b3d"
TEXT, MUTED, GREEN, CYAN, RED, AMBER = "#f0f6fc", "#8b949e", "#8fd400", "#00f0ff", "#f87171", "#fbbf24"
STATUS_COLOR = {PENDING: MUTED, DOWNLOADING: CYAN, IN_INBOX: CYAN, CLIPPING: AMBER, DONE: GREEN, FAILED: RED}


def _open(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - opens Explorer on a local folder
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.clips = Path(self.cfg.INBOX).parent
        self.queue = JobQueue(self.clips / QUEUE_FILE)
        self.events: "_q.Queue[str]" = _q.Queue()
        self.worker = Worker(self.cfg, self.queue, on_change=lambda: self.events.put("jobs"))

        self.root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        self.root.title("SmarterGamers Clipper Drop")
        self.root.geometry("980x680")
        self.root.minsize(760, 520)
        self.root.configure(bg=BG)
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.worker.start()
        self._refresh_jobs()
        self._refresh_uploads()
        self._poll_health()
        self._pump()

    # ------------------------------------------------------------------------------------------- layout
    def _style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED)
        s.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 16, "bold"))
        s.configure("H.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 11, "bold"))
        s.configure("TButton", background=SURFACE, foreground=TEXT, bordercolor=BORDER, padding=(12, 6))
        s.map("TButton", background=[("active", "#1c212e")])
        s.configure("Go.TButton", background=GREEN, foreground="#0d0f15", font=("Segoe UI", 10, "bold"))
        s.map("Go.TButton", background=[("active", "#a3ee05")])
        s.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER)
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=26,
                    bordercolor=BORDER)
        s.configure("Treeview.Heading", background=SURFACE, foreground=MUTED, relief="flat")
        s.map("Treeview", background=[("selected", "#1c2a3e")])

    def _build(self) -> None:
        pad = {"padx": 16}
        top = ttk.Frame(self.root)
        top.pack(fill="x", pady=(14, 6), **pad)
        ttk.Label(top, text="Clipper Drop", style="Title.TLabel").pack(side="left")
        self.status = ttk.Label(top, text="checking…", style="Muted.TLabel")
        self.status.pack(side="right")

        # drop zone
        self.zone = tk.Label(self.root, bg=SURFACE, fg=MUTED, font=("Segoe UI", 12), height=4,
                             highlightthickness=2, highlightbackground=BORDER, highlightcolor=GREEN,
                             text=("Drop videos or links here" if TkinterDnD else
                                   "Add videos with the button, or paste a link below")
                             + "\nTwitch / Kick / YouTube VODs, or files (.mp4 .mov .mkv …)")
        self.zone.pack(fill="x", pady=6, **pad)
        if TkinterDnD:
            self.zone.drop_target_register(DND_FILES, DND_TEXT)
            self.zone.dnd_bind("<<Drop>>", self._on_drop)

        row = ttk.Frame(self.root)
        row.pack(fill="x", pady=(2, 10), **pad)
        ttk.Button(row, text="Add videos…", command=self._pick_files).pack(side="left")
        self.url = ttk.Entry(row)
        self.url.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        self.url.insert(0, "")
        self.url.bind("<Return>", lambda e: self._add_url())
        ttk.Button(row, text="Add link", style="Go.TButton", command=self._add_url).pack(side="left")

        # queue
        ttk.Label(self.root, text="Queue  (one at a time, so the GPU isn't shared)", style="H.TLabel").pack(
            anchor="w", **pad)
        qf = ttk.Frame(self.root)
        qf.pack(fill="both", expand=True, pady=(4, 6), **pad)
        self.jobs = ttk.Treeview(qf, columns=("source", "status", "detail"), show="headings", height=7)
        for c, w, t in (("source", 380, "Video / link"), ("status", 110, "Status"), ("detail", 360, "Detail")):
            self.jobs.heading(c, text=t, anchor="w")
            self.jobs.column(c, width=w, anchor="w", stretch=(c != "status"))
        for st, col in STATUS_COLOR.items():
            self.jobs.tag_configure(st, foreground=col)
        self.jobs.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(qf, orient="vertical", command=self.jobs.yview)
        sb.pack(side="right", fill="y")
        self.jobs.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", pady=(0, 10), **pad)
        self.pause_btn = ttk.Button(btns, text="Pause queue", command=self._toggle_pause)
        self.pause_btn.pack(side="left")
        ttk.Button(btns, text="Retry failed", command=self._retry).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear finished", command=self._clear).pack(side="left")
        ttk.Button(btns, text="Open Ready folder", command=lambda: _open(Path(self.cfg.READY))).pack(side="right")
        ttk.Button(btns, text="Open Inbox", command=lambda: _open(Path(self.cfg.INBOX))).pack(side="right", padx=6)

        # recent uploads
        ttk.Label(self.root, text="Recent Shorts  (double-click to open on YouTube)", style="H.TLabel").pack(
            anchor="w", **pad)
        uf = ttk.Frame(self.root)
        uf.pack(fill="both", expand=True, pady=(4, 14), **pad)
        self.ups = ttk.Treeview(uf, columns=("title", "when", "status"), show="headings", height=7)
        for c, w, t in (("title", 560, "Title"), ("when", 200, "Publishes (UTC)"), ("status", 100, "Status")):
            self.ups.heading(c, text=t, anchor="w")
            self.ups.column(c, width=w, anchor="w")
        self.ups.pack(side="left", fill="both", expand=True)
        self.ups.bind("<Double-1>", self._open_upload)
        self._up_urls: dict = {}

    # ------------------------------------------------------------------------------------------ actions
    def _add(self, sources) -> None:
        added = 0
        for s in sources:
            s = s.strip().strip('"')
            if not s:
                continue
            if not is_url(s) and not Path(s).is_file():
                messagebox.showwarning("Clipper Drop", f"Not a file or link:\n{s}")
                continue
            self.queue.add(s)
            added += 1
        if added:
            self._refresh_jobs()

    def _on_drop(self, event) -> None:
        self._add(parse_drop_data(event.data))

    def _pick_files(self) -> None:
        exts = " ".join("*" + e for e in self.cfg.VIDEO_EXTENSIONS)
        self._add(filedialog.askopenfilenames(title="Videos to clip", filetypes=[("Videos", exts)]))

    def _add_url(self) -> None:
        text = self.url.get()
        links = [t for t in text.split() if t]
        if not links:
            return
        bad = [t for t in links if not is_url(t)]
        if bad:
            messagebox.showwarning("Clipper Drop", "That doesn't look like a link:\n" + "\n".join(bad))
            return
        self._add(links)
        self.url.delete(0, "end")

    def _toggle_pause(self) -> None:
        if self.worker.paused.is_set():
            self.worker.paused.clear()
            self.pause_btn.configure(text="Pause queue")
        else:
            self.worker.paused.set()
            self.pause_btn.configure(text="Resume queue")

    def _retry(self) -> None:
        for j in self.queue.snapshot():
            self.queue.retry(j["id"])
        self._refresh_jobs()

    def _clear(self) -> None:
        self.queue.clear_finished()
        self._refresh_jobs()

    def _open_upload(self, _event) -> None:
        sel = self.ups.focus()
        if sel in self._up_urls:
            webbrowser.open(self._up_urls[sel])

    # ----------------------------------------------------------------------------------------- refresh
    def _refresh_jobs(self) -> None:
        self.jobs.delete(*self.jobs.get_children())
        for j in reversed(self.queue.snapshot()):
            label = j.get("title") or (Path(j["source"]).name if j["kind"] == "file" else j["source"])
            self.jobs.insert("", "end", values=(label, j["status"], j.get("detail", "")), tags=(j["status"],))

    def _refresh_uploads(self) -> None:
        try:
            lines = Path(self.cfg.RUN_LOG).read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        self.ups.delete(*self.ups.get_children())
        self._up_urls = {}
        for u in recent_uploads(lines):
            iid = self.ups.insert("", "end", values=(u["title"], u["publish_at"].replace("T", " ")[:16],
                                                     u["status"]))
            self._up_urls[iid] = u["url"]
        self.root.after(60_000, self._refresh_uploads)

    def _poll_health(self) -> None:
        def check() -> None:
            runner = http_ok(f"http://127.0.0.1:{self.cfg.PORT}/health")
            ollama = http_ok("http://127.0.0.1:11434/api/tags")
            n8n = http_ok("http://127.0.0.1:5678/healthz")
            parts = [("Runner", runner is not None), ("Ollama", ollama is not None), ("n8n", n8n is not None)]
            self.events.put("health:" + "   ".join(f"{'●' if ok else '○'} {n}" for n, ok in parts)
                            + ("" if all(ok for _, ok in parts) else "   — start the missing service"))

        import threading
        threading.Thread(target=check, daemon=True).start()
        self.root.after(15_000, self._poll_health)

    def _pump(self) -> None:
        """Applies worker/health events on the Tk thread (Tk is not thread-safe)."""
        dirty = False
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "jobs":
                    dirty = True
                elif ev.startswith("health:"):
                    txt = ev[7:]
                    self.status.configure(text=txt, foreground=GREEN if "missing" not in txt else AMBER)
        except _q.Empty:
            pass
        if dirty:
            self._refresh_jobs()
        self.root.after(500, self._pump)

    def _close(self) -> None:
        busy = [j for j in self.queue.snapshot() if j["status"] in (DOWNLOADING, CLIPPING)]
        if busy and not messagebox.askyesno(
                "Clipper Drop", "An item is still in progress. Close anyway?\n"
                                "(It resumes next time; a clip already rendering keeps going.)"):
            return
        self.worker.stop.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
