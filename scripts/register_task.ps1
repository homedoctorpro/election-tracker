# Registers the "ElectionTracker Daily Refresh" task: every day at 7:00 AM local time.
# StartWhenAvailable makes it run on wake/login if the PC was off or asleep at 7.
$script = Join-Path $PSScriptRoot "refresh.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At 7:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "ElectionTracker Daily Refresh" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Pulls early-vote data and Senate/Governor/House polls" -Force
