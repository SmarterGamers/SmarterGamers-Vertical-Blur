<#
.SYNOPSIS
  Pin the Personal Clipper's compute (Ollama + faster-whisper) to a secondary GPU, leaving the primary
  GPU for OBS/NVENC and the stream. Idempotent; safe to run before the second card is installed (it just
  reports "no secondary GPU" and changes nothing).

.DESCRIPTION
  Run in an ELEVATED PowerShell after the RTX 3060 is installed:
      powershell -ExecutionPolicy Bypass -File scripts\pin_gpus.ps1
  What it does:
    1. Lists GPUs via nvidia-smi and picks the secondary = the one that is NOT the display/primary
       (default: the non-4060; override with -SecondaryName "RTX 3060").
    2. Ollama: sets the SYSTEM env var CUDA_VISIBLE_DEVICES=<secondary UUID> *for the Ollama process only*
       by writing a small launcher (C:\SmarterGamers\ollama_on_secondary.cmd) and pointing the
       "Ollama" startup entry at it. Using the UUID, not an index, so CUDA re-ordering can never move it.
    3. Whisper: sets SG_CLIPPER_WHISPER_DEVICE=cuda:<index> in the "SmarterGamers Clipper Runner"
       scheduled task's environment (via a wrapper .cmd), where <index> is the secondary's CUDA index.
    4. Prints what to restart.
  Nothing here touches OBS, the Vertical Blur render path (NVENC stays on the primary) or n8n.
  Revert: run with -Undo.
#>
[CmdletBinding()]
param(
    [string]$SecondaryName = "",
    [switch]$Undo
)
$ErrorActionPreference = "Stop"
$root = "C:\SmarterGamers"
$launcher = Join-Path $root "ollama_on_secondary.cmd"
$runnerWrap = Join-Path $root "run_runner_pinned.cmd"
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Ollama (secondary GPU).lnk"

function Get-Gpus {
    $rows = & nvidia-smi --query-gpu=index,name,uuid,memory.total --format=csv,noheader 2>$null
    if (-not $rows) { return @() }
    $rows | ForEach-Object {
        $p = $_ -split ",\s*"
        [pscustomobject]@{ Index = [int]$p[0]; Name = $p[1]; Uuid = $p[2]; Mem = $p[3] }
    }
}

if ($Undo) {
    Remove-Item $launcher, $runnerWrap, $startup -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable("SG_CLIPPER_WHISPER_DEVICE", $null, "Machine")
    Write-Host "Undone. Restart Ollama and the 'SmarterGamers Clipper Runner' task to go back to the default GPU."
    exit 0
}

$gpus = Get-Gpus
Write-Host "GPUs:"; $gpus | Format-Table -AutoSize | Out-String | Write-Host
if ($gpus.Count -lt 2) {
    Write-Host "No secondary GPU present - nothing changed. Re-run after installing the second card." -ForegroundColor Yellow
    exit 0
}
$secondary = if ($SecondaryName) { $gpus | Where-Object Name -match $SecondaryName | Select-Object -First 1 }
             else { $gpus | Where-Object Name -notmatch "4060" | Select-Object -First 1 }
if (-not $secondary) { throw "Could not identify the secondary GPU. Pass -SecondaryName." }
Write-Host "Secondary GPU: [$($secondary.Index)] $($secondary.Name) $($secondary.Uuid)" -ForegroundColor Green

# --- Ollama launcher: process-scoped CUDA_VISIBLE_DEVICES so OBS/Whisper/ffmpeg are unaffected
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
@"
@echo off
rem Starts Ollama pinned to the secondary GPU (set by scripts\pin_gpus.ps1). Edit the UUID if the card changes.
set CUDA_VISIBLE_DEVICES=$($secondary.Uuid)
set OLLAMA_HOST=127.0.0.1:11434
start "" /min "$ollama" serve
"@ | Set-Content -Path $launcher -Encoding ASCII
# Startup shortcut (per-user) so it comes back at logon; the stock "Ollama" tray autostart should be turned off
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($startup); $lnk.TargetPath = $launcher; $lnk.WindowStyle = 7; $lnk.Save()
Write-Host "Ollama launcher: $launcher (+ Startup shortcut)."

# --- Whisper: machine-wide env var read by src\clipper\config.py (SG_CLIPPER_* overrides)
[Environment]::SetEnvironmentVariable("SG_CLIPPER_WHISPER_DEVICE", "cuda:$($secondary.Index)", "Machine")
Remove-Item "$root\Clips\.whisper_device" -ErrorAction SilentlyContinue   # drop the cached device choice
Write-Host "Whisper: SG_CLIPPER_WHISPER_DEVICE=cuda:$($secondary.Index) (machine env)."

Write-Host ""
Write-Host "Now:" -ForegroundColor Cyan
Write-Host "  1. Quit the Ollama tray app, turn off its 'Start at login' option, then run: $launcher"
Write-Host "  2. Restart the runner: Stop-ScheduledTask -TaskName 'SmarterGamers Clipper Runner'; Start-ScheduledTask -TaskName 'SmarterGamers Clipper Runner'"
Write-Host "  3. Verify: nvidia-smi  (ollama.exe should appear only on GPU $($secondary.Index)); then drop a VOD in Inbox and check runner.log says device cuda:$($secondary.Index)."
