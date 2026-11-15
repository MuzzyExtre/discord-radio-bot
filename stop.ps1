$ErrorActionPreference = "SilentlyContinue"

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*radio-bot*bot.py*' }

if (-not $procs) {
    Write-Host "Бот не запущен."
    return
}

foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force
}

Write-Host "Бот остановлен."
