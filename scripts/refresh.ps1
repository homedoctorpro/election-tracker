# Daily refresh entry point (called by Task Scheduler). Logs to logs\refresh-YYYY-MM-DD.log
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("refresh-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

Set-Location $root
& "C:\Users\pross\AppData\Local\Programs\Python\Python312\python.exe" -m tracker.run 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
exit $LASTEXITCODE
