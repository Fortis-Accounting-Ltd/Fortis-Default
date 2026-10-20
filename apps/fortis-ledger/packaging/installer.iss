; Fortis Ledger - the Windows installer (Inno Setup 6.7 or later).
;
; Makes one file, dist\Fortis-Ledger-Setup-<version>.exe, from the PyInstaller build in dist\Fortis Ledger.
; Share that file; each person double-clicks it to install.  Build it with packaging\build_installer.ps1.
;
; - Installs for the person running it, in %LOCALAPPDATA%\Programs\Fortis Ledger: no administrator rights.
;   IT can install for everyone on a PC with  /ALLUSERS  from an administrator prompt.
; - Start menu entry, plus desktop icons the person ticks: Fortis Ledger, and Connect to Xero.
; - Running a newer setup upgrades in place.  Uninstalling (Settings > Apps) removes the program and its
;   icons only: the ledger in SharePoint and the person's settings are kept.
; - Silent install, e.g. for Intune:  Fortis-Ledger-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES
;   (add /TASKS=desktopicon,xeroicon to choose the icons).
; - Update files (.fortisupdate - see fortis_ledger/updates.py) open in Fortis Ledger, which checks them and asks
;   before installing them.  Installed updates live in %LOCALAPPDATA%\Fortis Ledger\updates; uninstalling removes them.

#define AppName "Fortis Ledger"
#define AppExe "Fortis Ledger.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; the AppId identifies Fortis Ledger to Windows - never change it, or upgrades install a second copy
AppId={{20B8688E-BC74-4FAB-8F6D-83C09D0C050A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Fortis Accounting Partners
AppPublisherURL=https://fortisap.com.au
AppSupportURL=https://fortisap.com.au
AppCopyright=Fortis Accounting Partners
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Fortis Accounting Partners
VersionInfoDescription={#AppName} setup
VersionInfoProductName={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
DisableReadyPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Fortis-Ledger-Setup-{#AppVersion}
SetupIconFile=fortis-ledger.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardImageFile=installer-side.png,installer-side-2x.png
WizardSmallImageFile=installer-top.png,installer-top-2x.png,installer-top-3x.png
ShowLanguageDialog=no
Compression=lzma2/max
SolidCompression=yes
; Claude's connector (fortis-ledger-mcp.exe) is closed by [Code] below; anything else still using the program's files
; (Fortis Ledger itself) is listed for the person to close, and closed by force if it does not respond
CloseApplications=force
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
SetupLogging=yes
ChangesAssociations=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This installs [name/ver] on this computer.%n%nThe ledger itself stays in the firm's SharePoint library - this installs only the program, and you do not need administrator rights.
FinishedLabel=Fortis Ledger is installed. Open it from its desktop icon or the Start menu.%n%nIf Claude Desktop is open, quit it (right-click its icon by the clock, then Quit) and open it again, so Claude uses this version.

[Tasks]
Name: "desktopicon"; Description: "Fortis Ledger"; GroupDescription: "Put these icons on the desktop:"
Name: "xeroicon"; Description: "Fortis Ledger - Connect to Xero (opens it straight at Connect to Xero)"; GroupDescription: "Put these icons on the desktop:"

[InstallDelete]
; an upgrade replaces the program's own folder completely, so no file from an older version is left over
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Fortis Ledger\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; tells the app that this installer looks after the desktop icons (so it does not add its own)
Source: "install.ini"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; update files open in Fortis Ledger (HKA: this person's own settings, or the PC's for an /ALLUSERS install)
Root: HKA; Subkey: "Software\Classes\.fortisupdate"; ValueType: string; ValueName: ""; ValueData: "FortisLedger.Update"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.fortisupdate\OpenWithProgids"; ValueType: string; ValueName: "FortisLedger.Update"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\FortisLedger.Update"; ValueType: string; ValueName: ""; ValueData: "Fortis Ledger update"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\FortisLedger.Update\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"
Root: HKA; Subkey: "Software\Classes\FortisLedger.Update\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" --install-update ""%1"""

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "The firm's ledger and Financial Reports"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "The firm's ledger and Financial Reports"; Tasks: desktopicon
Name: "{autodesktop}\{#AppName} - Connect to Xero"; Filename: "{app}\{#AppExe}"; Parameters: "--start xero"; WorkingDir: "{app}"; Comment: "Open Fortis Ledger at Connect to Xero"; Tasks: xeroicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open Fortis Ledger now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; icons the app itself made on this person's desktop (Settings > Desktop icons)
Type: files; Name: "{userdesktop}\{#AppName}.lnk"
Type: files; Name: "{userdesktop}\{#AppName} - Connect to Xero.lnk"
; the updates this person installed (a later install starts from the program's own version)
Type: filesandordirs; Name: "{localappdata}\{#AppName}\updates"

[Code]
// Claude Desktop keeps Claude's connector, fortis-ledger-mcp.exe, running in the background while the Fortis Ledger
// extension is on. It has no window, so Windows cannot ask it to close, and it holds no work of its own: close it
// before its files are replaced or removed. Claude Desktop starts the new one when it is next opened.
procedure CloseClaudeConnector;
var
  ResultCode: Integer;
begin
  if Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM fortis-ledger-mcp.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    if ResultCode = 0 then
      Sleep(500);   // one was running: let Windows release its files
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseClaudeConnector;
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    CloseClaudeConnector;
end;
