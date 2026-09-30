# CI only: install and start the real Claude desktop app on a Windows runner.
$ErrorActionPreference = "Continue"

function Start-ClaudeIfInstalled {
    $paths = @("$env:LOCALAPPDATA\AnthropicClaude\claude.exe") +
        @(Get-ChildItem "$env:LOCALAPPDATA\AnthropicClaude\app-*\claude.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName)
    foreach ($p in $paths) {
        if (Test-Path $p) { Write-Host "Starting $p"; Start-Process $p; return $true }
    }
    $pkg = Get-AppxPackage -Name "*Claude*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pkg) {
        Write-Host "Starting packaged app $($pkg.PackageFamilyName)"
        Start-Process "explorer.exe" "shell:AppsFolder\$($pkg.PackageFamilyName)!Claude"
        return $true
    }
    return $false
}

$installed = $false
if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host "Installing Claude with winget..."
    winget install --id Anthropic.Claude --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    $installed = ($LASTEXITCODE -eq 0)
}
if (-not $installed) {
    Write-Host "Installing Claude from the direct download..."
    $setup = Join-Path $env:TEMP "Claude-Setup-x64.exe"
    Invoke-WebRequest "https://storage.googleapis.com/osprey-downloads-c02f6a0d-347c-492b-a752-3e0651722e97/nest-win-x64/Claude-Setup-x64.exe" -OutFile $setup -UseBasicParsing
    Start-Process $setup -ArgumentList "--silent" -Wait
}

Start-Sleep -Seconds 10
if (-not (Get-Process -Name "claude" -ErrorAction SilentlyContinue)) {
    if (-not (Start-ClaudeIfInstalled)) { throw "Claude desktop could not be found after installation" }
}
exit 0
