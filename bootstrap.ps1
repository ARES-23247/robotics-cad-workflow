<#
.SYNOPSIS
    Zero-Prerequisite One-Line Bootstrapper for a Brand-New Windows 11/10 Computer.
.EXAMPLE
    irm https://raw.githubusercontent.com/ARES-23247/robotics-cad-workflow/main/bootstrap.ps1 | iex
#>
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:CAD_WORKFLOW_REPO_URL) { $env:CAD_WORKFLOW_REPO_URL } else { "https://github.com/ARES-23247/robotics-cad-workflow.git" }
$TargetDir = if ($env:CAD_WORKSPACE_DIR) { $env:CAD_WORKSPACE_DIR } else { Join-Path $HOME "dev\robotics\CAD" }

Write-Host ""
Write-Host "Bootstrapping Robotics CAD Workflow into: $TargetDir" -ForegroundColor Cyan

# Ensure Git is available (auto-install via winget on fresh Windows 11/10 PCs)
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git not found on PATH; installing Git via winget..." -ForegroundColor Yellow
    winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
    $GitPaths = @("C:\Program Files\Git\cmd", "$env:LOCALAPPDATA\Programs\Git\cmd")
    foreach ($gp in $GitPaths) {
        if (Test-Path $gp) { $env:PATH = "$gp;$env:PATH" }
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is still not on PATH. Please open a new PowerShell window and re-run this command."
}

$ParentDir = Split-Path -Parent $TargetDir
if (-not (Test-Path $ParentDir)) {
    New-Item -ItemType Directory -Path $ParentDir -Force | Out-Null
}

if (Test-Path (Join-Path $TargetDir ".git")) {
    Write-Host "Updating existing repository at $TargetDir ..." -ForegroundColor Yellow
    git -C $TargetDir pull --ff-only
} else {
    Write-Host "Cloning $RepoUrl -> $TargetDir ..." -ForegroundColor Yellow
    git clone $RepoUrl $TargetDir
}

& (Join-Path $TargetDir "install.ps1")
