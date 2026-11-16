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

[Icons]
Name: "{autoprograms}\MT5 Copier"; Filename: "{app}\mt5copier.exe"
Name: "{autodesktop}\MT5 Copier"; Filename: "{app}\mt5copier.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\mt5copier.exe"; Description: "{cm:LaunchProgram,MT5 Copier}"; Flags: nowait postinstall skipifsilent
