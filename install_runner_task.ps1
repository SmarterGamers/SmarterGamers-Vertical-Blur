<#
.SYNOPSIS
  Registers the "SmarterGamers Clipper Runner" as a hidden scheduled task that starts at logon.

.DESCRIPTION
  Runs src\clipper\runner.py with C:\SmarterGamers\clipper-venv314\Scripts\python.exe (system
  Python 3.14; the miniconda 3.11 venv crashes on ctranslate2/onnxruntime import) as an S4U
  task (no stored password, no visible window), restarting on failure. Re-running the script
  replaces the existing task. Run from an elevated PowerShell:

      powershell -ExecutionPolicy Bypass -File .\install_runner_task.ps1
      powershell -ExecutionPolicy Bypass -File .\install_runner_task.ps1 -Uninstall
#>
param(
    [string]$PythonExe = "C:\SmarterGamers\clipper-venv314\Scripts\python.exe",
    [string]$TaskName  = "SmarterGamers Clipper Runner",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repoDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$runnerPy = Join-Path $repoDir "src\clipper\runner.py"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[runner] task '$TaskName' removed"
    } else {
        Write-Host "[runner] task '$TaskName' not present"
    }
    exit 0
}

if (-not (Test-Path $PythonExe)) { throw "venv python not found: $PythonExe (py -3.14 -m venv C:\SmarterGamers\clipper-venv314)" }
if (-not (Test-Path $runnerPy)) { throw "runner not found: $runnerPy" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action    = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$runnerPy`"" -WorkingDirectory $repoDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Personal Clipper HTTP runner for n8n (127.0.0.1:5690)" | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "[runner] task '$TaskName' registered (S4U, hidden, at logon) and started"
Write-Host "[runner] check: Invoke-RestMethod http://127.0.0.1:5690/health"
