param(
    [string]$Python = "python",
    [string]$UpdaterScript = "client/updater.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanUpdater"
)

$ErrorActionPreference = "Stop"

Write-Host "[1/3] Installing/upgrading pyinstaller..."
& $Python -m pip install --upgrade pyinstaller

Write-Host "[2/3] Building updater exe..."
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

Write-Host "[3/3] Updater build completed: $artifact"
