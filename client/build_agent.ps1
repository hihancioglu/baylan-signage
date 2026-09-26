param(
    [string]$Python = "python",
    [string]$ClientScript = "client/client.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanSignageAgent",
    [string]$RuntimeTmpDir = "$env:ProgramData\BaylanSignage\RuntimeTmp",
    [switch]$SkipInstallPyInstaller,
    [switch]$ForceUpgradePyInstaller,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not $SkipTests) {
    Write-Host "[1/6] Running test suite..."

    Push-Location $projectRoot
    try {
        & $Python -m unittest discover `
            -s tests `
            -p "test_*.py" `
            -v

        if ($LASTEXITCODE -ne 0) {
            throw "Test suite failed with exit code $LASTEXITCODE. Build aborted."
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "[tests] All tests passed."
} else {
    Write-Host "[1/6] Skipping tests..."
}

if (-not $SkipInstallPyInstaller) {
    Write-Host "[2/6] Installing/upgrading pyinstaller..."
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
    Write-Host "[2/6] Skipping pyinstaller installation step..."
}

Write-Host "[3/6] Building client executable..."
$clientScriptDir = Split-Path -Parent $ClientScript
$artifact = Join-Path $OutputDir "$Name.exe"

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
    "--hidden-import"
    "websocket"
    "--hidden-import"
    "psutil"
    "--distpath"
    $OutputDir
    $ClientScript
)

& $Python -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('webview') else 1)" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[agent] pywebview bulundu, collect/hidden-import parametreleri ekleniyor."
    $clientPyInstallerArgs += @(
        "--collect-all", "webview",
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.edgechromium"
    )
} else {
    Write-Host "[agent] pywebview bulunamadı, widget viewer backend'i devre dışı kalacak."
}

if (Test-Path $artifact) {
    Write-Host "[build] Removing previous artifact: $artifact"
    Remove-Item $artifact -Force
}

& $Python -m PyInstaller @clientPyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

if (!(Test-Path $artifact)) {
    throw "Client artifact not found: $artifact"
}

Write-Host "[4/6] Viewer sidecar build adımı kaldırıldı (tek EXE mimarisi)."

$buildVersion = "build-$(Get-Date -Format 'yyyyMMddHHmmss')"
$marker = "BAYLAN_CLIENT_BUILD:$buildVersion"

Write-Host "[5/6] Embedding build marker..."
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

Write-Host "[6/6] Client build completed: $artifact"
Write-Host "Embedded build marker: $buildVersion"
