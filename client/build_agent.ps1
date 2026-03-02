param(
    [string]$Python = "python",
    [string]$ClientScript = "client/client.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanSignageAgent",
    [switch]$SkipInstallPyInstaller
)

$ErrorActionPreference = "Stop"

if (-not $SkipInstallPyInstaller) {
    Write-Host "[1/4] Installing/upgrading pyinstaller..."
    & $Python -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller install failed with exit code $LASTEXITCODE"
    }
} else {
    Write-Host "[1/4] Skipping pyinstaller installation step..."
}

Write-Host "[2/4] Building client executable..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name $Name `
    --distpath $OutputDir `
    $ClientScript

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$artifact = Join-Path $OutputDir "$Name.exe"
if (!(Test-Path $artifact)) {
    throw "Client artifact not found: $artifact"
}

$buildVersion = "build-$(Get-Date -Format 'yyyyMMddHHmmss')"
$marker = "BAYLAN_CLIENT_BUILD:$buildVersion"

Write-Host "[3/4] Embedding build marker..."
Add-Content -Path $artifact -Value $marker -Encoding ASCII -NoNewline

Write-Host "[4/4] Client build completed: $artifact"
Write-Host "Embedded build marker: $buildVersion"
