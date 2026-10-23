"""Publish GitHub Release v1.2.0 and upload assets using Python urllib."""

import json
import mimetypes
import os
from pathlib import Path
from urllib.request import Request, urlopen

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = "SmarterGamers/SmarterGamers-Vertical-Blur"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "SmarterGamers-Release-Publisher",
}

BODY_MD = """## SmarterGamers Vertical Blur Editor v1.2.0 — The Creator Studio Update

The third major release of **SmarterGamers Vertical Blur Editor**, introducing high-performance sub-clip trimming, multi-track audio downmixing (Game + Mic), duration-aware preview scrubbing, and volume gain controls.

---

### ⚡ Quick Install for Gamers (No Dev Tools Required)

#### Option 1: 1-Line PowerShell Quick Install
Open **PowerShell** or **Windows Terminal** and run:
```powershell
irm https://raw.githubusercontent.com/SmarterGamers/SmarterGamers-Vertical-Blur/main/install.ps1 | iex
```

#### Option 2: Windows Setup Installer
- **Windows Installer:** [`SmarterGamers.Vertical.Blur.Editor.Setup.1.2.0.exe`](https://github.com/SmarterGamers/SmarterGamers-Vertical-Blur/releases/download/v1.2.0/SmarterGamers.Vertical.Blur.Editor.Setup.1.2.0.exe)
  - Desktop & Start Menu shortcuts
  - Windows Explorer Right-Click Context Menu ("Convert to 9:16 Short with SmarterGamers")
  - Clean Uninstaller included

#### Option 3: Portable Edition (Standalone)
- **Portable Executable:** [`SmarterGamers.Vertical.Blur.Editor.1.2.0.exe`](https://github.com/SmarterGamers/SmarterGamers-Vertical-Blur/releases/download/v1.2.0/SmarterGamers.Vertical.Blur.Editor.1.2.0.exe)
  - 100% self-contained standalone executable — no installation required.

---

### 🚀 What's New in v1.2.0

1. **✂️ Sub-Clip Trimming & Range Selection:**
   - Turn 5-minute full gameplay sessions into 15s, 30s, or 60s viral Shorts in one step without needing external video editors.
   - Fast-seek engine seeks instantly to keyframes before processing.

2. **🎙️ Multi-Track Audio Mixing (Game + Mic):**
   - Automatically detects OBS multi-track recordings (Desktop Game Sound on Track 1, Microphone on Track 2) and cleanly merges them into unified stereo audio.

3. **🔊 Volume Gain & Normalization:**
   - Boost quiet gameplay dialogue or mics (+3dB, +6dB) or normalize volume directly in the app.

4. **⏱️ Interactive Live Preview Scrubber:**
   - Scrub through your entire clip to inspect blur strength, framing, and HUD alignment at any timestamp with duration-aware seeking guards.

5. **⚡ Smart Decode Acceleration:**
   - Auto-optimized decoding pipeline: lightweight CPU decoding for fast 1080p clips to avoid GPU context latency, and hardware acceleration for 4K UHD captures.
"""

def create_release():
    url = f"https://api.github.com/repos/{REPO}/releases"
    payload = {
        "tag_name": "v1.2.0",
        "target_commitish": "main",
        "name": "SmarterGamers Vertical Blur Editor v1.2.0 - The Creator Studio Update",
        "body": BODY_MD,
        "draft": False,
        "prerelease": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=HEADERS, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        print(f"Created Release: {res['html_url']} (ID: {res['id']})")
        return res

def upload_asset(upload_url_tmpl, file_path: Path):
    upload_url = upload_url_tmpl.replace("{?name,label}", "") + f"?name={file_path.name}"
    content_type, _ = mimetypes.guess_type(str(file_path))
    content_type = content_type or "application/octet-stream"
    
    file_bytes = file_path.read_bytes()
    print(f"Uploading {file_path.name} ({len(file_bytes) / (1024*1024):.1f} MB)...")
    
    req = Request(upload_url, data=file_bytes, headers=HEADERS, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(file_bytes)))
    
    with urlopen(req) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        print(f"Uploaded: {res['name']} -> {res['browser_download_url']}")

def main():
    rel = create_release()
    upload_url = rel["upload_url"]
    
    assets_dir = Path("dist/releases")
    for f in [
        assets_dir / "SmarterGamers.Vertical.Blur.Editor.Setup.1.2.0.exe",
        assets_dir / "SmarterGamers.Vertical.Blur.Editor.1.2.0.exe",
        assets_dir / "SHA256SUMS.txt",
    ]:
        if f.exists():
            upload_asset(upload_url, f)
        else:
            print(f"[WARN] File not found: {f}")
    
    print("\nRELEASE v1.2.0 COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
