<#
.SYNOPSIS
    Turnkey Windows 11/10 Installer for the Robotics CAD Workflow & 2,588-Part COTS Library.
.DESCRIPTION
    Automatically installs `uv` + Python 3.12 `.venv`, Python CAD/FEA/Physics dependencies,
    SolidCraft Desktop (`v0.40.0+`), the 2,588-part FTC/FRC COTS `.step` + `.sc` library,
    Antigravity AI Plugin + MCP servers, and the `Alt+C` SolidCraft COTS HUD plugin.
#>
param(
    [switch]$WithGpuGenesis,
    [switch]$SkipCotsDownload,
    [switch]$SkipGlobalConfig,
    [switch]$Doctor,
    [string]$HomeDir = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $RepoRoot

try {
    Write-Host ""
    Write-Host "======================================================================" -ForegroundColor Cyan
    Write-Host "  Robotics CAD Workflow + COTS Hardware Library — Turnkey Installer   " -ForegroundColor Cyan
    Write-Host "======================================================================" -ForegroundColor Cyan
    Write-Host "Workspace: $RepoRoot" -ForegroundColor Gray

    # 1. Locate or install `uv` (fast Python + package manager; requires no pre-installed Python)
    $UvExe = $null
    $UvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($UvCmd) {
        $UvExe = $UvCmd.Source
    } else {
        foreach ($cand in @("$HOME\.local\bin\uv.exe", "$HOME\.cargo\bin\uv.exe", "$env:LOCALAPPDATA\uv\uv.exe")) {
            if (Test-Path $cand) {
                $UvExe = $cand
                $env:PATH = "$(Split-Path -Parent $cand);$env:PATH"
                break
            }
        }
    }

    if (-not $UvExe) {
        Write-Host "[1/4] Installing uv Python manager..." -ForegroundColor Yellow
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        foreach ($cand in @("$HOME\.local\bin\uv.exe", "$HOME\.cargo\bin\uv.exe")) {
            if (Test-Path $cand) {
                $UvExe = $cand
                $env:PATH = "$(Split-Path -Parent $cand);$env:PATH"
                break
            }
        }
        if (-not $UvExe) {
            throw "Failed to locate uv.exe after installation. Re-open PowerShell and run .\install.ps1 again."
        }
    } else {
        Write-Host "[1/4] Found uv: $UvExe" -ForegroundColor Green
    }

    # 2. Provision Python 3.12 .venv and install CAD/FEA/Physics packages
    $VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $VenvPy)) {
        Write-Host "[2/4] Creating Python 3.12 virtual environment (.venv)..." -ForegroundColor Yellow
        & $UvExe venv --python 3.12 ".venv"
        if ($LASTEXITCODE -ne 0) { throw "uv venv creation failed." }
    } else {
        Write-Host "[2/4] Found existing virtual environment: $VenvPy" -ForegroundColor Green
    }

    if (-not $Doctor) {
        $ReqFile = if ($WithGpuGenesis -and (Test-Path "requirements-gpu.txt")) { "requirements-gpu.txt" } else { "requirements.txt" }
        Write-Host "[3/4] Syncing Python CAD, OpenCASCADE, FEA, and MuJoCo packages ($ReqFile)..." -ForegroundColor Yellow
        & $UvExe pip install --python $VenvPy -r $ReqFile
        if ($LASTEXITCODE -ne 0) { throw "uv pip install failed for $ReqFile." }
    }

    # 3. Run Python cross-platform setup & health verification
    Write-Host "[4/4] Configuring SolidCraft, COTS CAD Library, Antigravity Plugin & MCP Servers..." -ForegroundColor Yellow
    $PyArgs = @("tools\setup_solidcraft_cots.py", "--no-build")
    if ($Doctor) { $PyArgs = @("tools\setup_solidcraft_cots.py", "--doctor") }
    if ($SkipCotsDownload) { $PyArgs += "--skip-cots-download" }
    if ($SkipGlobalConfig) { $PyArgs += "--skip-global-config" }
    if ($HomeDir) { $PyArgs += @("--home-dir", $HomeDir) }

    & $VenvPy @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "setup_solidcraft_cots.py exited with code $LASTEXITCODE" }

    Write-Host ""
    Write-Host "======================================================================" -ForegroundColor Green
    Write-Host "  Setup Complete! Ready to use in Antigravity + SolidCraft            " -ForegroundColor Green
    Write-Host "======================================================================" -ForegroundColor Green
    Write-Host "Next steps:" -ForegroundColor White
    Write-Host "  1. Open '$RepoRoot' as your workspace in Antigravity (restart Antigravity if it was already open so MCP servers reload)."
    Write-Host "  2. Launch SolidCraft + the Alt+C COTS Hardware HUD anytime by running:"
    Write-Host "       solidcraft-plugin" -ForegroundColor Cyan
    Write-Host "     (or search Windows Start Menu for 'SolidCraft + Robotics COTS HUD')."
    Write-Host "  3. Try building the starter assembly:"
    Write-Host "       .\.venv\Scripts\python.exe tools\cad_pipeline.py build projects\starter-bearing-block\src\build_bearing_block.py" -ForegroundColor Cyan
    Write-Host ""
} finally {
    Pop-Location
}
