#define AppName "ESCSim"
#define AppVersion "0.1.0"
#define AppPublisher "AM32 project"

[Setup]
AppId={{3D4D34A4-D7A7-4A7F-AF28-8D0D1CBDBEA9}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\ESCSim
DefaultGroupName=ESCSim
OutputDir=..\dist\installer
OutputBaseFilename=ESCSim-installer
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\ESCSim.exe
WizardStyle=modern

[Files]
Source: "..\dist\ESCSim\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ESCSim"; Filename: "{app}\ESCSim.exe"
Name: "{autodesktop}\ESCSim"; Filename: "{app}\ESCSim.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\ESCSim.exe"; Description: "Launch ESCSim"; Flags: nowait postinstall skipifsilent
