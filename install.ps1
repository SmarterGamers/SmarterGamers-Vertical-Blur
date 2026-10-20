<#
.SYNOPSIS
    SmarterGamers Vertical Blur Editor - Automated 1-Line Windows Installer
.DESCRIPTION
    Downloads the verified installer or portable release directly from GitHub
    and launches it.
#>

$ErrorActionPreference = "Stop"

$repo = "SmarterGamers/SmarterGamers-Vertical-Blur"
$apiUrl = "https://api.github.com/repos/$repo/releases/latest"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "       SmarterGamers Vertical Blur Editor Installer         " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""

try {
    Write-Host "[*] Fetching latest release info from GitHub..." -ForegroundColor Yellow
    $release = Invoke-RestMethod -Uri $apiUrl -Headers @{ "User-Agent" = "SmarterGamers-Installer" }
    $tag = $release.tag_name
    Write-Host "[+] Found latest version: $tag" -ForegroundColor Green

    # Find setup executable asset
    $setupAsset = $release.assets | Where-Object { $_.name -like "*Setup*.exe" } | Select-Object -First 1
    if (-not $setupAsset) {
        $setupAsset = $release.assets | Where-Object { $_.name -like "*.exe" } | Select-Object -First 1
    }

    if (-not $setupAsset) {
        throw "Could not locate installer executable in release assets."
    }

    $downloadUrl = $setupAsset.browser_download_url
    $tempInstaller = Join-Path $env:TEMP $setupAsset.name

    Write-Host "[*] Downloading $($setupAsset.name)..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $downloadUrl -OutFile $tempInstaller

    Write-Host "[+] Download complete. Launching installer..." -ForegroundColor Green
    Start-Process -FilePath $tempInstaller

    Write-Host ""
    Write-Host "[SUCCESS] SmarterGamers Vertical Blur Editor installer is running!" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Green
}
catch {
    Write-Host "[ERROR] Installation failed: $_" -ForegroundColor Red
    Write-Host "You can manually download the installer from: https://github.com/$repo/releases" -ForegroundColor Gray
}
