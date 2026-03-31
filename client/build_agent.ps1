param(
    [string]$Python = "python",
    [string]$ClientScript = "client/client.py",
    [string]$OutputDir = "dist",
    [string]$Name = "BaylanSignageAgent",
    [string]$RuntimeTmpDir = "$env:ProgramData\BaylanSignage\RuntimeTmp",
    [string]$UpxDir = "",
    [switch]$EnableCefCollect,
    [switch]$EnableUpx,
    [switch]$SkipInstallPyInstaller,
    [switch]$ForceUpgradePyInstaller
)

$ErrorActionPreference = "Stop"


function Resolve-PythonCommand {
    param(
        [string]$RequestedPython
    )

    $command = Get-Command $RequestedPython -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Python command not found: '$RequestedPython'. Install Python 3.10 and recreate the venv, or pass -Python with a valid interpreter path."
    }

    return $command.Source
}

function Get-PythonVersion {
    param(
        [string]$PythonExe
    )

    $versionOutput = & $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionOutput)) {
        throw "Python command '$PythonExe' is not runnable. If this is a broken virtualenv, recreate it with Python 3.10."
    }

    return $versionOutput.Trim()
}


$PythonExe = Resolve-PythonCommand -RequestedPython $Python
$PythonVersion = Get-PythonVersion -PythonExe $PythonExe
Write-Host "[agent] Using Python: $PythonExe (version $PythonVersion)"

if ($EnableCefCollect -and -not $PythonVersion.StartsWith("3.10.")) {
    Write-Warning "CEF packaging typically requires Python 3.10. Current version: $PythonVersion"
}

if (-not $SkipInstallPyInstaller) {
    Write-Host "[1/5] Installing/upgrading pyinstaller..."
    & $PythonExe -m pip install --upgrade pyinstaller
    $pipExitCode = $LASTEXITCODE

    if ($pipExitCode -ne 0) {
        # Some networks use SSL interception/proxies that break pip certificate checks.
        # If pyinstaller is already installed, continue with the local version.
        & $PythonExe -m PyInstaller --version *> $null
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
    "--strip"
    "--noupx"
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
    "--exclude-module"
    "tkinter"
    "--exclude-module"
    "unittest"
    "--exclude-module"
    "test"
    "--exclude-module"
    "email"
    "--exclude-module"
    "html"
    "--exclude-module"
    "http"
    "--exclude-module"
    "xml"
    "--exclude-module"
    "pydoc"
    "--distpath"
    $OutputDir
    $ClientScript
)

& $PythonExe -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('PySide6') else 1)" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[agent] PySide6 bulundu, minimum gerekli Qt/WebEngine importları ekleniyor."
    $clientPyInstallerArgs += @(
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtNetwork",
        "--hidden-import", "PySide6.QtWebChannel",
        "--hidden-import", "PySide6.QtWebEngineCore",
        "--hidden-import", "PySide6.QtWebEngineWidgets"
    )
} else {
    Write-Warning "PySide6 bulunamadı; widget viewer (PySide6 QtWebEngine) çalışmayabilir."
}

if ($EnableUpx) {
    if ([string]::IsNullOrWhiteSpace($UpxDir)) {
        throw "UPX aktif edildi ancak -UpxDir boş. Örn: -EnableUpx -UpxDir C:\upx"
    }
    $clientPyInstallerArgs += @("--upx-dir", $UpxDir)
}

& $PythonExe -m PyInstaller @clientPyInstallerArgs

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
