# Re-run the KL commercial / sale searches once the current full update (run_full_update.ps1) has finished.
Set-Location $PSScriptRoot
$log = "logs\full_update_part2_$(Get-Date -Format yyyyMMdd_HHmm).log"
function Say($m) { "$(Get-Date -Format 'HH:mm:ss') $m" | Tee-Object -FilePath $log -Append }
Say "waiting for the current full update to finish..."
while (Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like '*run_full_update.ps1*' -or $_.CommandLine -like '*scraper.py*' -or
        $_.CommandLine -like '*condo_summary.py*' }) { Start-Sleep 30 }
Say "step: KL commercial for rent RM8k+, homes for sale RM3M+, commercial for sale RM3M+"
& .venv\Scripts\python.exe -u scraper.py --only kl-commercial-rent-8k,kl-sale-3m,kl-commercial-sale-3m *>> $log
Say "finished"
