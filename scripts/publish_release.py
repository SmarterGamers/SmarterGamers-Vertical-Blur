"""Publish GitHub Release v1.1.0 and upload assets using Python urllib."""

import os
import json
import mimetypes
from pathlib import Path
from urllib.request import Request, urlopen

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = "SmarterGamers/SmarterGamers-Vertical-Blur"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "SmarterGamers-Release-Publisher",
}

BODY_MD = """## SmarterGamers Vertical Blur Editor v1.1.0 — Performance & Stability Update

The second major update to **SmarterGamers Vertical Blur Editor**, bringing native Windows Drag & Drop, universal aspect ratio presets, 10-bit HDR normalization, and deadlock-free stream processing.

---

### ⚡ Quick Install for Gamers (No Dev Tools Required)

#### Option 1: 1-Line PowerShell Quick Install
Open **PowerShell** or **Windows Terminal** and run:
```powershell
irm https://raw.githubusercontent.com/SmarterGamers/SmarterGamers-Vertical-Blur/main/install.ps1 | iex
```

#### Option 2: Windows Setup Installer
- **Windows Installer:** [`SmarterGamers.Vertical.Blur.Editor.Setup.1.1.0.exe`](https://github.com/SmarterGamers/SmarterGamers-Vertical-Blur/releases/download/v1.1.0/SmarterGamers.Vertical.Blur.Editor.Setup.1.1.0.exe)
  - Desktop & Start Menu shortcuts
  - Windows Explorer Right-Click Context Menu ("Convert to 9:16 Short with SmarterGamers")
  - Clean Uninstaller included

#### Option 3: Portable Edition (Standalone)
- **Portable Executable:** [`SmarterGamers.Vertical.Blur.Editor.1.1.0.exe`](https://github.com/SmarterGamers/SmarterGamers-Vertical-Blur/releases/download/v1.1.0/SmarterGamers.Vertical.Blur.Editor.1.1.0.exe)
  - 100% self-contained standalone executable — no installation required.

---

### 🚀 What's New in v1.1.0

1. **📦 Native Windows Drag & Drop:**
   - Drag video files (`.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`) straight from File Explorer or OBS into the app window.

2. **📱 Multi-Preset Aspect Ratios:**
   - **9:16 Shorts / TikTok** (1080 × 1920)
   - **9:16 4K Ultra HD** (2160 × 3840)
   - **9:16 720p Draft** (720 × 1280)
   - **1:1 Square Feed** (1080 × 1080) for Instagram & Discord
   - **4:5 Portrait Feed** (1080 × 1350) for Instagram Posts

3. **🛡️ Deadlock-Free Stream Pipeline:**
   - Asynchronous reader threads eliminate pipe buffer exhaustion during heavy logging or large batch runs.

4. **🔊 Robust Audio Safeguards:**
   - Detects whether source clips have audio streams. Clips without audio (muted OBS clips or converted GIFs) render seamlessly with `-an` instead of failing.

5. **🎨 10-Bit HDR & Color Normalization:**
   - Universal `yuv420p` pipeline prevents washed out colors when converting modern 10-bit HDR recordings.

6. **🎯 Queue Item Selection & Removal:**
   - Click any video in the queue to instantly load its 9:16 preview. Delete individual items with the `✕` button.
"""

def create_release():
    url = f"https://api.github.com/repos/{REPO}/releases"
    payload = {
        "tag_name": "v1.1.0",
        "target_commitish": "main",
        "name": "SmarterGamers Vertical Blur Editor v1.1.0 - Performance & Polish Update",
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
        assets_dir / "SmarterGamers.Vertical.Blur.Editor.Setup.1.1.0.exe",
        assets_dir / "SmarterGamers.Vertical.Blur.Editor.1.1.0.exe",
        assets_dir / "SHA256SUMS.txt",
    ]:
        if f.exists():
            upload_asset(upload_url, f)
        else:
            print(f"[WARN] File not found: {f}")
    
    print("\nRELEASE v1.1.0 COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
