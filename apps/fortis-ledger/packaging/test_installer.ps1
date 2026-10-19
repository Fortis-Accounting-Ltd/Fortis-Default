# Check the Windows installer the way staff will use it: install for this person only (no administrator
# rights), check the desktop icons and the Start menu entry, run the installed copy's self-test, talk to the
# Claude connector program the way Claude Desktop does (directly, and through the Claude Desktop extension the
# installed copy makes, when Node.js is on the PC), install again over the top (an upgrade), then
# uninstall and check that the program and its icons are gone but the person's settings are kept.
# Used by CI; on your own PC it installs and then removes Fortis Ledger.
#     powershell -ExecutionPolicy Bypass -File packaging\test_installer.ps1 dist\Fortis-Ledger-Setup-<version>.exe
param([Parameter(Mandatory = $true)] [string] $Setup)
$ErrorActionPreference = "Stop"
$Setup = (Resolve-Path $Setup).Path

$dir = Join-Path $env:LOCALAPPDATA "Programs\Fortis Ledger"
$exe = Join-Path $dir "Fortis Ledger.exe"
$connector = Join-Path $dir "fortis-ledger-mcp.exe"
$desktop = [Environment]::GetFolderPath("Desktop")
$appIcon = Join-Path $desktop "Fortis Ledger.lnk"
$xeroIcon = Join-Path $desktop "Fortis Ledger - Connect to Xero.lnk"
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Fortis Ledger.lnk"
$settingsDir = Join-Path $env:APPDATA "Fortis Ledger"
$log = Join-Path ([IO.Path]::GetTempPath()) "fortis-ledger-setup.log"

function Install-Once {
    $p = Start-Process -FilePath $Setup -PassThru -Wait -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/TASKS=desktopicon,xeroicon", "/LOG=`"$log`"")
    if ($p.ExitCode -ne 0) {
        if (Test-Path $log) { Get-Content $log -Tail 40 }
        throw "setup failed (exit $($p.ExitCode))"
    }
}

function Wait-For([scriptblock] $Done, [string] $What) {
    $deadline = (Get-Date).AddSeconds(120)
    while (-not (& $Done)) {
        if ((Get-Date) -gt $deadline) { throw "timed out waiting for $What" }
        Start-Sleep -Milliseconds 500
    }
}

Write-Host "1. Install for this person only, with both desktop icons"
Install-Once
foreach ($f in @($exe, $connector, (Join-Path $dir "install.ini"), $appIcon, $xeroIcon, $startMenu)) {
    if (-not (Test-Path $f)) { throw "missing after install: $f" }
}

Write-Host "2. The icons open the installed copy (the second one at Connect to Xero)"
$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in @($appIcon, $startMenu)) {
    $s = $shell.CreateShortcut($lnk)
    if ($s.TargetPath -ne $exe -or $s.Arguments) { throw "$lnk opens '$($s.TargetPath) $($s.Arguments)'" }
}
$s = $shell.CreateShortcut($xeroIcon)
if ($s.TargetPath -ne $exe -or $s.Arguments -ne "--start xero") { throw "$xeroIcon opens '$($s.TargetPath) $($s.Arguments)'" }

Write-Host "3. Settings > Apps lists it"
$entry = Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" |
         Where-Object { $_.GetValue("DisplayName") -eq "Fortis Ledger" }
if (-not $entry) { throw "Fortis Ledger is not in Settings > Apps" }
Write-Host "   $($entry.GetValue('DisplayName')) $($entry.GetValue('DisplayVersion')) by $($entry.GetValue('Publisher'))"

Write-Host "4. The installed copy passes its self-test"
$p = Start-Process -FilePath $exe -ArgumentList "--self-test" -Wait -PassThru
Get-Content (Join-Path $settingsDir "self-test.txt")
if ($p.ExitCode -ne 0) { throw "the installed copy failed its self-test (exit $($p.ExitCode))" }

Write-Host "4b. The Claude connector program answers the way Claude Desktop talks to it"
if (Get-Command python -ErrorAction SilentlyContinue) {
    python (Join-Path $PSScriptRoot "check_connector.py") $connector
    if ($LASTEXITCODE -ne 0) { throw "the installed Claude connector failed its check" }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        Write-Host "   and through its Claude Desktop extension, run with Node.js as Claude Desktop does"
        python (Join-Path $PSScriptRoot "check_connector.py") --extension $connector
        if ($LASTEXITCODE -ne 0) { throw "the installed copy's Claude Desktop extension failed its check" }
    }
} else {
    Write-Host "   (Python is not on this PC, so the connector check was skipped)"
}

Write-Host "5. Installing again over the top (an upgrade) works"
Install-Once
if (-not (Test-Path $exe)) { throw "the upgrade removed the program" }

Write-Host "6. Uninstall: the program and its icons go, the person's settings stay"
$uninstaller = Join-Path $dir "unins000.exe"
Start-Process -FilePath $uninstaller -Wait -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
Wait-For { -not (Test-Path $exe) } "the uninstaller"
Wait-For { -not (Test-Path $uninstaller) } "the uninstaller to remove itself"
foreach ($f in @($appIcon, $xeroIcon, $startMenu)) {
    if (Test-Path $f) { throw "left behind after uninstall: $f" }
}
if (-not (Test-Path (Join-Path $settingsDir "self-test.txt"))) { throw "uninstall removed the person's settings folder" }
Write-Host "Installer OK: $Setup"
