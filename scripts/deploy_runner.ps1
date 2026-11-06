# Deploy the clipper runner without touching Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_runner.ps1 [-NoPull]
#
# 1. POST /restart {"pull": true} -> the runner git-pulls the repo and exits non-zero; the scheduled task
#    ("SmarterGamers Clipper Runner", restart-on-failure every minute) relaunches it under the same account.
# 2. Polls GET /health until the new process is up and reports the expected git head.
# Works from a normal (non-elevated) shell. No secrets involved.
param(
    [string]$RunnerUrl = "http://127.0.0.1:5690",
    [switch]$NoPull,
    [int]$TimeoutSec = 180
)
$ErrorActionPreference = "Stop"
$before = Invoke-RestMethod "$RunnerUrl/health" -TimeoutSec 10
"runner before: head=$($before.head) started=$($before.started)"
$body = (@{ pull = (-not $NoPull) } | ConvertTo-Json -Compress)
$r = Invoke-RestMethod -Method Post -Uri "$RunnerUrl/restart" -ContentType application/json -Body $body -TimeoutSec 150
"restart accepted: head=$($r.head) pulled=[$($r.pulled)]"
$deadline = (Get-Date).AddSeconds($TimeoutSec)
Start-Sleep 5
while ((Get-Date) -lt $deadline) {
    try {
        $h = Invoke-RestMethod "$RunnerUrl/health" -TimeoutSec 5
        if ($h.started -ne $before.started) { "runner back: head=$($h.head) started=$($h.started)"; exit 0 }
    } catch { }
    Start-Sleep 5
}
Write-Error "runner did not come back within $TimeoutSec s - check Task Scheduler ('SmarterGamers Clipper Runner')"
exit 1
