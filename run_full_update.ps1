# Full update, one step after another (one browser at a time):
#   1. waits for any scraper run already in progress
#   2. homes for rent RM 8k+ (search pages + full details for listings that don't have them yet)
#   3. any other searches not already running
#   4. condo prices (KLCC, Mont Kiara)
# Log: logs\full_update_<time>.log
Set-Location $PSScriptRoot
$log = "logs\full_update_$(Get-Date -Format yyyyMMdd_HHmm).log"
function Say($m) { "$(Get-Date -Format 'HH:mm:ss') $m" | Tee-Object -FilePath $log -Append }

Say "waiting for any scraper already running..."
while (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*scraper.py*' }) { Start-Sleep 30 }

Say "step: homes for rent RM 8k+ (with details)"
& .venv\Scripts\python.exe -u scraper.py --only kl-rent-8k *>> $log
Say "step: condo prices"
& .venv\Scripts\python.exe -u condo_summary.py *>> $log
Say "full update finished"
