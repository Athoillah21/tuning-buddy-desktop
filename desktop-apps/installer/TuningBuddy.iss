; Inno Setup script for Tuning Buddy: the install ENGINE. Built by build.ps1 after PyInstaller:
;   iscc /DAppVersion=1.3.7 [/DDemoDatabase] installer\TuningBuddy.iss
; DemoDatabase adds the optional demo PostgreSQL (build\pgsql, staged by stage_pgsql.py).
;
; Users normally never see this wizard. TuningBuddySetup.exe (setup_ui/) shows its own window and
; runs this engine silently:
;   TuningBuddyEngine.exe /SILENT /SUPPRESSMSGBOXES /NORESTART /ALLUSERS|/CURRENTUSER
;       /DIR=... /TASKS=demodb,desktopicon /PROGRESSFILE=<path> /LOG=<path>
; and follows /PROGRESSFILE, a small JSON file this script rewrites as it goes. /SILENT rather than
; /VERYSILENT because only then can Setup be cancelled; its progress window is made invisible (see
; InitializeWizard). The window cancels by creating <progressfile>.cancel: before the next file,
; this script closes the wizard as if Cancel were clicked, and Setup rolls back what it copied.
; The wizard (skinned to match) only appears when the engine is run directly, e.g. where WebView2
; is missing.

#ifndef AppVersion
  #define AppVersion "1.3.7"
#endif

#define AppName "Tuning Buddy"
#define AppExe "TuningBuddy.exe"
#define UninstallerExe "TuningBuddyUninstall.exe"
#define AppIdGuid "{6B0E4C2A-9E57-4F1B-A7D3-5C2E8B1F0A94}"

