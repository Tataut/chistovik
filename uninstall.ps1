# Chistovik: stop it and remove it from startup. The program files stay.
# Run: powershell -ExecutionPolicy Bypass -File uninstall.ps1

$script = Join-Path $PSScriptRoot "chistovik.py"

$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Chistovik.lnk"
if (Test-Path $lnk) {
    Remove-Item $lnk
    Write-Host "Removed from startup"
}

$procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script) }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force
    Write-Host "Stopped process $($p.ProcessId)"
}
if (-not $procs) { Write-Host "No running Chistovik found" }

Write-Host "Done. In Handy, switch the provider back or turn post-processing off."
