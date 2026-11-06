# Deploy the clipper runner without touching Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\deploy_runner.ps1 [-NoPull]
#
# 1. POST /restart {"pull": true} -> the runner git-pulls the repo and exits.
# 2. Start-ScheduledTask "SmarterGamers Clipper Runner" relaunches it under the task's account (allowed from a
#    normal, non-elevated shell of the same user; Task Scheduler's own restart-on-failure only covers
#    trigger-started runs, so we start it explicitly).
# 3. Polls GET /health until the new process is up.
# No secrets involved.
param(
    [string]$RunnerUrl = "http://127.0.0.1:5690",
    [string]$TaskName = "SmarterGamers Clipper Runner",
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
Start-Sleep 4
$kicked = $false
while ((Get-Date) -lt $deadline) {
    try {
        $h = Invoke-RestMethod "$RunnerUrl/health" -TimeoutSec 5
        if ($h.started -ne $before.started) { "runner back: head=$($h.head) started=$($h.started)"; exit 0 }
    } catch {
        if (-not $kicked) {
            try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop; "scheduled task started"; $kicked = $true }
            catch { "Start-ScheduledTask failed: $($_.Exception.Message) (Task Scheduler may still relaunch it)" ; $kicked = $true }
        }
    }
    Start-Sleep 4
}
Write-Error "runner did not come back within $TimeoutSec s - check Task Scheduler ('SmarterGamers Clipper Runner')"
exit 1
