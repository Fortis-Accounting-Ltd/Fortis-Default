# Check the Windows installer the way staff will use it: install for this person only (no administrator
# rights), check the desktop icons and the Start menu entry, run the installed copy's self-test, talk to the
# Claude connector program the way Claude Desktop does (directly, and through the Claude Desktop extension the
# installed copy makes, when Node.js is on the PC), install an update file (when one is given: it must be signed
# by GitHub for the firm's build - CI makes one), install again over the top (an upgrade), then uninstall and
# check that the program, its icons and updates are gone but the person's settings are kept.
# Used by CI; on your own PC it installs and then removes Fortis Ledger.
#     powershell -ExecutionPolicy Bypass -File packaging\test_installer.ps1 dist\Fortis-Ledger-Setup-<version>.exe [update file]
param([Parameter(Mandatory = $true)] [string] $Setup, [string] $TestUpdate = "")
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
$updates = Join-Path $env:LOCALAPPDATA "Fortis Ledger\updates"
$fileType = "HKCU:\Software\Classes\FortisLedger.Update"
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

Write-Host "3b. Update files (.fortisupdate) open in Fortis Ledger"
$assoc = (Get-ItemProperty "HKCU:\Software\Classes\.fortisupdate").'(default)'
$command = (Get-ItemProperty "$fileType\shell\open\command").'(default)'
if ($assoc -ne "FortisLedger.Update" -or $command -ne "`"$exe`" --install-update `"%1`"") {
    throw "update files do not open in Fortis Ledger ($assoc, $command)"
}

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

if ($TestUpdate) {
    $TestUpdate = (Resolve-Path $TestUpdate).Path
    $installed = (& $connector --version | Out-String).Trim()
    $next = [IO.Path]::GetFileNameWithoutExtension($TestUpdate) -replace '^Fortis-Ledger-Update-', ''
    Write-Host "4c. An update file installs, and the program starts with it ($installed -> $next)"
    $out = (& $connector --install-update $TestUpdate --yes | Out-String).Trim()
    Write-Host "   $out"
    if ($LASTEXITCODE -ne 0) { throw "the update did not install" }
    $now = (& $connector --version | Out-String).Trim()
    if ($now -ne "Fortis Ledger $next") { throw "after the update the program says '$now'" }
    $p = Start-Process -FilePath $exe -ArgumentList "--self-test" -Wait -PassThru
    $report = Get-Content (Join-Path $settingsDir "self-test.txt")
    $report | Select-Object -First 3
    if ($p.ExitCode -ne 0) { $report; throw "the updated program failed its self-test (exit $($p.ExitCode))" }
    if (-not ($report -match [regex]::Escape("Fortis Ledger\updates\$next\fortis_ledger"))) { throw "the self-test did not run the update" }

    Write-Host "4d. A changed update file is refused"
    $bad = Join-Path ([IO.Path]::GetTempPath()) "Fortis-Ledger-Update-changed.fortisupdate"
    Copy-Item $TestUpdate $bad -Force
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::Open($bad, "Update")
    $entry = $zip.GetEntry("fortis_ledger/store.py")
    $reader = New-Object IO.StreamReader($entry.Open())
    $code = $reader.ReadToEnd()
    $reader.Close()
    $entry.Delete()
    $writer = New-Object IO.StreamWriter($zip.CreateEntry("fortis_ledger/store.py").Open())
    $writer.Write($code + "`n# changed`n")
    $writer.Close()
    $zip.Dispose()
    $out = (& $connector --install-update $bad --yes | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -or $out -notmatch "changed after the update was made") { throw "a changed update file was not refused: $out" }
    Write-Host "   $out"

    Write-Host "4e. An update that does not start is set aside, and the program's own version starts"
    $broken = Join-Path $updates "99.0\fortis_ledger"
    New-Item -ItemType Directory -Force $broken | Out-Null
    Set-Content (Join-Path $broken "__init__.py") 'raise RuntimeError("this update is broken")'
    Set-Content (Join-Path $updates "current.json") '{"version": "99.0", "dir": "99.0", "runtime": "1"}'
    $now = (& $connector --version | Out-String).Trim()
    if ($now -ne $installed) { throw "with a broken update the program says '$now', not '$installed'" }
    $cur = Get-Content (Join-Path $updates "current.json") -Raw | ConvertFrom-Json
    if ($cur.bad -notmatch "this update is broken") { throw "the broken update was not set aside ($($cur.bad))" }
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
if (Test-Path $fileType) { throw "uninstall left the update file type behind" }
if (Test-Path $updates) { throw "uninstall left the installed updates behind: $updates" }
if (-not (Test-Path (Join-Path $settingsDir "self-test.txt"))) { throw "uninstall removed the person's settings folder" }
Write-Host "Installer OK: $Setup"
