<#
.SYNOPSIS
  Removes stale (not-present) NVIDIA display-adapter entries left behind by cards that are no longer
  installed — e.g. the ghost "GeForce GTX 1650" that still shows under Display adapters on this box.
  Harmless but confuses GPU-index tooling. Run ELEVATED. Never touches a present/working device.
#>
$ErrorActionPreference = "Stop"
$ghosts = Get-PnpDevice -Class Display | Where-Object { $_.Status -ne "OK" -and $_.FriendlyName -match "NVIDIA" }
if (-not $ghosts) { Write-Host "No ghost NVIDIA display adapters found."; exit 0 }
foreach ($g in $ghosts) {
    Write-Host "Removing ghost device: $($g.FriendlyName)  [$($g.InstanceId)]"
    & pnputil /remove-device "$($g.InstanceId)" | Out-String | Write-Host
}
Write-Host "Done. Remaining display adapters:"; Get-PnpDevice -Class Display | Select-Object Status, FriendlyName | Format-Table -AutoSize
