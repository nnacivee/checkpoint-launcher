; Установщик Checkpoint (Inno Setup).
;
; Зачем он вообще появился. Раньше лаунчер собирался одним .exe (--onefile).
; Такой файл при КАЖДОМ запуске распаковывает Python и все библиотеки во
; временную папку (Temp\_MEIxxxx) и грузит их оттуда. Сразу после обновления
; антивирус проверяет только что записанный файл, распаковка спотыкается — и
; вылезает "Failed to load Python DLL python312.dll". Задержки и пробный
; запуск это не лечили: момент окончания проверки антивирусом нам неподвластен.
;
; Здесь лаунчер ставится папкой (--onedir): при запуске НИЧЕГО не
; распаковывается, DLL просто лежат рядом. Ошибка уходит по построению, старт
; быстрее, и антивирусы ругаются заметно меньше — самораспаковка была главной
; причиной их подозрений.
;
; Ставим в {localappdata} и PrivilegesRequired=lowest — тогда Windows не
; спрашивает права администратора, окно UAC не появляется.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Checkpoint"
#define MyAppExeName "Launcher.exe"

[Setup]
AppId={{8E4B1F2A-6C3D-4A7E-9B15-2D8F0A3C7E51}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Checkpoint
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=installer_out
OutputBaseFilename=CheckpointSetup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Обновление ставится поверх работающего лаунчера — просим Windows закрыть его
; сами, иначе файлы окажутся заняты и установка упадёт.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "dist\Launcher\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; skipifsilent — при тихом обновлении лаунчер перезапускает себя сам.
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent
