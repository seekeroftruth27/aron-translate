# Starts Aron by itself before each service: one Windows scheduled task with a
# trigger per service time (Sunday 9:10 and 17:40, Tuesday 18:40 by default).
# Each run opens church-translate.exe with --autostart: the page turns Web
# captions on and starts listening, as if the operator had pressed Start.
#
# Run once on the streaming computer, signed in as the operator (no admin needed):
#   powershell -ExecutionPolicy Bypass -File aron-schedule.ps1
# Options:  -AronDir <folder with church-translate.exe>   -Times 'Sunday 09:10','Sunday 17:40','Tuesday 18:40'
# To remove: powershell -ExecutionPolicy Bypass -File aron-schedule.ps1 -Remove
# Details: docs/scheduled-start.md
param(
    [string]$AronDir = (Join-Path $env:USERPROFILE 'Documents\aron-public'),
    [string[]]$Times = @('Sunday 09:10', 'Sunday 17:40', 'Tuesday 18:40'),
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$TaskName = 'Aron - start before services'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the scheduled task '$TaskName'."
    exit 0
}

if (-not (Test-Path $AronDir)) { throw "Folder not found: $AronDir  (pass -AronDir with the folder holding church-translate.exe)" }
$exe = Get-ChildItem $AronDir -Filter 'church-translate*.exe' -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $exe) { throw "No church-translate*.exe under $AronDir" }

# The task runs this small starter, which picks the newest exe each time (so an
# update needs no new task) and does nothing when Aron is already running (the
# operator started it by hand: a second copy would listen twice).
$starter = Join-Path $AronDir 'start-aron-scheduled.ps1'
@'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Get-Process -Name 'church-translate*' -ErrorAction SilentlyContinue) { exit 0 }
$exe = Get-ChildItem $dir -Filter 'church-translate*.exe' -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $exe) { exit 1 }
Start-Process -FilePath $exe.FullName -ArgumentList '--autostart' -WorkingDirectory $exe.DirectoryName
'@ | Set-Content -Path $starter -Encoding UTF8

$triggers = foreach ($t in $Times) {
    $day, $clock = $t -split '\s+', 2
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek $day -At ([datetime]::ParseExact($clock, 'HH:mm', $null))
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$starter`""
# Interactive: it runs in the operator's session, so the browser page (and its
# microphone permission) is the one the operator normally uses.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
    -Description 'Starts Aron (church-translate.exe --autostart) before each service: Web captions on, listening.' -Force | Out-Null

Write-Host "Scheduled '$TaskName' for: $($Times -join ', ')"
Write-Host "Aron: $($exe.FullName)"
Write-Host "Starter: $starter"
Write-Host "Try it now: Start-ScheduledTask -TaskName '$TaskName'"
