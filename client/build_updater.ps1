param(
    [string]$Python = "python",
    [string]$UpdaterScript = "client/updater.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanUpdater"
)

$ErrorActionPreference = "Stop"

Write-Host "[1/4] Installing/upgrading pyinstaller..."
& $Python -m pip install --upgrade pyinstaller

Write-Host "[2/4] Building updater exe..."
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name $Name `
    --distpath $OutputDir `
    $UpdaterScript

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$artifact = Join-Path $OutputDir "$Name.exe"
if (!(Test-Path $artifact)) {
    throw "Updater artifact not found: $artifact"
}

$buildVersion = "build-$(Get-Date -Format 'yyyyMMddHHmmss')"
$marker = "BAYLAN_UPDATER_BUILD:$buildVersion"

Write-Host "[3/4] Embedding updater build marker..."
Add-Content -Path $artifact -Value $marker -Encoding ASCII -NoNewline

Write-Host "[4/4] Updater build completed: $artifact"
Write-Host "Embedded updater build marker: $buildVersion"
