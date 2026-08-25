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
SetupIconFile=escsim.ico
WizardStyle=modern

[Files]
Source: "..\dist\ESCSim\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\licenses\usbip-win2-BSD-2-Clause.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "..\build\packaging\USBip-0.9.7.7-x64.exe"; Flags: dontcopy

[Icons]
Name: "{group}\ESCSim"; Filename: "{app}\ESCSim.exe"
Name: "{autodesktop}\ESCSim"; Filename: "{app}\ESCSim.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\ESCSim.exe"; Description: "Launch ESCSim"; Flags: nowait postinstall skipifsilent

[Code]
const
  UsbipInstallerName = 'USBip-0.9.7.7-x64.exe';

function UsbipReady(): Boolean;
var
  Major, Minor, Revision, Build: Cardinal;
  VersionMS, VersionLS: Cardinal;
  Executable: String;
begin
  Executable := ExpandConstant('{autopf}\USBip\usbip.exe');
  Result := GetVersionNumbers(Executable, VersionMS, VersionLS);
  if not Result then
    Exit;
  Major := VersionMS shr 16;
  Minor := VersionMS and $ffff;
  Revision := VersionLS shr 16;
  Build := VersionLS and $ffff;
  Result := (Major > 0) or (Minor > 9) or
    ((Minor = 9) and ((Revision > 7) or ((Revision = 7) and (Build >= 7))));
  { 0.9.7.8 corrupts full-speed transfers and is explicitly unsupported. }
  if (Major = 0) and (Minor = 9) and (Revision = 7) and (Build = 8) then
    Result := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep <> ssPostInstall) or WizardSilent or UsbipReady() then
    Exit;

  if SuppressibleMsgBox(
       'Browser USB support needs the bundled safe usbip-win2 driver. The ' +
       'installed copy is missing, too old, or an unsupported release.' + #13#10 + #13#10 +
       'Installing it requires administrator approval, temporarily restarts ' +
       'USB 3 hubs and connected devices, and may require a Windows reboot. ' +
       'Save any work using USB devices before continuing.' + #13#10 + #13#10 +
       'Install usbip-win2 now?',
       mbConfirmation, MB_YESNO, IDYES) <> IDYES then
    Exit;

  ExtractTemporaryFile(UsbipInstallerName);
  if Exec(ExpandConstant('{tmp}\' + UsbipInstallerName), '/NORESTART', '',
          SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode) and
     ((ResultCode = 0) or (ResultCode = 3010)) then
    SuppressibleMsgBox(
      'usbip-win2 setup finished. Restart Windows before using ESCSim with ' +
      'the browser USB configurator, even if the driver installer did not ' +
      'request it.', mbInformation, MB_OK, IDOK)
  else
    SuppressibleMsgBox(
      'usbip-win2 setup could not be started. ESCSim is installed, but browser ' +
      'USB support will remain unavailable until usbip-win2 is installed.',
      mbError, MB_OK, IDOK);
end;
