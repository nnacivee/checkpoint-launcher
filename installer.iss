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
#define MyAppDisplayName "GloryCraft"
#define MyAppExeName "Launcher.exe"

[Setup]
AppId={{8E4B1F2A-6C3D-4A7E-9B15-2D8F0A3C7E51}
AppName={#MyAppDisplayName}
AppVersion={#MyAppVersion}
AppPublisher=GloryCraft
; Метаданные САМОГО установщика (CheckpointSetup.exe). Без них у файла пустые
; свойства (издатель/описание/версия) — а для неподписанного .exe это один из
; поводов для McAfee/Defender считать его подозрительным. Заполняем, чтобы
; ложных срабатываний было меньше. AppId и DefaultDirName НЕ трогаем: их
; смена завела бы вторую копию установки и сломала обновление поверх. AppName
; теперь является только видимой маркой Industrial Horizon.
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany=GloryCraft
VersionInfoDescription=GloryCraft Launcher Setup
VersionInfoProductName=GloryCraft Launcher
VersionInfoProductVersion={#MyAppVersion}
VersionInfoOriginalFileName=CheckpointSetup.exe
VersionInfoCopyright=(c) GloryCraft
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
; Смена марки: старые ярлыки Industrial Horizon убираем, иначе у игрока
; на рабочем столе останутся два значка на один и тот же лаунчер.
Type: files; Name: "{userdesktop}\Industrial Horizon.lnk"
Type: files; Name: "{userprograms}\Industrial Horizon\Industrial Horizon.lnk"
Type: files; Name: "{userprograms}\Industrial Horizon\Удалить Industrial Horizon.lnk"

[Icons]
Name: "{userdesktop}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppDisplayName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Удалить {#MyAppDisplayName}"; Filename: "{uninstallexe}"

[Run]
; При обычной установке оставляем привычную галочку «Запустить».
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppDisplayName}"; \
    Flags: nowait postinstall skipifsilent
; Автообновление запускает Setup с /VERYSILENT. После успешной замены файлов
; новое окно должно открыться само, иначе игрок видит установленную версию, но
; думает, что лаунчер сломался. /NOAUTOLAUNCH=1 оставлен для CI и поддержки.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; \
    Check: ShouldLaunchAfterSilentInstall

[Code]
function ShouldLaunchAfterSilentInstall(): Boolean;
begin
  Result :=
    WizardSilent and
    (CompareText(ExpandConstant('{param:NOAUTOLAUNCH|0}'), '1') <> 0);
end;
