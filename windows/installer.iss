; Inno Setup script - builds BeetMusic-Setup.exe from dist\BeetMusic (run bundle.sh first).
; Compile:  ISCC.exe windows\installer.iss   (optionally  /DAppVersion=1.2.0)
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#define AppName "Beet Music"

[Setup]
AppId={{6F1A7C52-2FDE-4B6B-9D31-BEE7A11C0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=StudioOne
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
; per-user install: no administrator rights needed
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=BeetMusic-Setup
SetupIconFile=..\assets\beet-music.ico
UninstallDisplayIcon={app}\Beet Music.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\dist\BeetMusic\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Beet Music.exe"; IconFilename: "{app}\beet-music.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Beet Music.exe"; IconFilename: "{app}\beet-music.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\Beet Music.exe"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
