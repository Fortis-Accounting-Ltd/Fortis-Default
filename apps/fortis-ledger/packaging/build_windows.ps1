# Build Fortis Ledger for Windows.  Run from apps\fortis-ledger in PowerShell:
#     powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Result: dist\Fortis Ledger\Fortis Ledger.exe (copy the whole 'Fortis Ledger' folder to each PC).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .venv-build)) { python -m venv .venv-build }
.\.venv-build\Scripts\python -m pip install --upgrade pip
.\.venv-build\Scripts\python -m pip install -r requirements.txt pyinstaller
.\.venv-build\Scripts\pyinstaller --noconfirm --clean packaging\fortis-ledger.spec

# check the build end to end (ledger, imports, Financial Reports for every entity type)
$p = Start-Process -FilePath "dist\Fortis Ledger\Fortis Ledger.exe" -ArgumentList "--self-test" -Wait -PassThru
Get-Content "$env:APPDATA\Fortis Ledger\self-test.txt"
if ($p.ExitCode -ne 0) { throw "self-test failed" }
Compress-Archive -Path "dist\Fortis Ledger" -DestinationPath "dist\Fortis-Ledger-windows.zip" -Force
Write-Host "Built dist\Fortis Ledger\Fortis Ledger.exe and dist\Fortis-Ledger-windows.zip"
