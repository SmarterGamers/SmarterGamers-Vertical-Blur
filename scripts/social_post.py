"""Cross-post SmarterGamers Shorts to TikTok and the Facebook Page, hands-off and free.

When a Short goes live on YouTube, the runner's ``/live_check`` adds it to ``<Clips>/social_queue.json``.
This script (running in the background in Brian's Windows session, see ``--install``) posts each queued clip
to TikTok Studio and to the SmarterGamers Facebook Page as a Reel, in a tab of the already-logged-in debug
Chrome (``scripts/chrome_debug.ps1``, port 9222; started automatically if closed). Falls back to a separate
Playwright profile (``C:\\SmarterGamers\\social_profile``, ``--login``). No API keys, no paid services.

    python scripts/social_post.py --login              # one time: log in to TikTok + Facebook in the window
    python scripts/social_post.py --watch              # background loop (what the logon task runs)
    python scripts/social_post.py --once               # post whatever is queued now, then exit
    python scripts/social_post.py --post clip.mp4 --title "Tank reveal"   # one clip by hand
    python scripts/social_post.py --install            # register the "SmarterGamers Social Poster" logon task

Results go to ``<Clips>/social_posted.json`` (one entry per video_id + platform, never posted twice) and
``<Clips>/social_post.log``. On a failure a screenshot lands in ``<Clips>/social_fail``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from clipper.config import load_config  # noqa: E402

PROFILE_DIR = Path(r"C:\SmarterGamers\social_profile")          # fallback when the debug Chrome isn't available
DEBUG_PROFILE = Path(r"C:\SmarterGamers\chrome_debug")          # copy of Brian's logged-in Chrome (chrome_debug.ps1)
CDP_URL = "http://127.0.0.1:9222"
PLATFORMS = ("tiktok", "facebook")
TIKTOK_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload"
FB_REEL_CREATE = "https://www.facebook.com/reels/create"
FB_PAGE_NAME = "SmarterGamers"
TASK_NAME = "SmarterGamers Social Poster"

GAME_TAGS = [  # (keywords seen in the title or source name, hashtags)
    (("forza",), "#forzahorizon6 #forza"),
    (("battlefield", "bf6", "conquest"), "#battlefield6 #bf6"),
    (("call of duty", "callofduty", "black ops", "bo7", "warzone", "cod"), "#callofduty #blackops7"),
    (("beamng",), "#beamng #beamngdrive"),
    (("wardogs",), "#wardogs"),
    (("hell let loose", "hll"), "#hellletloose"),
    (("bodycam",), "#bodycam"),
    (("need for speed", "nfs", "underground"), "#needforspeed #nfsu2"),
    (("worldofwarships", "world of warships", "warships"), "#worldofwarships"),
    (("flightsimulator", "flight simulator", "msfs"), "#msfs #flightsimulator"),
    (("granturismo", "gran turismo"), "#granturismo #ps1"),
    (("nhl",), "#nhl #ps1"),
    (("tripleplay", "baseball"), "#baseball #ps1"),
    (("bmx", "mathoffman"), "#bmx #ps1"),
    (("trashtalk", "trash talk", "ai robot", "chatgpt"), "#ai #chatgpt"),
]
BASE_TAGS = "#gaming #streamer #smartergamers"
GAME_NAMES = {"#forzahorizon6": "Forza Horizon 6", "#battlefield6": "Battlefield 6", "#callofduty": "Call of Duty",
              "#beamng": "BeamNG", "#wardogs": "WARDOGS", "#hellletloose": "Hell Let Loose", "#bodycam": "Bodycam",
              "#needforspeed": "Need for Speed Underground 2", "#worldofwarships": "World of Warships",
              "#msfs": "Flight Simulator", "#granturismo": "Gran Turismo 2", "#nhl": "NHL 2001",
              "#baseball": "Triple Play Baseball", "#bmx": "Mat Hoffman's Pro BMX", "#ai": "AI trash-talk bot"}


# ------------------------------------------------------------------------------------------ pure helpers
def build_caption(title: str, source: str = "", platform: str = "tiktok") -> str:
    """Three lines: title (YouTube title minus '#Shorts'), a one-line description naming the game, and exactly
    five hashtags (game tags first, then #gaming #streamer #smartergamers to fill)."""
    t = re.sub(r"\s*#shorts\b", "", title or "", flags=re.I).strip() or "Stream moment"
    hay = f"{title} {source}".lower().replace("_", " ")
    tags: List[str] = []
    game = ""
    for keys, tag in GAME_TAGS:
        if any(re.search(rf"\b{re.escape(k)}\b", hay) for k in keys):
            tags += [x for x in tag.split() if x not in tags]
            game = game or GAME_NAMES.get(tag.split()[0], "")
    for filler in ("#gaming", "#streamer", "#smartergamers", "#fyp", "#clips"):
        if len(tags) >= 5:
            break
        if filler not in tags:
            tags.append(filler)
    tags = tags[:4] + (["#smartergamers"] if "#smartergamers" not in tags[:4] else [tags[4]])
    desc = (f"Straight from the {game} stream. " if game else "Straight from the stream. ") + \
        "Live on TikTok, Twitch & Kick."
    return f"{t}\n{desc}\n{' '.join(tags[:5])}"

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def enqueue(queue_path: Path, items: List[Dict[str, Any]]) -> int:
    """Adds ``{video_id, output, title, source}`` items not already queued. Returns how many were added."""
    q = load_json(queue_path, [])
    have = {x.get("video_id") for x in q}
    added = 0
    for it in items:
        if it.get("video_id") and it["video_id"] not in have and it.get("output"):
            q.append({k: it.get(k) for k in ("video_id", "output", "title", "source")})
            have.add(it["video_id"])
            added += 1
    if added:
        save_json(queue_path, q)
    return added


def pending(queue: List[Dict[str, Any]], posted: Dict[str, Any], platforms=PLATFORMS) -> List[tuple]:
    """(item, platform) pairs still to do; a platform that failed 3 times is given up on."""
    out = []
    for it in queue:
        for p in platforms:
            rec = posted.get(f"{it['video_id']}:{p}") or {}
            if rec.get("ok") or rec.get("attempts", 0) >= 3:
                continue
            out.append((it, p))
    return out


# ------------------------------------------------------------------------------------------ browser side
class Poster:
    def __init__(self, cfg: Any, headless: bool = False, dry_run: bool = False):
        self.dry_run = dry_run              # go through every step but stop before the final Post click
        self.cfg = cfg
        self.clips = Path(cfg.INBOX).parent
        sfx = "_dryrun" if dry_run else ""      # a dry run never touches the real queue/ledger
        self.queue_path = self.clips / f"social_queue{sfx}.json"
        self.posted_path = self.clips / f"social_posted{sfx}.json"
        self.log_path = self.clips / "social_post.log"
        self.fail_dir = self.clips / "social_fail"
        self.headless = headless
        self._pw = self._ctx = None
        self._browser = self._tab = None

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        try:
            print(line.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8"),
                  flush=True)
        except Exception:  # noqa: BLE001 - pythonw has no console
            pass
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # -- browser lifetime ---------------------------------------------------------------------------------
    def _cdp(self):
        """Attach to the logged-in debug Chrome (scripts/chrome_debug.ps1), starting it if it isn't running."""
        try:
            return self._pw.chromium.connect_over_cdp(CDP_URL, timeout=5000)
        except Exception:  # noqa: BLE001 - not running yet
            pass
        ps1 = Path(__file__).resolve().parent / "chrome_debug.ps1"
        if os.name == "nt" and ps1.exists() and Path(DEBUG_PROFILE, "Default").exists():
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                           capture_output=True, timeout=120)
            try:
                return self._pw.chromium.connect_over_cdp(CDP_URL, timeout=10000)
            except Exception:  # noqa: BLE001
                pass
        return None

    def open(self):
        from playwright.sync_api import sync_playwright
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._cdp() if not self.headless else None
        if self._browser:                       # the already-logged-in debug Chrome: post in a tab of its own
            self._ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            self._tab = self._ctx.new_page()
            return self._ctx
        args = ["--disable-blink-features=AutomationControlled", "--window-size=1280,1000"]
        if not self.headless:
            args.append("--window-position=40,40")
        kw = dict(user_data_dir=str(PROFILE_DIR), headless=self.headless, args=args,
                  viewport={"width": 1280, "height": 960}, ignore_default_args=["--enable-automation"])
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kw)   # real Chrome
        except Exception:  # noqa: BLE001 - Chrome not installed: Playwright's own Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(**kw)
        return self._ctx

    def close(self) -> None:
        try:
            if getattr(self, "_browser", None):
                try:
                    self._tab.close()           # leave the debug Chrome itself running
                except Exception:  # noqa: BLE001
                    pass
            elif self._ctx:
                self._ctx.close()
        finally:
            self._browser = self._tab = None
            if self._pw:
                self._pw.stop()
            self._ctx = self._pw = None

    def page(self):
        if getattr(self, "_tab", None):
            return self._tab
        pages = self._ctx.pages
        return pages[0] if pages else self._ctx.new_page()

    def shot(self, page, platform: str) -> str:
        self.fail_dir.mkdir(parents=True, exist_ok=True)
        p = self.fail_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{platform}.png"
        try:
            page.screenshot(path=str(p), full_page=False)
        except Exception:  # noqa: BLE001
            pass
        return str(p)

    @staticmethod
    def type_lines(page, text: str) -> None:
        """Insert a multi-line caption: one insert per line (no per-key races, emoji intact), Shift+Enter between."""
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            page.keyboard.insert_text(ln)
            if i < len(lines) - 1:
                page.keyboard.press("Shift+Enter")

    def set_file(self, page, path: Path) -> None:
        """Give the page's first file input a local file.

        Attached over CDP, Playwright would stream the whole video through the debugging socket (slow, times
        out on 30-60 MB clips); Chrome runs on this same PC, so hand it the path with DOM.setFileInputFiles.
        """
        if getattr(self, "_browser", None):
            cdp = self._ctx.new_cdp_session(page)
            try:
                root = cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
                node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": 'input[type="file"]'})["nodeId"]
                if node:
                    cdp.send("DOM.setFileInputFiles", {"files": [str(path)], "nodeId": node})
                    return
            finally:
                cdp.detach()
        page.locator('input[type="file"]').first.set_input_files(str(path), timeout=300000)

    # -- platforms --------------------------------------------------------------------------------------
    def tiktok(self, page, clip: Path, caption: str) -> str:
        page.goto(TIKTOK_UPLOAD, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if "login" in page.url:
            raise RuntimeError("TikTok is logged out - run social_post.py --login")
        page.locator('input[type="file"]').first.wait_for(state="attached", timeout=30000)
        self.set_file(page, clip)
        page.get_by_text(re.compile(r"Uploaded", re.I)).first.wait_for(timeout=180000)
        box = page.locator('div[contenteditable="true"]').first
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        self.type_lines(page, caption)
        page.keyboard.press("Escape")
        page.wait_for_timeout(2500)
        post = page.get_by_role("button", name="Post", exact=True)
        post.wait_for(timeout=60000)
        for _ in range(60):                       # enabled once TikTok finishes its checks
            if post.is_enabled():
                break
            page.wait_for_timeout(2000)
        if self.dry_run:
            return "dry-run: ready to post"
        post.click()
        try:
            page.get_by_role("button", name=re.compile(r"^Post now$", re.I)).click(timeout=6000)
        except Exception:  # noqa: BLE001 - no confirmation dialog this time
            pass
        page.wait_for_url(re.compile(r"/tiktokstudio/content"), timeout=90000)
        return "posted"

    def _fb_switch_to_page(self, page) -> None:
        """Account menu -> 'Switch to SmarterGamers' (Facebook posts as whichever profile is active)."""
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        page.get_by_role("button", name=re.compile(r"Your profile|Account controls|Account", re.I)).first.click()
        page.wait_for_timeout(2500)
        for loc in (page.get_by_role("button", name=re.compile(rf"Switch to {FB_PAGE_NAME}", re.I)),
                    page.get_by_text(re.compile(rf"Switch to {FB_PAGE_NAME}", re.I)),
                    page.get_by_role("button", name=re.compile(rf"^{FB_PAGE_NAME}$", re.I))):
            if loc.count():
                loc.first.click()
                page.wait_for_timeout(8000)
                self.log(f"facebook: switched to the {FB_PAGE_NAME} Page")
                return
        raise RuntimeError(f"no 'Switch to {FB_PAGE_NAME}' option in the Facebook account menu")

    def facebook(self, page, clip: Path, caption: str) -> str:
        page.goto(FB_REEL_CREATE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        if "login" in page.url:
            raise RuntimeError("Facebook is logged out - run social_post.py --login")
        if not page.get_by_text(re.compile(rf"on your mind, {FB_PAGE_NAME}", re.I)).count():
            self._fb_switch_to_page(page)
            page.goto(FB_REEL_CREATE, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            if not page.get_by_text(re.compile(rf"on your mind, {FB_PAGE_NAME}", re.I)).count():
                raise RuntimeError(f"Facebook is not acting as the {FB_PAGE_NAME} Page and switching failed")
        page.locator('input[type="file"]').first.wait_for(state="attached", timeout=30000)
        self.set_file(page, clip)
        desc = page.get_by_role("textbox", name=re.compile(r"Describe your reel", re.I)).first
        desc.wait_for(timeout=120000)
        desc.click()
        self.type_lines(page, caption)
        nxt = page.get_by_role("button", name="Next", exact=True).first
        for _ in range(60):
            if nxt.is_enabled():
                break
            page.wait_for_timeout(2000)
        nxt.click()
        post = page.get_by_role("button", name="Post", exact=True).first
        post.wait_for(timeout=60000)
        aud = page.get_by_role("button", name=re.compile(r"Post audience", re.I)).first
        if aud.count() and "public" not in (aud.inner_text() or "").lower():
            raise RuntimeError("Facebook reel audience is not Public")
        if self.dry_run:
            return "dry-run: ready to post"
        post.click()
        post.wait_for(state="detached", timeout=120000)
        return "posted"

    # -- queue processing -------------------------------------------------------------------------------
    def run_queue(self, platforms=PLATFORMS) -> int:
        queue = load_json(self.queue_path, [])
        posted = load_json(self.posted_path, {})
        todo = pending(queue, posted, platforms)
        if not todo:
            return 0
        self.open()
        done = 0
        try:
            page = self.page()
            for it, platform in todo:
                clip = Path(str(it["output"]))
                key = f"{it['video_id']}:{platform}"
                rec = posted.get(key) or {"attempts": 0}
                rec["attempts"] = rec.get("attempts", 0) + 1
                caption = build_caption(it.get("title") or "", it.get("source") or clip.stem, platform)
                try:
                    if not clip.is_file():
                        raise RuntimeError(f"clip missing: {clip}")
                    result = getattr(self, platform)(page, clip, caption)
                    if self.dry_run:
                        self.log(f"{platform}: {result} {it['video_id']} '{caption}'")
                        continue
                    rec.update(ok=True, at=time.strftime("%Y-%m-%dT%H:%M:%S"), caption=caption, error=None)
                    self.log(f"{platform}: posted {it['video_id']} '{caption}'")
                    done += 1
                except Exception as exc:  # noqa: BLE001 - logged, retried next round (max 3)
                    rec.update(ok=False, error=str(exc)[:300], screenshot=self.shot(page, platform))
                    self.log(f"{platform}: FAILED {it['video_id']} (try {rec['attempts']}/3): {str(exc)[:200]}")
                posted[key] = rec
                save_json(self.posted_path, posted)
                page.wait_for_timeout(5000)
        finally:
            self.close()
        return done

    def login(self) -> None:
        self.open()
        page = self.page()
        page.goto("https://www.tiktok.com/login")
        fb = self._ctx.new_page()
        fb.goto("https://www.facebook.com/")
        self.log("login window open: sign in to TikTok and Facebook, switch Facebook to the SmarterGamers Page, "
                 "then close the window")
        try:
            while self._ctx.pages:
                time.sleep(1)
        except Exception:  # noqa: BLE001 - window closed
            pass
        self.close()


def install_task(script: Path, python: Path) -> None:
    """Logon task in Brian's own session (a visible Chrome needs the desktop; the runner's S4U task has none)."""
    pyw = python.with_name("pythonw.exe") if python.with_name("pythonw.exe").exists() else python
    ps = (f"$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument '-X utf8 \"{script}\" --watch' "
          f"-WorkingDirectory '{script.parent.parent}';"
          "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
          "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
          "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5);"
          "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited;"
          f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null;"
          f"Start-ScheduledTask -TaskName '{TASK_NAME}'; 'installed + started'")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--post", help="post one clip file by hand")
    ap.add_argument("--title", default="")
    ap.add_argument("--platforms", default=",".join(PLATFORMS))
    ap.add_argument("--interval", type=int, default=60, help="--watch: seconds between queue checks")
    ap.add_argument("--dry-run", action="store_true", help="do every step except the final Post click")
    ap.add_argument("--headless", action="store_true", help="not recommended: TikTok/Facebook block headless")
    args = ap.parse_args()
    cfg = load_config()
    poster = Poster(cfg, headless=args.headless, dry_run=args.dry_run)
    platforms = tuple(p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS)
    if args.install:
        install_task(Path(__file__).resolve(), Path(sys.executable))
        return 0
    if args.login:
        poster.login()
        return 0
    if args.post:
        clip = Path(args.post)
        vid = "manual_" + re.sub(r"\W+", "_", clip.stem)
        enqueue(poster.queue_path, [{"video_id": vid, "output": str(clip), "title": args.title or clip.stem,
                                     "source": clip.stem}])
        return 0 if poster.run_queue(platforms) else 1
    if args.once:
        poster.run_queue(platforms)
        return 0
    if args.watch:
        poster.log("watching social_queue.json")
        while True:
            try:
                poster.run_queue(platforms)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                poster.log(f"round failed: {exc}")
            time.sleep(max(15, args.interval))
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
