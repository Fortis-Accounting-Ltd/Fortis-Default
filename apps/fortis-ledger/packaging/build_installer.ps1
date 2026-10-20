# Build the Windows installer, dist\Fortis-Ledger-Setup-<version>.exe, from the PyInstaller build in
# dist\Fortis Ledger (build that first - see build_windows.ps1).  Needs Inno Setup 6.7 or later:
# https://jrsoftware.org/isinfo.php   Run from apps\fortis-ledger in PowerShell:
#     powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1
param([string] $Iscc = "", [switch] $SkipIfNoInnoSetup)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path "dist\Fortis Ledger\Fortis Ledger.exe")) {
    throw "Build the app first: pyinstaller --noconfirm --clean packaging\fortis-ledger.spec"
}
if (-not $Iscc) {
    $Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
            Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $Iscc) {
    if ($SkipIfNoInnoSetup) { Write-Host "Inno Setup is not installed, so no installer was built: https://jrsoftware.org/isinfo.php"; return }
    throw "Inno Setup 6 is not installed - get it from https://jrsoftware.org/isinfo.php"
}

$version = (Select-String -Path "fortis_ledger\__init__.py" -Pattern '^__version__ = "([^"]+)"').Matches[0].Groups[1].Value
& $Iscc "/DAppVersion=$version" "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup could not build the installer (exit $LASTEXITCODE)" }

$setup = Resolve-Path "dist\Fortis-Ledger-Setup-$version.exe"
Write-Host "Built $setup"
