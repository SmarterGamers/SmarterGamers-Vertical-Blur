# Open a Chrome with remote debugging (127.0.0.1:9222) that is already logged in to everything.
#
#   powershell -ExecutionPolicy Bypass -File scripts\chrome_debug.ps1
#
# Chrome 136+ refuses --remote-debugging-port on its default profile folder, so this runs Chrome on its own
# folder, C:\SmarterGamers\chrome_debug (first run seeds it with a copy of the normal profile: bookmarks,
# extensions, settings). Chrome encrypts cookies per profile folder, so site logins do NOT carry over: log in
# to TikTok and Facebook once in this window and it keeps them. -Refresh re-copies the profile and LOGS
# EVERYTHING OUT again - don't use it unless the folder is broken. scripts\social_post.py posts through
# this window (and starts it if it's closed).
param([switch]$Refresh, [int]$Port = 9222)
$ErrorActionPreference = "Stop"
$chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw "Google Chrome not found" }
$src = "$env:LOCALAPPDATA\Google\Chrome\User Data"
$dst = "C:\SmarterGamers\chrome_debug"

function DebugUp { try { Invoke-RestMethod "http://127.0.0.1:$Port/json/version" -TimeoutSec 2 | Out-Null; $true } catch { $false } }
if ((DebugUp) -and -not $Refresh) { "debug Chrome already running on port $Port"; exit 0 }

if ($Refresh -or -not (Test-Path "$dst\Default")) {
    # close debug Chrome (if any) and normal Chrome gracefully, copy, reopen normal Chrome
    $wasOpen = [bool](Get-Process chrome -ErrorAction SilentlyContinue)
    if ($wasOpen) {
        "closing Chrome for a moment to copy the logged-in profile..."
        cmd /c "taskkill /IM chrome.exe >nul 2>&1"
        for ($i = 0; $i -lt 20 -and (Get-Process chrome -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
        if (Get-Process chrome -ErrorAction SilentlyContinue) { cmd /c "taskkill /F /IM chrome.exe >nul 2>&1"; Start-Sleep 2 }
    }
    New-Item -ItemType Directory -Force $dst | Out-Null
    Copy-Item "$src\Local State" "$dst\Local State" -Force
    $skip = @("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker\CacheStorage",
              "Service Worker\ScriptCache", "optimization_guide_hint_cache_store", "Shared Dictionary")
    robocopy "$src\Default" "$dst\Default" /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD $skip | Out-Null
    "profile copied to $dst"
    if ($wasOpen) { Start-Process $chrome -ArgumentList "--restore-last-session"; "normal Chrome reopened" }
    Start-Sleep 3
}

Start-Process $chrome -ArgumentList "--remote-debugging-port=$Port", "--user-data-dir=$dst", "--profile-directory=Default",
    "--no-first-run", "--no-default-browser-check", "https://www.tiktok.com/tiktokstudio/upload", "https://www.facebook.com/reels/create"
for ($i = 0; $i -lt 20 -and -not (DebugUp); $i++) { Start-Sleep 1 }
if (DebugUp) { "debug Chrome running: http://127.0.0.1:$Port" } else { throw "debug Chrome did not open port $Port" }
