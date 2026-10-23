; Inno Setup Script for SmarterGamers Vertical Blur Editor
; ####################################################################

#define MyAppName "SmarterGamers Vertical Blur Editor"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "SmarterGamers"
#define MyAppURL "https://github.com/SmarterGamers/SmarterGamers-Vertical-Blur"
#define MyAppExeName "SmarterGamersVerticalBlurEditor.exe"

[Setup]
AppId={{9F5F1B58-47BE-45D3-868E-32E16DF45F67}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\SmarterGamers\VerticalBlurEditor
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist\releases
OutputBaseFilename=SmarterGamers.Vertical.Blur.Editor.Setup.{#MyAppVersion}
SetupIconFile=..\assets\app_icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "contextmenu"; Description: "Add 'Convert to 9:16 Short' to video right-click context menu"; GroupDescription: "Windows Integration:"

[Files]
Source: "..\dist\SmarterGamersVerticalBlurEditor.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\app_icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\app_icon.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app_icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app_icon.ico"; Tasks: desktopicon

[Registry]
; Context Menu Integration for MP4
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\Shell\SmarterGamersVerticalBlur"; ValueType: string; ValueData: "Convert to 9:16 Short with SmarterGamers"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\Shell\SmarterGamersVerticalBlur"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\app_icon.ico"""; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\Shell\SmarterGamersVerticalBlur\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: contextmenu

; Context Menu Integration for MOV
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mov\Shell\SmarterGamersVerticalBlur"; ValueType: string; ValueData: "Convert to 9:16 Short with SmarterGamers"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mov\Shell\SmarterGamersVerticalBlur"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\app_icon.ico"""; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mov\Shell\SmarterGamersVerticalBlur\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: contextmenu

; Context Menu Integration for MKV
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mkv\Shell\SmarterGamersVerticalBlur"; ValueType: string; ValueData: "Convert to 9:16 Short with SmarterGamers"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mkv\Shell\SmarterGamersVerticalBlur"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\app_icon.ico"""; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mkv\Shell\SmarterGamersVerticalBlur\command"; ValueType: string; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: contextmenu

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
