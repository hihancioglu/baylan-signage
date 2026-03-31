param(
    [string]$Python = "python",
    [string]$UpdaterScript = "client/updater.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanUpdater",
    [string]$UpxDir = "",
    [switch]$EnableUpx
)

$ErrorActionPreference = "Stop"

if ($EnableUpx -and [string]::IsNullOrWhiteSpace($UpxDir)) {
    throw "UPX aktif edildi ancak -UpxDir boş. Örn: -EnableUpx -UpxDir C:\upx"
}

Write-Host "[1/4] Installing/upgrading pyinstaller..."
& $Python -m pip install --upgrade pyinstaller

Write-Host "[2/4] Building updater exe..."
$pyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--onefile"
    "--noupx"
    "--exclude-module", "tkinter"
    "--exclude-module", "unittest"
    "--exclude-module", "test"
    "--exclude-module", "email"
    "--exclude-module", "html"
    "--exclude-module", "http"
    "--exclude-module", "xml"
    "--exclude-module", "pydoc"
    "--name", $Name
    "--distpath", $OutputDir
    $UpdaterScript
)

if ($EnableUpx) {
    $pyInstallerArgs += @("--upx-dir", $UpxDir)
}

& $Python -m PyInstaller @pyInstallerArgs

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
