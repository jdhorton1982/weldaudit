; Inno Setup script for WeldAudit.
;
;     Build.bat installer          (or: ISCC installer\WeldAudit.iss)
;
; Produces dist\WeldAudit-Setup.exe from the folder build in dist\WeldAudit.
;
; PER-USER, and that is the whole point of the settings below. WeldAudit runs
; on managed the operator laptops whose users are not administrators, so an
; installer that asks for elevation is an installer nobody can run. With
; PrivilegesRequired=lowest the program lands in
;
;     %LOCALAPPDATA%\Programs\WeldAudit
;
; no UAC prompt appears, nothing is written outside the user's own profile,
; and it still gets a Start Menu entry, a Desktop shortcut and a proper
; listing in Settings > Apps so it can be removed the ordinary way.
;
; The folder build is installed rather than the single exe: it starts in a
; couple of seconds instead of half a minute, it does not unpack 300 MB into
; %TEMP% on every launch, and it is the shape the in-app updater can replace.

#define AppName       "WeldAudit"
#define AppPublisher  "Jacob Horton"
#define AppExe        "WeldAudit.exe"
#define BuildDir      "..\dist\WeldAudit"

; Passed in by Build.bat so the installer, the program and any release it
; publishes all agree. Falls back for a hand-run compile.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change AppId: it is what lets an upgrade replace the previous install
; instead of sitting beside it, and what Settings > Apps keys the entry on.
AppId={{6E5C6E7A-2F1B-4E5E-9E9A-2B0C7A5D41C2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}

PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}

OutputDir=..\dist
OutputBaseFilename=WeldAudit-Setup
SetupIconFile=..\weldaudit.ico
WizardStyle=modern
; The payload is ~300 MB of mostly-compressible DLLs and models; solid LZMA2
; gets it to about a third of that, and this is downloaded rarely.
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

; An update installed over a running copy would leave half the old build in
; place. Inno asks to close it rather than failing on a locked file.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; \
  GroupDescription: "Shortcuts:"

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; \
  WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; \
  Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Written by the program after it is installed, so Inno does not know about
; them and would otherwise leave the folder behind.
Type: files; Name: "{app}\weldaudit-version.txt"
Type: filesandordirs; Name: "{app}.old"
Type: filesandordirs; Name: "{app}.new"

[Code]
{ What the uninstaller must NOT touch: everything the auditor has built up.
  The database, the page readings and the typed corrections live in
  %USERPROFILE%\.weldaudit and are the expensive part -- most of the readings
  were paid for a page at a time. Removing the program must never remove
  those, so nothing here goes near that folder, and the message says so. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('WeldAudit has been removed.'#13#10#13#10 +
           'Your audits, page readings and corrections have been left alone, ' +
           'in:'#13#10#13#10'    ' + ExpandConstant('{%USERPROFILE}') +
           '\.weldaudit'#13#10#13#10 +
           'Delete that folder by hand if you want them gone too.',
           mbInformation, MB_OK);
end;
