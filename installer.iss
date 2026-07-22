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

#define MyAppInternalName "Checkpoint"
#define MyAppDisplayName "Industrial Horizon"
#define MyAppExeName "Launcher.exe"

[Setup]
AppId={{8E4B1F2A-6C3D-4A7E-9B15-2D8F0A3C7E51}
AppName={#MyAppDisplayName}
AppVersion={#MyAppVersion}
AppPublisher=Industrial Horizon
; Метаданные САМОГО установщика (CheckpointSetup.exe). Без них у файла пустые
; свойства (издатель/описание/версия) — а для неподписанного .exe это один из
; поводов для McAfee/Defender считать его подозрительным. Заполняем, чтобы
; ложных срабатываний было меньше. AppId и DefaultDirName НЕ трогаем: их
; смена завела бы вторую копию установки и сломала обновление поверх. AppName
; теперь является только видимой маркой Industrial Horizon.
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany=Industrial Horizon
VersionInfoDescription=Industrial Horizon Launcher Setup
VersionInfoProductName=Industrial Horizon Launcher
VersionInfoProductVersion={#MyAppVersion}
VersionInfoOriginalFileName=CheckpointSetup.exe
VersionInfoCopyright=(c) Industrial Horizon
DefaultDirName={localappdata}\{#MyAppInternalName}
DefaultGroupName={#MyAppDisplayName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=installer_out
OutputBaseFilename=CheckpointSetup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppDisplayName}
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

[InstallDelete]
; onedir обновляется целиком. Удаляем старый runtime перед копированием, чтобы
; устаревшие DLL/модули из предыдущей версии не оставались в _internal.
Type: filesandordirs; Name: "{app}\_internal"
; После обновления не оставляем рядом старые ярлыки Checkpoint. Сама папка
; установки и AppId сохраняются, поэтому пользовательские данные не теряются.
Type: files; Name: "{userdesktop}\Checkpoint.lnk"
Type: files; Name: "{userprograms}\Checkpoint\Checkpoint.lnk"
Type: files; Name: "{userprograms}\Checkpoint\Удалить Checkpoint.lnk"

[Icons]
Name: "{userdesktop}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Удалить {#MyAppDisplayName}"; Filename: "{uninstallexe}"

[Run]
; skipifsilent — намеренно: при ТИХОМ обновлении лаунчер НЕ открывается заново.
; Обновление применяется в момент, когда игрок сам закрывает лаунчер (см.
; on_close в launcher.py), поэтому перезапускать его не нужно и не нужно, чтобы
; он выскакивал обратно. При обычной установке мастером (не тихой) галочка
; «Запустить» остаётся, как и раньше.
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppDisplayName}"; \
    Flags: nowait postinstall skipifsilent
