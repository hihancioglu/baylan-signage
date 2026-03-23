param(
    [string]$Python = "python",
    [string]$ClientScript = "client/client.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanSignageAgent",
    [string]$RuntimeTmpDir = "$env:ProgramData\BaylanSignage\RuntimeTmp",
    [switch]$EnableCefCollect,
    [switch]$SkipInstallPyInstaller,
    [switch]$ForceUpgradePyInstaller
)

$ErrorActionPreference = "Stop"

if (-not $SkipInstallPyInstaller) {
    Write-Host "[1/5] Installing/upgrading pyinstaller..."
    & $Python -m pip install --upgrade pyinstaller
    $pipExitCode = $LASTEXITCODE

    if ($pipExitCode -ne 0) {
        # Some networks use SSL interception/proxies that break pip certificate checks.
        # If pyinstaller is already installed, continue with the local version.
        & $Python -m PyInstaller --version *> $null
        $hasLocalPyInstaller = ($LASTEXITCODE -eq 0)

        if ($hasLocalPyInstaller -and -not $ForceUpgradePyInstaller) {
            Write-Warning "PyInstaller upgrade failed (exit code $pipExitCode). Continuing with the installed local PyInstaller. Use -ForceUpgradePyInstaller to fail fast instead."
        } else {
            throw "PyInstaller install failed with exit code $pipExitCode"
        }
    }
} else {
    Write-Host "[1/5] Skipping pyinstaller installation step..."
}

Write-Host "[2/5] Building client executable..."
$clientScriptDir = Split-Path -Parent $ClientScript
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

if ([string]::IsNullOrWhiteSpace($RuntimeTmpDir)) {
    throw "Runtime tmp directory cannot be empty."
}

if (!(Test-Path $RuntimeTmpDir)) {
    New-Item -ItemType Directory -Path $RuntimeTmpDir -Force | Out-Null
}

Write-Host "[agent] Using fixed runtime tmp dir: $RuntimeTmpDir"

# Keep both package-qualified (client.*) and bare module names for compatibility:
# runtime imports can resolve either style depending on launch context/PYTHONPATH.
$clientPyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--onefile"
    "--noconsole"
    "--runtime-tmpdir"
    $RuntimeTmpDir
    "--name"
    $Name
    "--add-data"
    "client/widget_engine.html;client"
    "--add-data"
    "client/idle.py;client"
    "--add-data"
    "client/media_manager.py;client"
    "--add-data"
    "client/player.py;client"
    "--add-data"
    "client/state_machine.py;client"
    "--add-data"
    "client/widget_viewer.py;client"
    "--paths"
    $projectRoot
    "--paths"
    $clientScriptDir
    "--hidden-import"
    "client.idle"
    "--hidden-import"
    "client.media_manager"
    "--hidden-import"
    "client.player"
    "--hidden-import"
    "client.state_machine"
    "--hidden-import"
    "client.widget_viewer"
    "--hidden-import"
    "idle"
    "--hidden-import"
    "media_manager"
    "--hidden-import"
    "player"
    "--hidden-import"
    "state_machine"
    "--distpath"
    $OutputDir
    $ClientScript
)


if ($EnableCefCollect) {
    & $Python -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('cefpython3') else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[agent] CEF bulundu, --collect-all cefpython3 eklenecek."
        $clientPyInstallerArgs += @("--collect-all", "cefpython3")
    } else {
        Write-Warning "-EnableCefCollect verildi ancak cefpython3 bulunamadı; CEF collect adımı atlanıyor."
    }
}

& $Python -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('webview') else 1)" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[agent] pywebview bulundu, collect/hidden-import parametreleri ekleniyor."
    $clientPyInstallerArgs += @(
        "--collect-all", "webview",
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.edgechromium"
    )
} else {
    Write-Host "[agent] pywebview bulunamadı, sadece CEF/diğer backend'lerle devam edilecek."
}

& $Python -m PyInstaller @clientPyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$artifact = Join-Path $OutputDir "$Name.exe"
if (!(Test-Path $artifact)) {
    throw "Client artifact not found: $artifact"
}

Write-Host "[3/5] Viewer sidecar build adımı kaldırıldı (tek EXE mimarisi)."

$buildVersion = "build-$(Get-Date -Format 'yyyyMMddHHmmss')"
$marker = "BAYLAN_CLIENT_BUILD:$buildVersion"

Write-Host "[4/5] Embedding build marker..."
$maxAttempts = 10
$delaySeconds = 1
$markerEmbedded = $false

for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    try {
        Add-Content -Path $artifact -Value $marker -Encoding ASCII -NoNewline
        $markerEmbedded = $true
        break
    } catch {
        if ($attempt -eq $maxAttempts) {
            throw "Unable to embed build marker into '$artifact' after $maxAttempts attempts. Last error: $($_.Exception.Message)"
        }

        Write-Host "File is currently in use. Retrying in $delaySeconds second(s)... ($attempt/$maxAttempts)"
        Start-Sleep -Seconds $delaySeconds
    }
}

if (-not $markerEmbedded) {
    throw "Unable to embed build marker into '$artifact'."
}

Write-Host "[5/5] Client build completed: $artifact"
Write-Host "Embedded build marker: $buildVersion"
