"""Automated Release Builder for SmarterGamers Vertical Blur Editor.

Builds:
1. Portable Standalone Executable (.exe)
2. Professional Windows Inno Setup Installer (.exe)
3. Cryptographic SHA-256 Hashes (SHA256SUMS.txt)
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
RELEASES = DIST / "releases"
VERSION = "1.0.0"

ISCC_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "iscc.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\iscc.exe"),
    Path(r"C:\Program Files\Inno Setup 6\iscc.exe"),
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()


def find_iscc() -> Path:
    for p in ISCC_PATHS:
        if p.exists():
            return p
    # Check PATH
    which_iscc = shutil.which("iscc")
    if which_iscc:
        return Path(which_iscc)
    raise FileNotFoundError("Inno Setup Compiler (iscc.exe) not found!")


def build_pyinstaller():
    print("[1/4] Compiling Standalone Executable with PyInstaller...")
    DIST.mkdir(parents=True, exist_ok=True)
    RELEASES.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", "SmarterGamersVerticalBlurEditor",
        "--icon", str(ROOT / "assets" / "app_icon.ico"),
        "--collect-all", "customtkinter",
        "--collect-all", "imageio_ffmpeg",
        "--add-data", f"{ROOT / 'assets' / 'app_icon.ico'};assets",
        "--add-data", f"{ROOT / 'assets' / 'app_icon.png'};assets",
        str(ROOT / "src" / "main.py"),
    ]

    res = subprocess.run(cmd, cwd=str(ROOT))
    if res.returncode != 0:
        print("[ERROR] PyInstaller build failed!")
        sys.exit(1)

    built_exe = DIST / "SmarterGamersVerticalBlurEditor.exe"
    if not built_exe.exists():
        print(f"[ERROR] Expected output missing: {built_exe}")
        sys.exit(1)

    # Copy to releases as portable edition
    portable_target = RELEASES / f"SmarterGamers.Vertical.Blur.Editor.{VERSION}.exe"
    shutil.copy2(built_exe, portable_target)
    print(f" -> Portable Executable: {portable_target}")


def build_inno_setup():
    print("\n[2/4] Compiling Windows Installer with Inno Setup...")
    iscc = find_iscc()
    iss_file = ROOT / "installer" / "setup.iss"

    cmd = [str(iscc), str(iss_file)]
    res = subprocess.run(cmd, cwd=str(ROOT / "installer"))
    if res.returncode != 0:
        print("[ERROR] Inno Setup compilation failed!")
        sys.exit(1)

    setup_target = RELEASES / f"SmarterGamers.Vertical.Blur.Editor.Setup.{VERSION}.exe"
    if not setup_target.exists():
        print(f"[ERROR] Expected setup installer missing: {setup_target}")
        sys.exit(1)
    print(f" -> Windows Setup Installer: {setup_target}")


def generate_checksums():
    print("\n[3/4] Computing Cryptographic SHA-256 Hashes...")
    sums_file = RELEASES / "SHA256SUMS.txt"
    lines = []

    for f in sorted(RELEASES.glob("*.exe")):
        checksum = sha256_file(f)
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  {f.name} ({size_mb:.1f} MB) -> {checksum}")
        lines.append(f"{checksum}  {f.name}")

    sums_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f" -> Saved checksums to: {sums_file}")


def main():
    print("=" * 65)
    print(f" SmarterGamers Vertical Blur Editor v{VERSION} — Release Build Pipeline")
    print("=" * 65)

    build_pyinstaller()
    build_inno_setup()
    generate_checksums()

    print("\n[4/4] Release Package Ready in dist/releases/!")


if __name__ == "__main__":
    main()
