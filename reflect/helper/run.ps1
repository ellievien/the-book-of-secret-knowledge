<#
  Reflect helper for Windows.

    .\run.ps1                 start Reflect (tray icon)
    .\run.ps1 -Pair           show a new pairing code for another phone
    .\run.ps1 -CaptureTest    save one frame of the Claude window to capture-test.png
    .\run.ps1 -NoTray         run without the tray icon

  First run installs Python 3.11 (for this user only) if needed, creates a
  virtual environment in .venv and installs the dependencies.
#>
param(
    [switch]$CaptureTest,
    [string]$Out = "capture-test.png",
    [switch]$PhoneMode,
    [switch]$NoTray,
    [switch]$Pair,
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Find-Python311 {
    $ErrorActionPreference = "Continue"
    if ($env:REFLECT_PYTHON -and (Test-Path -LiteralPath $env:REFLECT_PYTHON)) { return $env:REFLECT_PYTHON }
    try {
        $exe = & py -3.11 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    } catch { }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

$python = Find-Python311
if (-not $python) {
    Write-Host "Python 3.11 was not found. Installing it for your user account..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.11 --exact --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $python = Find-Python311
    }
    if (-not $python) {
        $installer = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $installer -UseBasicParsing
        Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 Include_test=0" -Wait
        $python = Find-Python311
    }
    if (-not $python) { throw "Could not install Python 3.11 automatically. Install it from https://www.python.org/downloads/release/python-3119/ and run this again." }
}

$venv = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating the virtual environment..."
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the virtual environment failed." }
}

$stamp = Join-Path $venv ".requirements.sha256"
$hash = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "requirements.txt") -Algorithm SHA256).Hash
$installed = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { "" }
if ($installed -ne $hash) {
    Write-Host "Installing dependencies (first run only)..."
    & $venvPython -m pip install --upgrade pip --disable-pip-version-check --quiet
    & $venvPython -m pip install -r requirements.txt --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "Installing dependencies failed (see the messages above)." }
    Set-Content -LiteralPath $stamp -Value $hash
}

$helperArgs = @("-m", "reflect_helper")
if ($CaptureTest) {
    $helperArgs += @("--capture-test", $Out)
    if ($PhoneMode) { $helperArgs += "--phone-mode" }
}
if ($NoTray) { $helperArgs += "--no-tray" }
if ($Pair) { $helperArgs += "--pair" }
if ($Verbose) { $helperArgs += "--verbose" }

& $venvPython @helperArgs
exit $LASTEXITCODE
