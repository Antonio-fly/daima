#define MyAppName "牛马便签"
#define MyAppVersion "1.0.0"
#define MyAppExeName "NiuMaNotes.exe"
[Setup]
AppId={{00F831B8-CA58-4866-92A8-23BA2B2013A8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\NiuMaNotes
DefaultGroupName={#MyAppName}
OutputDir=dist\installer
OutputBaseFilename=牛马便签_安装程序_v1.0.0
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
[Files]
Source: "dist\NiuMaNotes\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他任务："
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动牛马便签"; Flags: nowait postinstall skipifsilent
