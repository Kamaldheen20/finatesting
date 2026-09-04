; ============================
; Finance Management Installer
; ============================

#define MyAppName "Finance Management System"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Your Company"
#define MyAppExeName "Finance.exe"

[Setup]
AppId={{A8D7F2A5-1234-4567-89AB-123456789ABC}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

OutputDir=Output
OutputBaseFilename=Finance_Setup

Compression=lzma2
SolidCompression=yes

WizardStyle=modern

PrivilegesRequired=admin

ArchitecturesInstallIn64BitMode=x64

DisableProgramGroupPage=yes

AllowNoIcons=yes

UninstallDisplayIcon={app}\{#MyAppExeName}

SetupIconFile=logo.ico



VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create Desktop Shortcut"; GroupDescription: "Additional Shortcuts:"; Flags: unchecked

[Files]


Source: "dist\FinanceManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]

Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]

Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]

Type: filesandordirs; Name: "{app}"

[Code]

function InitializeSetup(): Boolean;
begin
  Result := True;
end;