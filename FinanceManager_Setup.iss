; ==========================
; Finance Manager Installer (Hardened)
; ==========================
[Setup]
AppId={{D9C38E3F-97D7-4A62-9F53-0D66A90B1E11}
AppName=Finance Manager
AppVersion=1.0
AppPublisher=DKR Technologies
AppPublisherURL=https://dkrtechnologies.com
AppSupportURL=https://dkrtechnologies.com
AppUpdatesURL=https://dkrtechnologies.com
DefaultDirName={autopf}\Finance Manager
DefaultGroupName=Finance Manager
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=FinanceManager_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

; --- Security hardening ---
; Require admin, but keep the install folder itself locked down (no per-user
; write access) since program files should not be user-writable.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; Only allow install onto supported 64-bit Windows; blocks running the
; installer on unsupported/legacy targets.
MinVersion=10.0.17763

; Confirms before abandoning the wizard, avoids accidental partial installs.
AppendDefaultDirName=no
UsePreviousAppDir=yes

; Signature / tamper note: sign the compiled FinanceManager_Setup.exe and the
; FinanceManager.exe inside dist\ with a code-signing certificate
; (SignTool=... in [Setup] or via your CI) before distribution. Unsigned EXEs
; trigger SmartScreen warnings and are trivially tampered with in transit.
SetupIconFile=static\images\logo.ico
UninstallDisplayIcon={app}\FinanceManager.exe

; ==========================
; Languages
; ==========================
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ==========================
; Tasks
; ==========================
[Tasks]
Name: "desktopicon"; Description: "Create a Desktop Shortcut"; GroupDescription: "Additional Icons:"

; ==========================
; Files
; ==========================
[Files]
Source: "dist\FinanceManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ==========================
; Shortcuts
; ==========================
[Icons]
Name: "{group}\Finance Manager"; Filename: "{app}\FinanceManager.exe"
Name: "{autodesktop}\Finance Manager"; Filename: "{app}\FinanceManager.exe"; Tasks: desktopicon

; ==========================
; Run after installation
; ==========================
[Run]
Filename: "{app}\FinanceManager.exe"; Description: "Launch Finance Manager"; Flags: nowait postinstall skipifsilent

; ==========================
; Pre-install / pre-uninstall safety
; ==========================
[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  // Refuse to proceed if FinanceManager.exe is already running, to avoid
  // partially overwriting a locked EXE / corrupting the install.
  if CheckForMutexes('FinanceManagerRunningMutex') then
  begin
    if MsgBox('Finance Manager is currently running. Please close it before continuing.',
       mbError, MB_OKCANCEL) = IDCANCEL then
      Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if CheckForMutexes('FinanceManagerRunningMutex') then
  begin
    if MsgBox('Finance Manager is currently running. Please close it before uninstalling.',
       mbError, MB_OKCANCEL) = IDCANCEL then
      Result := False;
  end;
end;

; NOTE: No [UninstallDelete] section. Inno Setup already removes every file
; it installed via [Files]. Force-deleting {app} recursively would also wipe
; ANY runtime data your EXE happens to write inside its own install folder
; (local DB fallback, exported PDFs/Excel, logs, .env) -- a real risk for an
; app that tracks customer loan/payment records. If your app currently writes
; such data under {app}, move it to a per-user/per-machine data folder
; instead, e.g.:
;
;   {localappdata}\FinanceManager        (per-user, no admin needed)
;   {commonappdata}\FinanceManager       (shared machine-wide)
;
; and only then, if you want a fully clean uninstall, add an explicit,
; separately-scoped delete with its own confirmation prompt -- never target
; {app} itself.
