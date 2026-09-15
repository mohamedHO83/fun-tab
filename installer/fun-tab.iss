; Per-user installer. Compile with Inno Setup 6 after `build.bat` has
; packed dist\FunTab\FunTab.exe. `build.bat` will do that when ISCC is on PATH.

#define MyAppName "Fun Tab"
#define MyAppVersion "0.4.0"
#define MyAppPublisher "Fun Tab"
#define MyAppURL "https://github.com"
#define MyAppExeName "FunTab.exe"

[Setup]
AppId={{6E0F2C4A-8B17-4D9E-9A3C-1B2D3E4F5A61}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\FunTab
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=FunTabSetup
SetupIconFile=..\assets\fun-tab.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Fun Tab when Windows starts"; Flags: unchecked

[Files]
Source: "..\dist\FunTab\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "FunTab"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Fun Tab"; Flags: nowait postinstall skipifsilent
