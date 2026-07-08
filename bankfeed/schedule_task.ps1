<#
schedule_task.ps1 — register a Windows Task Scheduler job that pulls a bank's
statement into Zoho every N hours (the "near real-time" driver for bankfeed).

Run in PowerShell from the metamorphosis/ folder, e.g.:
    powershell -ExecutionPolicy Bypass -File bankfeed\schedule_task.ps1 -Bank hdfc -IntervalHours 2
    # dry-run task (no writes), every 4h:
    powershell -ExecutionPolicy Bypass -File bankfeed\schedule_task.ps1 -Bank hdfc -IntervalHours 4 -DryRun

Remove a task:  Unregister-ScheduledTask -TaskName "bankfeed-hdfc" -Confirm:$false
List runs:      Get-ScheduledTask -TaskName "bankfeed-*"
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("hdfc","axis","boi")][string]$Bank,
  [int]$IntervalHours = 2,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# metamorphosis/ is this script's parent folder — run the module from there.
$WorkDir = Split-Path -Parent $PSScriptRoot
$Python  = (Get-Command python).Source
$LiveFlag = if ($DryRun) { "" } else { "--live" }
$Arguments = "-m bankfeed.run_feed --bank $Bank $LiveFlag"
$TaskName  = "bankfeed-$Bank"

Write-Host "Registering '$TaskName':" -ForegroundColor Cyan
Write-Host "  $Python $Arguments"
Write-Host "  start-in: $WorkDir  |  every $IntervalHours h  |  mode: $(if($DryRun){'DRY-RUN'}else{'LIVE'})"

$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $WorkDir
# repeat every N hours, indefinitely, starting 1 minute from now
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "  (replaced existing task)" -ForegroundColor Yellow
}

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
  -Settings $Settings -Description "Pull $Bank statement into Zoho Books (bankfeed)" | Out-Null

Write-Host "Done. First run in ~1 min, then every $IntervalHours h." -ForegroundColor Green
Write-Host "Check state: type bankfeed_state.json  |  Remove: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
