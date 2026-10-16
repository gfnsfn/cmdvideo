; cmdvideo 安装包脚本（Inno Setup 6）
; 编译：ISCC.exe cmdvideo_setup.iss
;
; 会做的事：
;   1. 装到 %LOCALAPPDATA%\Programs\cmdvideo\（per-user，不需要管理员）
;   2. 建开始菜单快捷方式 + 可选桌面快捷方式
;   3. 注册「卸载 cmdvideo」（控制面板/设置里能看到）
;   4. 提供 .mp4/.mkv 等视频格式的「用 cmdvideo 打开」（可选）

#define MyAppName "cmdvideo"
#define MyAppVersion "1.7.0"
#define MyAppPublisher "cmdvideo"
#define MyAppExeName "cmdvideo.exe"

[Setup]
AppId={{8F3A2C41-7B9E-4D62-A5C8-1E4F0B93D7A1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 装到用户目录，避免要管理员权限
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer_out
OutputBaseFilename=cmdvideo_setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableDirPage=no
AllowNoIcons=yes

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce
Name: "assoc"; Description: "把视频文件关联到 cmdvideo（右键「打开方式」里会出现）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "dist\cmdvideo.exe";      DestDir: "{app}"; Flags: ignoreversion
Source: "ffmpeg.exe";             DestDir: "{app}"; Flags: ignoreversion
Source: "demo.mp4";               DestDir: "{app}"; Flags: ignoreversion
Source: "README.md";              DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";              Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}";          Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 cmdvideo"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
; 注：卸载后目录里会剩下一个 unins000.exe。
; 这是 Inno Setup 的标准行为 —— 卸载程序正在运行，删不掉自己，
; 系统把它排进重启删除队列，下次重启自动清掉，不是缺陷。

[Registry]
; 「打开方式」里注册，不动系统默认关联（更安全）
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "cmdvideo 字符视频播放器 / 窗口投屏"; Flags: uninsdeletekey; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".mp4"; ValueData: ""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".mkv"; ValueData: ""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".avi"; ValueData: ""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".mov"; ValueData: ""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".webm"; ValueData: ""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".wmv"; ValueData: ""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".flv"; ValueData: ""; Tasks: assoc
Root: HKCU; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; ValueType: string; ValueName: ".m4v"; ValueData: ""; Tasks: assoc