[Setup]
AppId={{6B0E4C2A-9E57-4F1B-A7D3-5C2E8B1F0A94}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Tuning Buddy
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Program Files for everyone by default; the dialog allows a per-user install without admin rights
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Same name as the launcher's single-instance mutex: setup asks to close a running copy
AppMutex=Local\TuningBuddy.SingleInstance
SourceDir=..
; The engine is embedded into TuningBuddySetup.exe by build.ps1
OutputDir=build\engine
OutputBaseFilename=TuningBuddyEngine
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
; The fallback wizard, skinned to match the custom installer: follows Windows light/dark
WizardStyle=modern dynamic windows11 hidebevels
WizardImageFile=build\art\wizard-side.png
WizardImageFileDynamicDark=build\art\wizard-side-dark.png
WizardSmallImageFile=build\art\wizard-small.png
WizardSmallImageFileDynamicDark=build\art\wizard-small-dark.png

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
#ifdef DemoDatabase
Name: "demodb"; Description: "Load the demo PostgreSQL database to try Tuning Buddy on (about 400 MB of sample tables, takes under a minute)"; GroupDescription: "Demo data:"
#endif

[Files]
Source: "dist\TuningBuddy\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; BeforeInstall: StopIfCancelled
; The premium uninstaller (setup_ui in uninstall mode); Settings > Apps opens it, see ssPostInstall
Source: "build\setup_ui\{#UninstallerExe}"; DestDir: "{app}"; Flags: ignoreversion; BeforeInstall: StopIfCancelled
#ifdef DemoDatabase
Source: "build\pgsql\*"; DestDir: "{app}\pgsql"; Tasks: demodb; Flags: ignoreversion recursesubdirs createallsubdirs; BeforeInstall: StopIfCancelled
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{app}\{#UninstallerExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  DemoDataHint = '%LOCALAPPDATA%\TuningBuddy\demo-db';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdGuid}_is1';

var
  LastPercent: Integer;
  DemoResult: String;
  CancelSent: Boolean;
  WasInstalled: Boolean;

// ---------------------------------------------------------------------------
// Progress for the custom installer window (only when /PROGRESSFILE= is given)
// ---------------------------------------------------------------------------

function ProgressFile(): String;
begin
  Result := ExpandConstant('{param:PROGRESSFILE|}');
end;

procedure Report(Phase: String; Percent: Integer);
var
  Json: String;
begin
  if ProgressFile() = '' then
    Exit;
  Json := '{"phase": "' + Phase + '", "percent": ' + IntToStr(Percent);
  if DemoResult <> '' then
    Json := Json + ', "demo": "' + DemoResult + '"';
  Json := Json + ', "version": "{#AppVersion}"}';
  SaveStringToFile(ProgressFile(), Json, False);
end;

{ Cancel from the window: it creates <progressfile>.cancel }
function CancelRequested(): Boolean;
begin
  Result := (ProgressFile() <> '') and FileExists(ProgressFile() + '.cancel');
end;

{ Runs before each file is copied: closing the wizard is Setup's own Cancel, which rolls back.
  (An error raised here would only be logged, and /VERYSILENT ignores every way of cancelling.) }
procedure StopIfCancelled();
begin
  if CancelRequested() and not CancelSent then
  begin
    CancelSent := True;
    Report('cancelling', LastPercent);
    WizardForm.Close;
  end;
end;

{ No "are you sure?" for a cancel the window already confirmed }
procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if CancelRequested() then
    Confirm := False;
end;

{ Driven by the custom window, the /SILENT progress window must not show: fully transparent,
  click-through, never activated, and no taskbar button }
const
  GWL_EXSTYLE = -20;
  WS_EX_TRANSPARENT = $20;
  WS_EX_TOOLWINDOW = $80;
  WS_EX_APPWINDOW = $40000;
  WS_EX_LAYERED = $80000;
  WS_EX_NOACTIVATE = $8000000;
  LWA_ALPHA = 2;

function GetWindowLong(Wnd: HWND; Index: Integer): Longint; external 'GetWindowLongW@user32.dll stdcall';
function SetWindowLong(Wnd: HWND; Index: Integer; NewLong: Longint): Longint; external 'SetWindowLongW@user32.dll stdcall';
function SetLayeredWindowAttributes(Wnd: HWND; Key: DWORD; Alpha: Byte; Flags: DWORD): BOOL;
  external 'SetLayeredWindowAttributes@user32.dll stdcall';

procedure InitializeWizard();
begin
  if ProgressFile() = '' then
    Exit;
  SetWindowLong(WizardForm.Handle, GWL_EXSTYLE,
    (GetWindowLong(WizardForm.Handle, GWL_EXSTYLE) or WS_EX_TRANSPARENT or WS_EX_TOOLWINDOW or WS_EX_LAYERED or
     WS_EX_NOACTIVATE) and (not WS_EX_APPWINDOW));
  SetLayeredWindowAttributes(WizardForm.Handle, 0, 0, LWA_ALPHA);
end;

procedure CurInstallProgressChanged(CurProgress, MaxProgress: Integer);
var
  Percent: Integer;
begin
  if MaxProgress <= 0 then
    Exit;
  Percent := (CurProgress * 100) div MaxProgress;
  if Percent <> LastPercent then
  begin
    LastPercent := Percent;
    Report('files', Percent);
  end;
end;

// ---------------------------------------------------------------------------
// The demo database
// ---------------------------------------------------------------------------

// A demo server left running (e.g. after a crash) holds files in the app's pgsql folder open. Only
// versions that ship pgsql understand --stop-demo; an older TuningBuddy.exe would open its window instead.
procedure StopDemoDatabase(AsOriginalUser: Boolean);
var
  ResultCode: Integer;
begin
  if not FileExists(ExpandConstant('{app}\pgsql\bin\pg_ctl.exe')) then
    Exit;
  if AsOriginalUser then
    ExecAsOriginalUser(ExpandConstant('{app}\{#AppExe}'), '--stop-demo', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
  else
    Exec(ExpandConstant('{app}\{#AppExe}'), '--stop-demo', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

#ifdef DemoDatabase
{ Runs as the user who started setup (not an elevated admin), so the data lands in their profile }
procedure SetUpDemoDatabase();
var
  ResultCode: Integer;
  Ok: Boolean;
  Params: String;
begin
  if CancelRequested() then
  begin
    DemoResult := 'cancelled';
    Exit;
  end;
  Report('demo', 100);
  Params := '--setup-demo';
  if ProgressFile() <> '' then
    Params := Params + ' --progress-file "' + ProgressFile() + '.demo"';
  if not WizardSilent() then
  begin
    WizardForm.StatusLabel.Caption := 'Loading the demo database. This takes under a minute...';
    WizardForm.FilenameLabel.Caption := '';
    WizardForm.ProgressGauge.Style := npbstMarquee;
  end;
  try
    Ok := ExecAsOriginalUser(ExpandConstant('{app}\{#AppExe}'), Params, '', SW_HIDE,
                             ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  finally
    if not WizardSilent() then
      WizardForm.ProgressGauge.Style := npbstNormal;
  end;
  if Ok then
    DemoResult := 'ok'
  else if ResultCode = 3 then
    DemoResult := 'cancelled'  { the window's Cancel; the setup removed what it had loaded }
  else
  begin
    DemoResult := 'failed';
    SuppressibleMsgBox('The demo database could not be set up. Tuning Buddy works without it, and you can ' +
                       'still connect your own PostgreSQL.' + #13#10#13#10 +
                       'Details are in ' + DemoDataHint + '\setup.log', mbError, MB_OK, IDOK);
  end;
end;
#endif

// ---------------------------------------------------------------------------
// Install steps
// ---------------------------------------------------------------------------

{ Before Setup checks for files in use, so a leftover demo server is not listed as an app to close }
{ Cancelled while Windows was asking for admin permission: stop before anything is installed }
function InitializeSetup(): Boolean;
begin
  WasInstalled := RegKeyExists(HKLM64, UninstallKey) or RegKeyExists(HKLM32, UninstallKey) or
                  RegKeyExists(HKCU, UninstallKey);
  Result := not CancelRequested();
end;

{ A cancel that came after the files were copied (while the demo database loaded) can't be rolled
  back by Setup. A new install is removed again by its own uninstaller, run from here because this
  process already has the admin rights it needs; an update stays, without the demo database. }
procedure UndoIfCancelled();
var
  ResultCode: Integer;
begin
  if not CancelRequested() then
  begin
    Report('done', 100);
    Exit;
  end;
  if WasInstalled then
  begin
    Report('done', 100);
    Exit;
  end;
  Report('removing', 100);
  Exec(ExpandConstant('{uninstallexe}'), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Log('Removed again after the window''s Cancel; the uninstaller exited with ' + IntToStr(ResultCode));
  Report('cancelled', 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  LastPercent := -1;
  if CancelRequested() then
  begin
    Result := 'Cancelled from the setup window.';
    Exit;
  end;
  Report('preparing', 0);
  StopDemoDatabase(True);
  Result := '';
end;

{ Settings > Apps > Uninstall opens the premium uninstaller; scripts can still use the quiet one }
procedure PointUninstallAtPremiumUninstaller();
var
  Uninstaller: String;
begin
  Uninstaller := ExpandConstant('{app}\{#UninstallerExe}');
  if not FileExists(Uninstaller) then
    Exit;
  RegWriteStringValue(HKA, UninstallKey, 'UninstallString', '"' + Uninstaller + '"');
  RegWriteStringValue(HKA, UninstallKey, 'QuietUninstallString',
                      '"' + ExpandConstant('{uninstallexe}') + '" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    Report('files', 0)
  else if CurStep = ssPostInstall then
  begin
    PointUninstallAtPremiumUninstaller();
#ifdef DemoDatabase
    if WizardIsTaskSelected('demodb') then
      SetUpDemoDatabase();
#endif
    Report('finishing', 100);
  end
  else if CurStep = ssDone then
    UndoIfCancelled();
end;

// ---------------------------------------------------------------------------
// Uninstall
// ---------------------------------------------------------------------------

{ The premium uninstaller deletes the user's data itself (as the signed-in user, after this has
  finished). Run on its own, the stock uninstaller still asks. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
    StopDemoDatabase(False);
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\TuningBuddy');
    if DirExists(DataDir) and not UninstallSilent() then
      if MsgBox('Also remove your Tuning Buddy data?' + #13#10#13#10 +
                'This deletes saved connections, analysis history, AI providers, API keys and the ' +
                'demo database in:' + #13#10 + DataDir, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
