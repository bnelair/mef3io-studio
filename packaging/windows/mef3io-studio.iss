; Inno Setup script for the Windows installer.
; Built in CI by .github/workflows/build.yml (Inno Setup 6.7.3, pinned there):
;   iscc /DAppVersion=0.0.1 /DSourceDir=..\..\dist\mef3io-studio /DOutputDir=..\..\dist packaging\windows\mef3io-studio.iss
;
; Installs per user by default (no administrator rights needed, which matters
; on managed clinical machines); the user can choose an all-users install.
; TODO(signing): sign the app exe and this installer with Authenticode once a
; certificate is available (SignTool=... here, plus a signing step in build.yml).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\mef3io-studio"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
AppId={{6B0D3C8E-5E7B-4C4B-9A43-2E6F3B1A7D51}
AppName=mef3io-studio
AppVersion={#AppVersion}
AppVerName=mef3io-studio {#AppVersion}
AppPublisher=Mayo Clinic BNEL
AppPublisherURL=https://github.com/bnelair/mef3io-studio
AppSupportURL=https://github.com/bnelair/mef3io-studio/issues
DefaultDirName={autopf}\mef3io-studio
DefaultGroupName=mef3io-studio
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=mef3io-studio-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\mef3io-studio.exe
LicenseFile=..\..\LICENSE
#ifdef SetupIcon
SetupIconFile={#SetupIcon}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the whole bundle: files a previous version shipped but
; this one does not (an old Qt plugin, a renamed library) must not linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\mef3io-studio"; Filename: "{app}\mef3io-studio.exe"
Name: "{autodesktop}\mef3io-studio"; Filename: "{app}\mef3io-studio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\mef3io-studio.exe"; Description: "{cm:LaunchProgram,mef3io-studio}"; Flags: nowait postinstall skipifsilent

[Code]
var
  RemoveUserData: Boolean;

function InitializeUninstall(): Boolean;
begin
  { Default: keep preferences and logs (e.g. when reinstalling). A silent
    uninstall removes them only when run with /REMOVEUSERDATA=1. }
  RemoveUserData := ExpandConstant('{param:REMOVEUSERDATA|0}') = '1';
  if not UninstallSilent then
    RemoveUserData := MsgBox(
      'Also remove your mef3io-studio preferences and its log files?' + #13#10 + #13#10 +
      'Your recordings are never touched. Choose No to keep them, for example if you ' +
      'are reinstalling.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveUserData then
  begin
    DelTree(ExpandConstant('{localappdata}\BNEL\mef3io-studio'), True, True, True);
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\BNEL\mef3io-studio');
  end;
end;
