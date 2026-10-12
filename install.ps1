# Chistovik: install on Windows.
# Run: powershell -ExecutionPolicy Bypass -File install.ps1
# Saves the API key to key.txt (unless there is one already), adds Chistovik to startup, starts it and runs a check.

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$script = Join-Path $dir "chistovik.py"

# Python 3.11+ (needed for tomllib)
$python = $null
foreach ($cmd in @(@("py", "-3"), @("python"))) {
    $exe = $cmd[0]
    $pre = @($cmd | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try { $out = & $exe @pre -c "import sys; print(sys.executable); print(int(sys.version_info >= (3, 11)))" 2>$null }
    catch { continue }
    if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and $out[1] -eq "1") { $python = $out[0]; break }
}
if (-not $python) {
    Write-Host "Python 3.11 or newer not found. Install it from https://www.python.org/downloads/ and run the installer again." -ForegroundColor Red
    exit 1
}
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "Python: $python"

# API key
$keyFile = Join-Path $dir "key.txt"
if ($env:CHISTOVIK_API_KEY) {
    Write-Host "API key: CHISTOVIK_API_KEY environment variable"
} elseif (Test-Path $keyFile) {
    Write-Host "API key: key.txt"
} else {
    $secure = Read-Host "Paste your API key (for a local model just press Enter)" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if ($key.Trim()) {
        [IO.File]::WriteAllText($keyFile, $key.Trim())
        Write-Host "Key saved to key.txt"
    }
}

# Startup
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "Chistovik.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$script`""
$shortcut.WorkingDirectory = $dir
$shortcut.Description = "Chistovik: clean Russian dictation for Handy"
$shortcut.Save()
Write-Host "Startup: $lnk"

# Start (if it is already running, the second copy exits by itself)
Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $dir
$port = 8765
$m = Select-String -Path (Join-Path $dir "config.toml") -Pattern '^\s*port\s*=\s*(\d+)' -ErrorAction SilentlyContinue
if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
$up = $false
foreach ($i in 1..10) {
    Start-Sleep -Milliseconds 500
    try { (New-Object Net.Sockets.TcpClient("localhost", $port)).Close(); $up = $true; break } catch { }
}
if (-not $up) {
    Write-Host "Chistovik did not answer on port $port. See chistovik.log in the program folder." -ForegroundColor Red
    exit 1
}
Write-Host "Chistovik is running: http://localhost:$port/health"

# Test phrase
Write-Host ""
& $python $script --check
Write-Host ""
Write-Host "Now set up Handy: Base URL = http://localhost:$port/v1, prompt = `${output}. Step by step: README.md."
