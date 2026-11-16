; Inno Setup script for mt5copier.
;
; Build with:  scripts\build_installer.ps1   (reads the version from pyproject.toml)
; or by hand:  iscc /DAppVersion=0.1.0 installer.iss
;
; Decisions, so they are not re-litigated per build:
; - Per-user install (PrivilegesRequired=lowest): lands in %LOCALAPPDATA%\Programs\MT5 Copier,
;   so no UAC prompt, matching an app whose data already lives per-user in %APPDATA%.
; - The installer carries ONLY dist\mt5copier.exe -- the PyInstaller onefile build is
;   self-contained. The exe must exist before compiling; the build script checks.
; - Uninstall removes the program only. %APPDATA%\MT5Copier -- profiles, configs, the
;   mapping/dry-run stores, guard state, logs -- is deliberately never touched: that is
;   trade bookkeeping, not program files.
; - CloseApplications: a running copier holds its profile lock and the exe itself; Setup
;   asks Windows to close it before replacing, rather than failing the copy.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{B7A41C63-0D2E-4D7A-9B7E-5C1A2F8D33E1}
AppName=MT5 Copier
AppVersion={#AppVersion}
AppPublisher=mt5copier
DefaultDirName={autopf}\MT5 Copier
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=mt5copier-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=src\mt5copier\desktop\web\icon.ico
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\mt5copier.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\mt5copier.exe"; DestDir: "{app}"; Flags: ignoreversion
; The WebView2 Evergreen bootstrapper, fetched by scriptsuild_installer.ps1. Extracted and
; run only on a machine that lacks the runtime (see [Code]); dontcopy keeps it out of {app}.
Source: "build\MicrosoftEdgeWebView2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\MT5 Copier"; Filename: "{app}\mt5copier.exe"
Name: "{autodesktop}\MT5 Copier"; Filename: "{app}\mt5copier.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\mt5copier.exe"; Description: "{cm:LaunchProgram,MT5 Copier}"; Flags: nowait postinstall skipifsilent

[Code]
{ The window is WebView2; Windows 11 ships the runtime, an un-updated Windows 10 may not,
  and without it the window never opens. The Evergreen bootstrapper installs per-machine,
  so on a machine that needs it, IT shows the one UAC prompt this otherwise per-user
  installer never does. }
function WebView2Present(): Boolean;
var
  pv: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', pv) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', pv) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', pv);
  if Result then
    Result := (pv <> '') and (pv <> '0.0.0.0');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  rc: Integer;
begin
  if (CurStep = ssPostInstall) and not WebView2Present() then
  begin
    WizardForm.StatusLabel.Caption := 'Installing the Microsoft WebView2 runtime...';
    ExtractTemporaryFile('MicrosoftEdgeWebView2Setup.exe');
    if (not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebView2Setup.exe'), '/silent /install',
                 '', SW_HIDE, ewWaitUntilTerminated, rc)) or (rc <> 0) then
      MsgBox('The Microsoft WebView2 runtime could not be installed automatically '
             + '(it is needed for the app window). Install it from '
             + 'https://developer.microsoft.com/microsoft-edge/webview2 and run MT5 Copier again.',
             mbInformation, MB_OK);
  end;
end;
