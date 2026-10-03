<#
.SYNOPSIS
    Build Tuning Buddy for Windows: TuningBuddySetup.exe and TuningBuddyUninstall.exe (next to this
    script) and the app folder dist\TuningBuddy\.

    Both exes are the custom setup window (setup_ui\). The setup carries the install engine
    (installer\TuningBuddy.iss, compiled to build\engine\) and runs it silently; the uninstaller is
    also shipped inside the app, where Settings > Apps opens it.

.PARAMETER PgHome
    PostgreSQL 16 install with PostGIS and pgvector, copied (trimmed) for the installer's optional
    demo database.

.PARAMETER SignCertThumbprint
    Thumbprint of a code-signing certificate in your certificate store (Cert:\CurrentUser\My).
    When given, every exe is signed (SHA-256, timestamped) before the next step packs it, so Windows
    SmartScreen and antivirus can see the publisher. Without it nothing is signed.

.PARAMETER NoDemoDatabase
    Build an installer without the demo database option (no PostgreSQL needed on this machine).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File desktop-apps\build.ps1
    powershell -ExecutionPolicy Bypass -File desktop-apps\build.ps1 -SkipInstaller
#>
param(
    [string]$Version = "1.3.7",
    [string]$PgHome = "$env:ProgramFiles\PostgreSQL\16",
    [switch]$NoDemoDatabase,
    [switch]$SkipInstaller,
    [string]$SignCertThumbprint = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$BuildDir = Join-Path $Root "build"
$Venv = Join-Path $BuildDir "venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Stage = Join-Path $BuildDir "stage"

function Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }

# Native tools write progress to stderr, which Windows PowerShell turns into errors under "Stop";
# judge them by exit code instead
function Invoke-Native([string]$What, [scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}

# Code signing: optional, see -SignCertThumbprint
$SignTool = $null
if ($SignCertThumbprint) {
    $SignTool = Find-SignTool
    if (-not $SignTool) { throw "signtool.exe was not found. Install the Windows SDK (Signing Tools for Desktop Apps)." }
}
function Sign-Exe([string]$Path) {
    if (-not $SignTool) { return }
    Invoke-Native "Signing $(Split-Path $Path -Leaf)" {
        & $SignTool sign /sha1 $SignCertThumbprint /fd sha256 /tr "http://timestamp.digicert.com" /td sha256 $Path
    }
}

function Find-InnoCompiler {
    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    return $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

# Fail before the slow steps if a prerequisite is missing
$Iscc = $null
if (-not $SkipInstaller) {
    $Iscc = Find-InnoCompiler
    if (-not $Iscc) {
        throw "Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php, or pass -SkipInstaller to build only the app folder."
    }
}

if (-not $SkipInstaller -and -not $NoDemoDatabase -and -not (Test-Path (Join-Path $PgHome "bin\postgres.exe"))) {
    throw "No PostgreSQL at $PgHome for the demo database. Pass -PgHome <folder>, or -NoDemoDatabase to build without it."
}

if (-not (Test-Path $Python)) {
    Step "Creating the build environment (Python 3.11)"
    # Django 4.2 does not run on newer Pythons, so the build is pinned to 3.11
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & py -3.11 -c "import sys" 2>$null
    $hasPython311 = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $previous
    if (-not $hasPython311) { throw "Python 3.11 is required. Install it from https://www.python.org/downloads/ (the py launcher must list it: py -0)." }
    New-Item -ItemType Directory -Force $BuildDir | Out-Null
    Invoke-Native "Creating the venv" { py -3.11 -m venv $Venv }
}

Step "Installing dependencies"
Invoke-Native "pip upgrade" { & $Python -m pip install --quiet --upgrade pip }
Invoke-Native "pip install" { & $Python -m pip install --quiet -r (Join-Path $Root "requirements-desktop.txt") }

Step "Staging the services"
Invoke-Native "Staging" { & $Python (Join-Path $Root "stage.py") }

Step "Collecting static files"
Push-Location $Stage
try {
    $env:SECRET_KEY = "collectstatic-only"
    Invoke-Native "collectstatic" { & $Python manage.py collectstatic --noinput --verbosity 0 }
} finally {
    Remove-Item Env:SECRET_KEY -ErrorAction SilentlyContinue
    Pop-Location
}

$Icon = Join-Path $Root "assets\icon.ico"
if (-not (Test-Path $Icon)) {
    Step "Drawing the icon"
    Invoke-Native "Icon" { & $Python (Join-Path $Root "assets\make_icon.py") }
}

Step "Freezing with PyInstaller"
Invoke-Native "PyInstaller" {
    & $Python -m PyInstaller (Join-Path $Root "tuningbuddy.spec") --noconfirm --log-level WARN `
        --distpath (Join-Path $Root "dist") --workpath (Join-Path $BuildDir "pyinstaller")
}
$Exe = Join-Path $Root "dist\TuningBuddy\TuningBuddy.exe"
Sign-Exe $Exe
Write-Host "App folder: $(Split-Path $Exe)"

if ($SkipInstaller) {
    Write-Host "`nDone (installer skipped). Run: $Exe" -ForegroundColor Green
    exit 0
}

$IsccArgs = @("/Q", "/DAppVersion=$Version")
if (-not $NoDemoDatabase) {
    Step "Staging PostgreSQL for the demo database"
    Invoke-Native "Staging PostgreSQL" { & $Python (Join-Path $Root "stage_pgsql.py") $PgHome }
    $IsccArgs += "/DDemoDatabase"
}

Step "Drawing the installer artwork"
Invoke-Native "Artwork" { & $Python (Join-Path $Root "assets\make_installer_art.py") }

# The custom setup window, frozen twice from setup_ui.spec (see there)
$SetupDist = Join-Path $BuildDir "setup_ui"
function Freeze-SetupUi([string]$Target) {
    $env:TB_SETUP_TARGET = $Target
    $env:TB_SETUP_VERSION = $Version
    try {
        Invoke-Native "PyInstaller ($Target)" {
            & $Python -m PyInstaller (Join-Path $Root "setup_ui.spec") --noconfirm --log-level WARN `
                --distpath $SetupDist --workpath (Join-Path $BuildDir "pyinstaller-$Target")
        }
    } finally {
        Remove-Item Env:TB_SETUP_TARGET, Env:TB_SETUP_VERSION -ErrorAction SilentlyContinue
    }
}

Step "Building the uninstaller"
Freeze-SetupUi "uninstall"
Sign-Exe (Join-Path $SetupDist "TuningBuddyUninstall.exe")  # before the engine packs it

Step "Building the install engine"
Invoke-Native "Inno Setup" { & $Iscc @IsccArgs (Join-Path $Root "installer\TuningBuddy.iss") }
Sign-Exe (Join-Path $BuildDir "engine\TuningBuddyEngine.exe")  # before the setup packs it
$Manifest = @{ version = $Version; demo = (-not $NoDemoDatabase) } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $BuildDir "engine\engine.json"), $Manifest)

Step "Building the installer"
Freeze-SetupUi "setup"
Sign-Exe (Join-Path $SetupDist "TuningBuddySetup.exe")
if (-not $SignTool) { Write-Host "Not code-signed (no -SignCertThumbprint): Windows SmartScreen will warn users." -ForegroundColor Yellow }

Copy-Item (Join-Path $SetupDist "TuningBuddySetup.exe") $Root -Force
Copy-Item (Join-Path $SetupDist "TuningBuddyUninstall.exe") $Root -Force
Write-Host "`nDone: $(Join-Path $Root 'TuningBuddySetup.exe') and TuningBuddyUninstall.exe" -ForegroundColor Green
