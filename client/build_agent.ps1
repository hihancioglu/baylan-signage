param(
    [string]$Python = "python",
    [string]$ClientScript = "client/client.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanSignageAgent",
    [switch]$SkipViewerBuild,
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

# Keep both package-qualified (client.*) and bare module names for compatibility:
# runtime imports can resolve either style depending on launch context/PYTHONPATH.
$clientPyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--onefile"
    "--noconsole"
    "--name"
    $Name
    "--add-data"
    "client/widget_engine.html;client"
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
    "--hidden-import"
    "widget_viewer"
    "--distpath"
    $OutputDir
    $ClientScript
)

& $Python -m PyInstaller @clientPyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$artifact = Join-Path $OutputDir "$Name.exe"
if (!(Test-Path $artifact)) {
    throw "Client artifact not found: $artifact"
}

if (-not $SkipViewerBuild) {
    Write-Host "[3/5] Building widget viewer sidecar executable..."

    $viewerPyInstallerArgs = @(
        "--noconfirm"
        "--clean"
        "--onefile"
        "--noconsole"
        "--name"
        "widget_viewer"
        "--add-data"
        "client/widget_engine.html;."
        "--distpath"
        $OutputDir
    )

    if ($EnableCefCollect) {
        & $Python -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('cefpython3') else 1)" *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[viewer] CEF bulundu, --collect-all cefpython3 eklenecek."
            $viewerPyInstallerArgs += @("--collect-all", "cefpython3")
        } else {
            Write-Warning "-EnableCefCollect verildi ancak cefpython3 bulunamadı; CEF collect adımı atlanıyor."
        }
    }

    & $Python -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('webview') else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[viewer] pywebview bulundu, backend fallback için collect/hidden-import parametreleri ekleniyor."
        $viewerPyInstallerArgs += @(
            "--collect-all", "webview",
            "--hidden-import", "webview.platforms.winforms",
            "--hidden-import", "webview.platforms.edgechromium"
        )
    } else {
        Write-Host "[viewer] pywebview bulunamadı, sadece CEF/diğer backend'lerle devam edilecek."
    }

    $viewerPyInstallerArgs += "client/widget_viewer.py"

    & $Python -m PyInstaller @viewerPyInstallerArgs

    if ($LASTEXITCODE -ne 0) {
        throw "Widget viewer build failed with exit code $LASTEXITCODE"
    }
} else {
    Write-Host "[3/5] Skipping viewer sidecar executable builds..."
}

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
if (-not $SkipViewerBuild) {
    Write-Host "Viewer artifact: $(Join-Path $OutputDir 'widget_viewer.exe')"
}
Write-Host "Embedded build marker: $buildVersion"
