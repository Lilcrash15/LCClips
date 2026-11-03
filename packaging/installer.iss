; LCClips installer (Inno Setup 6). Built by packaging\build.py.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef Root
  #define Root ".."
#endif

[Setup]
AppId={{8F3C1A52-6B1E-4C1F-9D0A-6C2B7E5D4A91}
AppName=LCClips
AppVersion={#MyAppVersion}
AppVerName=LCClips {#MyAppVersion}
AppPublisher=Lilcrash15
AppPublisherURL=https://github.com/Lilcrash15/LCClips
AppSupportURL=https://github.com/Lilcrash15/LCClips/issues
; Per-user install (no admin prompt), like Discord / Medal.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\LCClips
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir={#Root}\dist
OutputBaseFilename=LCClips-Setup-{#MyAppVersion}
SetupIconFile={#Root}\build\icon.ico
UninstallDisplayIcon={app}\LCClips.exe
UninstallDisplayName=LCClips
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Closes a running LCClips (it lives in the tray) before upgrading files.
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start LCClips with Windows (runs quietly in the tray)"; GroupDescription: "Startup:"

[InstallDelete]
; Clear the previous version's bundled libraries so upgrades never mix files.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Root}\dist\LCClips\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\server\*"; DestDir: "{app}\server"; Excludes: "node_modules,.wrangler,.dev.vars"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\LCClips"; Filename: "{app}\LCClips.exe"
Name: "{autodesktop}\LCClips"; Filename: "{app}\LCClips.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LCClips"; \
  ValueData: """{app}\LCClips.exe"" --minimized"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\LCClips.exe"; Description: "Launch LCClips now"; Flags: nowait postinstall skipifsilent
; In-app updates run the installer with /SILENT - reopen LCClips afterwards.
Filename: "{app}\LCClips.exe"; Parameters: "--updated"; Flags: nowait skipifnotsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM LCClips.exe /F"; Flags: runhidden; RunOnceId: "StopLCClips"

[UninstallDelete]
; Settings, clips and logs live in %APPDATA%\LCClips and your Videos folder and are kept.
Type: filesandordirs; Name: "{app}\server\node_modules"
