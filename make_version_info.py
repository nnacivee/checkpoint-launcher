# -*- coding: utf-8 -*-
"""Генерирует version_info.txt для PyInstaller (флаг --version-file).

Зачем. Собранный Launcher.exe по умолчанию идёт с ПУСТЫМИ свойствами файла
(правый клик -> Свойства -> Подробно: издатель, описание, версия — все пустые).
Для неподписанного PyInstaller-бинарника это один из главных поводов для
антивирусов (McAfee, Windows Defender/SmartScreen) считать его подозрительным:
«неизвестный издатель, нет описания, самособранный». Заполненные метаданные
не заменяют цифровую подпись, но заметно снижают число ложных срабатываний и
делают файл в глазах эвристики похожим на нормальную программу, а не на зловред.

CI вызывает: python make_version_info.py <версия>   (например 1.65.2)
и передаёт готовый файл в pyinstaller через --version-file=version_info.txt.

Строки специально на латинице (ASCII): некоторые связки PyInstaller/окружения
CI спотыкаются на кириллице в version-файле, а надёжность сборки тут важнее
локализации свойств файла. Русский интерфейс это никак не затрагивает."""

import sys

COMPANY = "Industrial Horizon"
PRODUCT = "Industrial Horizon Launcher"
DESCRIPTION = "Industrial Horizon Minecraft Launcher"
COPYRIGHT = "(c) Industrial Horizon"
ORIGINAL_FILENAME = "Launcher.exe"
INTERNAL_NAME = "Launcher"

TEMPLATE = """\
# UTF-8
#
# Сгенерировано make_version_info.py — вручную не редактировать.
# Формат метаданных версии для PyInstaller (--version-file).
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v0}, {v1}, {v2}, {v3}),
    prodvers=({v0}, {v1}, {v2}, {v3}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'040904B0',
          [
            StringStruct(u'CompanyName', u'{company}'),
            StringStruct(u'FileDescription', u'{description}'),
            StringStruct(u'FileVersion', u'{version}'),
            StringStruct(u'InternalName', u'{internal}'),
            StringStruct(u'LegalCopyright', u'{copyright}'),
            StringStruct(u'OriginalFilename', u'{original}'),
            StringStruct(u'ProductName', u'{product}'),
            StringStruct(u'ProductVersion', u'{version}')
          ])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""


def version_tuple(version):
    parts = []
    for token in str(version).split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return parts[:4]


def render(version):
    v = version_tuple(version)
    return TEMPLATE.format(
        v0=v[0], v1=v[1], v2=v[2], v3=v[3],
        version=version,
        company=COMPANY,
        product=PRODUCT,
        description=DESCRIPTION,
        copyright=COPYRIGHT,
        original=ORIGINAL_FILENAME,
        internal=INTERNAL_NAME,
    )


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"
    out = sys.argv[2] if len(sys.argv) > 2 else "version_info.txt"
    text = render(version)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("version_info.txt written for version %s -> %s" % (version, out))


if __name__ == "__main__":
    main()
