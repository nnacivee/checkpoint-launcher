"""
Простой лаунчер Minecraft со своей сборкой модов.

Как это работает:
  1. Пользователь вводит ник -> офлайн-авторизация (без Microsoft-аккаунта).
  2. При первом запуске (или если вышло обновление сборки) лаунчер:
       - скачивает и ставит нужную версию Minecraft;
       - скачивает и ставит Fabric нужной версии;
       - скачивает zip с вашей сборкой модов и распаковывает его в
         папку экземпляра (mods/, config/, resourcepacks/ и т.д.);
  3. Запускает игру.

Настройки для сборщика лаунчера (то есть для вас) — блок CONFIG ниже.
Больше нигде в коде ничего менять не нужно.
"""

import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import minecraft_launcher_lib as mll
except ImportError:
    print("Не найдена библиотека minecraft-launcher-lib.")
    print("Установите её командой: pip install minecraft-launcher-lib")
    sys.exit(1)

# Pillow нужен только для миниатюр модов в меню опциональных модов (иконки с
# Modrinth бывают в формате webp, который штатный tkinter не умеет). Если
# библиотеки нет — иконки просто не покажутся, а меню продолжит работать.
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    _PIL_OK = True
except Exception:
    _PIL_OK = False


# =========================== Мини-NBT ===========================
# Minecraft хранит список серверов (вкладка "Множественная игра") в файле
# servers.dat в формате NBT. Готовых библиотек в стандартном Python нет, а
# тянуть внешнюю зависимость ради одного файла ни к чему — здесь минимальная
# самодостаточная реализация ровно того, что нужно: прочитать существующий
# список серверов (если он уже есть) и дописать в него свой сервер, не
# трогая остальные записи игрока.

_NBT_END = 0
_NBT_BYTE = 1
_NBT_SHORT = 2
_NBT_INT = 3
_NBT_LONG = 4
_NBT_FLOAT = 5
_NBT_DOUBLE = 6
_NBT_BYTE_ARRAY = 7
_NBT_STRING = 8
_NBT_LIST = 9
_NBT_COMPOUND = 10
_NBT_INT_ARRAY = 11
_NBT_LONG_ARRAY = 12


class NBTCompound:
    """Компаунд-тег NBT: список пар (имя -> (тип, значение)), сохраняющий
    порядок полей — нужен, чтобы при перезаписи файла не терялись и не
    переставлялись местами данные, которые мы сами не создавали."""

    def __init__(self):
        self.order = []
        self.fields = {}

    def set(self, name, tag_type, value):
        if name not in self.fields:
            self.order.append(name)
        self.fields[name] = (tag_type, value)

    def get_value(self, name, default=None):
        entry = self.fields.get(name)
        return entry[1] if entry else default


class _NBTReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _read(self, fmt):
        value = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += struct.calcsize(fmt)
        return value

    def _read_bytes(self, n):
        value = self.data[self.pos:self.pos + n]
        self.pos += n
        return value

    def _read_string(self):
        length = self._read(">H")
        return self._read_bytes(length).decode("utf-8", errors="replace")

    def _read_payload(self, tag_type):
        if tag_type == _NBT_BYTE:
            return self._read(">b")
        if tag_type == _NBT_SHORT:
            return self._read(">h")
        if tag_type == _NBT_INT:
            return self._read(">i")
        if tag_type == _NBT_LONG:
            return self._read(">q")
        if tag_type == _NBT_FLOAT:
            return self._read(">f")
        if tag_type == _NBT_DOUBLE:
            return self._read(">d")
        if tag_type == _NBT_BYTE_ARRAY:
            n = self._read(">i")
            return list(self._read_bytes(n))
        if tag_type == _NBT_STRING:
            return self._read_string()
        if tag_type == _NBT_LIST:
            elem_type = self._read(">b")
            n = self._read(">i")
            return (elem_type, [self._read_payload(elem_type) for _ in range(n)])
        if tag_type == _NBT_COMPOUND:
            compound = NBTCompound()
            while True:
                t = self._read(">b")
                if t == _NBT_END:
                    break
                name = self._read_string()
                compound.set(name, t, self._read_payload(t))
            return compound
        if tag_type == _NBT_INT_ARRAY:
            n = self._read(">i")
            return [self._read(">i") for _ in range(n)]
        if tag_type == _NBT_LONG_ARRAY:
            n = self._read(">i")
            return [self._read(">q") for _ in range(n)]
        raise ValueError("Неизвестный тип NBT-тега: %d" % tag_type)

    def read_named_tag(self):
        t = self._read(">b")
        if t == _NBT_END:
            return None, None, None
        name = self._read_string()
        return t, name, self._read_payload(t)


class _NBTWriter:
    def __init__(self):
        self.out = bytearray()

    def _write(self, fmt, value):
        self.out += struct.pack(fmt, value)

    def _write_string(self, value: str):
        data = value.encode("utf-8")
        self._write(">H", len(data))
        self.out += data

    def _write_payload(self, tag_type, value):
        if tag_type == _NBT_BYTE:
            self._write(">b", value)
        elif tag_type == _NBT_SHORT:
            self._write(">h", value)
        elif tag_type == _NBT_INT:
            self._write(">i", value)
        elif tag_type == _NBT_LONG:
            self._write(">q", value)
        elif tag_type == _NBT_FLOAT:
            self._write(">f", value)
        elif tag_type == _NBT_DOUBLE:
            self._write(">d", value)
        elif tag_type == _NBT_BYTE_ARRAY:
            self._write(">i", len(value))
            self.out += bytes(b & 0xFF for b in value)
        elif tag_type == _NBT_STRING:
            self._write_string(value)
        elif tag_type == _NBT_LIST:
            elem_type, items = value
            self._write(">b", elem_type)
            self._write(">i", len(items))
            for item in items:
                self._write_payload(elem_type, item)
        elif tag_type == _NBT_COMPOUND:
            for name in value.order:
                t, v = value.fields[name]
                self._write(">b", t)
                self._write_string(name)
                self._write_payload(t, v)
            self._write(">b", _NBT_END)
        elif tag_type == _NBT_INT_ARRAY:
            self._write(">i", len(value))
            for v in value:
                self._write(">i", v)
        elif tag_type == _NBT_LONG_ARRAY:
            self._write(">i", len(value))
            for v in value:
                self._write(">q", v)
        else:
            raise ValueError("Неизвестный тип NBT-тега: %d" % tag_type)

    def write_named_tag(self, tag_type, name, value):
        self._write(">b", tag_type)
        self._write_string(name)
        self._write_payload(tag_type, value)
# ================================================================


# =========================== CONFIG ===========================
# Здесь настраиваете лаунчер конкретно под вашу сборку.

CONFIG = {
    # Имя вашей сборки — будет папкой на диске у игрока и заголовком окна
    "PACK_NAME": "Checkpoint",

    # Версия Minecraft, под которую собраны моды
    "MC_VERSION": "1.21.1",

    # Загрузчик модов. Один из: "neoforge", "forge", "fabric", "quilt"
    "MOD_LOADER": "neoforge",

    # Версия загрузчика модов (NeoForge/Forge/Fabric/Quilt). Можно оставить
    # "" — тогда возьмётся последняя доступная для указанной MC_VERSION.
    "LOADER_VERSION": "",

    # Прямая ссылка на zip-архив с вашей сборкой модов.
    # Внутри архива должны лежать папки mods/, config/, resourcepacks/ и т.п.
    # (то есть содержимое, которое нужно положить прямо в папку .minecraft
    # экземпляра). Заливать можно на GitHub Releases, Яндекс.Диск (с прямой
    # ссылкой на скачивание), собственный сервер и т.д.
    "MODPACK_URL": "https://github.com/nnacivee/checkpoint-launcher/releases/download/modpack/modpack.zip",

    # Версия сборки модов "по умолчанию" — используется, только если ниже
    # НЕ указана MODPACK_VERSION_URL. Если её увеличить, тоже нужно заново
    # собирать .exe (см. MODPACK_VERSION_URL — так делать не обязательно).
    "MODPACK_VERSION": 3,

    # Моды, которые нужно убрать из сборки, даже если они лежат в архиве
    # modpack.zip. Лаунчер удаляет их из mods/ при каждом запуске — так можно
    # выкинуть мод, не перезаливая весь архив (он весит сотни мегабайт):
    # достаточно дописать сюда кусок имени файла. Сравнение без учёта регистра.
    # Осторожно: "konkrete" НЕ трогаем — он нужен моду Just Zoom.
    "REMOVED_MODS": ["fancymenu", "melody"],

    # (необязательно, но удобно) Ссылка на маленький текстовый файл, в
    # котором лежит только число — версия сборки. Если её указать, лаунчер
    # будет проверять актуальность модов через интернет при каждом запуске,
    # и вам НЕ придётся пересобирать .exe каждый раз, когда вы обновляете
    # моды — достаточно просто перезалить modpack.zip и увеличить число в
    # этом текстовом файле. Оставьте "" чтобы не использовать эту функцию.
    "MODPACK_VERSION_URL": "",

    # Сколько оперативной памяти выделять игре по умолчанию (в мегабайтах).
    # Игрок сможет изменить это значение ползунком в самом лаунчере —
    # выбор запоминается и в следующий раз подставляется автоматически.
    "MEMORY_MB": 4096,

    # Минимум и максимум для ползунка ОЗУ (в мегабайтах). Верхняя граница
    # всё равно не может превысить объём ОЗУ, реально установленной на
    # компьютере игрока — лаунчер сам её подрежет, если нужно.
    "MEMORY_MIN_MB": 1024,
    "MEMORY_MAX_MB": 32768,

    # Ссылка на ваш Discord-сервер (кнопка в лаунчере). Оставьте "", чтобы
    # кнопку не показывать.
    "DISCORD_URL": "https://discord.gg/rN22JGV9C",

    # ------------------------- ВЕРСИЯ ЛАУНЧЕРА -------------------------
    # Показывается мелким текстом внизу окна лаунчера, а по клику
    # открывается история изменений (список ниже). При каждой доработке
    # лаунчера (не сборки модов — за неё отвечает MODPACK_VERSION выше)
    # увеличивайте LAUNCHER_VERSION и добавляйте новую запись в начало
    # списка LAUNCHER_CHANGELOG — тогда друзья всегда будут видеть, что
    # именно поменялось, просто открыв "что нового" в лаунчере.
    "LAUNCHER_VERSION": "1.9.4",

    # ------------------- АВТОПРОВЕРКА ОБНОВЛЕНИЙ ЛАУНЧЕРА -------------------
    # Если заполнить это (после того как заведёте GitHub-репозиторий с
    # автосборкой — см. инструкцию), лаунчер сам будет тихо проверять при
    # запуске, не вышла ли версия новее, и покажет ссылку "скачать" внизу
    # окна. Формат: "имя_пользователя/название_репозитория", например
    # "ivanov/checkpoint-launcher". Оставьте "" чтобы выключить проверку —
    # тогда просто ничего не будет происходить, ошибок не будет.
    "GITHUB_REPO": "nnacivee/checkpoint-launcher",

    "LAUNCHER_CHANGELOG": [
        {
            "version": "1.9.4",
            "date": "14 июля 2026",
            "changes": [
                "Сборка качается в несколько потоков — заметно быстрее.",
            ],
        },
        {
            "version": "1.9.3",
            "date": "14 июля 2026",
            "changes": [
                "Стеклянный фон: цвет мягко просвечивает сквозь панель, "
                "появились тень и световая грань. Работает и в светлой теме.",
            ],
        },
        {
            "version": "1.9.2",
            "date": "14 июля 2026",
            "changes": [
                "Обновление больше не оставляет файл _update.bat на рабочем столе.",
                "Убрана ошибка python-DLL после обновления: лаунчер просто "
                "просит открыть его заново.",
            ],
        },
        {
            "version": "1.9.1",
            "date": "14 июля 2026",
            "changes": [
                "Обрыв связи при скачивании больше не роняет установку: закачка "
                "продолжается с места обрыва и повторяется автоматически. "
                "Заново качать сборку не нужно.",
            ],
        },
        {
            "version": "1.9.0",
            "date": "14 июля 2026",
            "changes": [
                "Новая иконка сборки — она же на окне и в панели задач.",
                "Спокойный чистый интерфейс: матовая панель с мягким свечением, "
                "синий акцент из иконки. Жёлтый убран.",
                "Кнопка Discord теперь со своим значком.",
                "Готовые ресурс-паки видны сразу общим списком.",
            ],
        },
        {
            "version": "1.8.0",
            "date": "14 июля 2026",
            "changes": [
                "Готовые ресурс-паки в один клик: Faithful 32x, Better Leaves, "
                "Low On Fire и Fast Better Grass.",
                "Fast Better Grass включается автоматически вместе с режимом "
                "для слабых ПК.",
                "Оперативная память и режим для слабых ПК переехали в настройки "
                "(шестерёнка) — на главном окне осталась короткая сводка.",
                "Полоса загрузки больше не сбрасывается по нескольку раз: теперь "
                "одна шкала на весь запуск с номером шага (например, «Шаг 3/5 · "
                "NeoForge — 40%»).",
            ],
        },
        {
            "version": "1.7.0",
            "date": "14 июля 2026",
            "changes": [
                "Появился менеджер ресурс-паков: установка из ZIP, превью и "
                "название пака, включение, отключение и удаление.",
                "Лаунчер снова полностью скрывается на время игры и возвращается "
                "сам, когда вы закрываете Minecraft.",
                "После обновления лаунчер открывается сам.",
                "Обновление сборки модов больше не удаляет ваши ресурс-паки "
                "и шейдеры.",
            ],
        },
        {
            "version": "1.6.0",
            "date": "14 июля 2026",
            "changes": [
                "Появился выбор папки установки (кнопка с шестерёнкой): игру "
                "можно поставить на любой диск, например D:\\Games\\IC3.",
                "Перенос игры в другую папку сохраняет все миры, настройки и "
                "скриншоты.",
            ],
        },
        {
            "version": "1.5.4",
            "date": "14 июля 2026",
            "changes": [
                "Окно больше не мигает у края экрана при запуске — сразу "
                "открывается по центру.",
                "Лаунчер надёжно сворачивается в панель задач при старте игры.",
            ],
        },
        {
            "version": "1.5.3",
            "date": "14 июля 2026",
            "changes": [
                "Повторный клик по ярлыку теперь возвращает уже открытое окно "
                "лаунчера, а не выдаёт ошибку.",
                "Во время игры лаунчер сворачивается в панель задач, а не "
                "исчезает — окно больше не «теряется».",
                "Окно всегда открывается по центру экрана и возвращается на "
                "него, если уехало за границы.",
                "После закрытия окна процесс гарантированно завершается.",
            ],
        },
        {
            "version": "1.5.2",
            "date": "14 июля 2026",
            "changes": [
                "FancyMenu окончательно убран: лаунчер теперь сам вычищает "
                "ненужные моды из сборки при запуске.",
            ],
        },
        {
            "version": "1.5.1",
            "date": "13 июля 2026",
            "changes": [
                "Надёжное обновление: убрал мелькавшее чёрное окно и автоперезапуск "
                "(из-за него новая версия иногда падала с ошибкой python-DLL). "
                "Теперь обновление молча ставится, а лаунчер просит открыть его снова.",
            ],
        },
        {
            "version": "1.5.0",
            "date": "13 июля 2026",
            "changes": [
                "Убрал мод FancyMenu из сборки.",
                "Меню опциональных модов переделано: иконки модов и компактные "
                "карточки. Моды, которых нет в сборке (например, InvMove), теперь "
                "докачиваются с Modrinth прямо при включении галочки.",
            ],
        },
        {
            "version": "1.4.9",
            "date": "13 июля 2026",
            "changes": [
                "Полировка интерфейса: убрал лишние вертикальные отступы, вернул "
                "строку версии внизу окна.",
            ],
        },
        {
            "version": "1.4.8",
            "date": "13 июля 2026",
            "changes": [
                "Надёжное обновление: перед заменой лаунчер проверяет, что новый "
                "файл скачался целиком, и делает паузу перед запуском — чтобы не "
                "получить повреждённый .exe.",
            ],
        },
        {
            "version": "1.4.7",
            "date": "13 июля 2026",
            "changes": [
                "Новый дизайн: квадратное окно, обновлённая графитово-янтарная "
                "палитра, более округлая карточка и заметная рамка.",
            ],
        },
        {
            "version": "1.4.6",
            "date": "13 июля 2026",
            "changes": [
                "Обновление лаунчера теперь скачивается и ставится прямо в окне "
                "(без перехода на сайт), с прогрессом и автоперезапуском.",
            ],
        },
        {
            "version": "1.4.5",
            "date": "13 июля 2026",
            "changes": [
                "Сборка модов переехала с Dropbox на GitHub (быстрее и надёжнее). "
                "Состав модов не изменился.",
            ],
        },
        {
            "version": "1.4.4",
            "date": "12 июля 2026",
            "changes": [
                "Лаунчер теперь сам скачивает набор качественных клиентских модов "
                "(Sound Physics, Dynamic FPS, Chat Heads, Controlling) — ставить вручную не нужно",
            ],
        },
        {
            "version": "1.4.3",
            "date": "12 июля 2026",
            "changes": [
                "Кнопка \"Играть (тест)\" теперь ставит в клиент ровно те моды, "
                "что лежат на локальном тестовом сервере — для перебора сборки",
            ],
        },
        {
            "version": "1.4.2",
            "date": "12 июля 2026",
            "changes": [
                "Добавлена кнопка \"Играть (тест)\" — быстрый заход на локальный "
                "тестовый сервер (localhost) для проверки сборки перед заливкой на хостинг",
            ],
        },
        {
            "version": "1.4.1",
            "date": "12 июля 2026",
            "changes": [
                "Исправлено: можно было случайно открыть несколько копий лаунчера "
                "одновременно — теперь вторая копия просто не запускается",
            ],
        },
        {
            "version": "1.4.0",
            "date": "12 июля 2026",
            "changes": [
                "Кнопка \"Играть\" теперь сразу подключает к серверу Checkpoint, минуя "
                "главное меню игры",
            ],
        },
        {
            "version": "1.3.0",
            "date": "12 июля 2026",
            "changes": [
                "Лаунчер сам проверяет при запуске, не вышла ли новая версия, и "
                "показывает ссылку на скачивание внизу окна",
                "Добавлена автосборка через GitHub Actions — обновление launcher.py "
                "в репозитории само публикует новый .exe, заново заливать на "
                "Google Диск/Dropbox не нужно",
            ],
        },
        {
            "version": "1.2.2",
            "date": "12 июля 2026",
            "changes": [
                "Режим \"для слабых ПК\" теперь применяется сразу же, даже на самом "
                "первом запуске игры — второй вход больше не нужен",
            ],
        },
        {
            "version": "1.2.1",
            "date": "12 июля 2026",
            "changes": [
                "Исправлено: режим \"для слабых ПК\" теперь ещё и выключает шейдеры "
                "(раньше выключал только графику в options.txt, а уже выбранный "
                "шейдер так и оставался включённым)",
            ],
        },
        {
            "version": "1.2.0",
            "date": "12 июля 2026",
            "changes": [
                "Лаунчер теперь сворачивается на время игры и открывается снова, "
                "когда вы закрываете Minecraft",
                "Иконка окна и панели задач самой игры (не только лаунчера) тоже "
                "меняется на жетон с буквой C — через мод Custom Window Title, "
                "который лаунчер ставит и настраивает сам",
            ],
        },
        {
            "version": "1.1.4",
            "date": "12 июля 2026",
            "changes": [
                "Новая иконка лаунчера — шестиугольный жетон с буквой C вместо шестерёнки",
            ],
        },
        {
            "version": "1.1.3",
            "date": "12 июля 2026",
            "changes": [
                "Исправлен баг: несколько нажатий на \"Играть\" запускали несколько "
                "копий игры одновременно — теперь кнопка заблокирована, пока игра "
                "не будет закрыта",
            ],
        },
        {
            "version": "1.1.2",
            "date": "10 июля 2026",
            "changes": [
                "Исправлена ошибка установки NeoForge на компьютерах без отдельно "
                "установленной Java (\"returned non-zero exit status 1\") — теперь "
                "используется Java, которую лаунчер уже скачал для самой игры",
            ],
        },
        {
            "version": "1.1.1",
            "date": "10 июля 2026",
            "changes": [
                "Исправлен баг: если иконки почему-то не находились, кнопка "
                "переключения темы раздувалась на весь экран и ломала весь интерфейс",
            ],
        },
        {
            "version": "1.1.0",
            "date": "10 июля 2026",
            "changes": [
                "Все эмодзи-иконки заменены на нарисованные значки в цветах сборки — "
                "выглядят одинаково на любом компьютере",
                "Новое окно \"Список модов сборки\" — категории модов со скриншота-постера",
                "Всплывающие подсказки при наведении на кнопки-иконки",
            ],
        },
        {
            "version": "1.0.0",
            "date": "10 июля 2026",
            "changes": [
                "Первая версия с номером и историей изменений в самом лаунчере",
                "Автоматическая закачка 4 шейдеров (Complementary Unbound/Reimagined, "
                "Sildur's Vibrant Lite, Nostalgia) прямо с Modrinth",
                "Добавлены опциональные моды EMI и InvMove",
            ],
        },
        {
            "version": "0.6.0",
            "date": "10 июля 2026",
            "changes": [
                "Индикатор \"сервер онлайн/оффлайн\" рядом с кнопкой Играть",
                "Кнопка \"Починить\" — переустановка Minecraft/модов с нуля без потери "
                "миров и скриншотов",
            ],
        },
        {
            "version": "0.5.0",
            "date": "10 июля 2026",
            "changes": [
                "Своя иконка лаунчера вместо стандартной",
                "Уменьшен размер .exe (без UPX — он оказался нестабильным на некоторых ПК)",
            ],
        },
        {
            "version": "0.4.0",
            "date": "10 июля 2026",
            "changes": [
                "Режим \"для слабых ПК\" — упрощённая графика одной галочкой",
                "Ваш сервер Checkpoint теперь сам добавляется в список серверов",
                "Увеличен максимум ползунка ОЗУ до 32 ГБ",
            ],
        },
        {
            "version": "0.3.0",
            "date": "10 июля 2026",
            "changes": [
                "Новый дизайн интерфейса — тёмная золотая тема, скруглённые формы",
                "Переключатель тёмной/светлой темы",
                "Опциональные моды: возможность включать/выключать часть модов самим игроком",
            ],
        },
        {
            "version": "0.2.0",
            "date": "10 июля 2026",
            "changes": [
                "Переход на NeoForge 1.21.1",
                "Ползунок выбора объёма оперативной памяти",
            ],
        },
        {
            "version": "0.1.0",
            "date": "10 июля 2026",
            "changes": [
                "Первая рабочая версия: ник, скачивание сборки модов, запуск игры",
            ],
        },
    ],

    # Сервер, который лаунчер сам добавляет в список серверов игрока
    # (вкладка "Множественная игра") при каждом запуске, если его там ещё
    # нет — вручную ничего добавлять не нужно. Остальные серверы, которые
    # игрок добавил сам, никак не затрагиваются. Оставьте "ip": "" чтобы
    # выключить эту функцию.
    "PINNED_SERVER": {
        "name": "Checkpoint",
        "ip": "95.216.30.64:25760",
    },

    # Если True — кнопка "Играть" сразу подключает игрока к серверу из
    # PINNED_SERVER выше, минуя главное меню (используется штатная
    # возможность самого Minecraft, надёжно работает на любой версии).
    # Поставьте False, если хотите, чтобы игрок сам заходил в мультиплеер
    # вручную (например, если часто нужен одиночный мир).
    "AUTO_JOIN_SERVER": True,

    # ------------------------- ШЕЙДЕРЫ (АВТО-СКАЧИВАНИЕ) -------------------
    # Шейдеры, которые лаунчер САМ скачивает с Modrinth (открытое API, без
    # ключей) прямо в папку shaderpacks/ — руками ничего скачивать и
    # никуда класть не нужно. Каждый скачивается только один раз (при
    # первом запуске после обновления лаунчера) — дальше лаунчер помнит,
    # что уже скачал, и просто ничего не трогает. Если конкретный шейдер
    # не найден для вашей версии Minecraft или Modrinth недоступен —
    # лаунчер тихо это пропустит и не помешает игре запуститься, это не
    # критичная часть.
    #   "slug"           — часть ссылки на страницу шейдера на Modrinth,
    #                       например для https://modrinth.com/shader/nostalgia-shader
    #                       это "nostalgia-shader".
    #   "label"           — название, которое увидит игрок в статусе загрузки.
    #   "prefer_keyword"  — (необязательно) если у шейдера несколько
    #                       вариантов файла в одном релизе (например, у
    #                       Sildur's — Lite/Medium/High/Extreme), укажите
    #                       часть имени файла, которую нужно выбрать.
    "EXTRA_SHADERPACKS": [
        {"slug": "complementary-unbound", "label": "Complementary Shaders (Unbound)"},
        {"slug": "complementary-reimagined", "label": "Complementary Shaders (Reimagined)"},
        {"slug": "sildurs-vibrant-shaders", "label": "Sildur's Vibrant Shaders (Lite)",
         "prefer_keyword": "lite"},
        {"slug": "nostalgia-shader", "label": "Nostalgia Shader"},
    ],

    # ------------------- ДОП. КЛИЕНТСКИЕ МОДЫ (АВТО-СКАЧИВАНИЕ) -------------
    # Качественные ЧИСТО КЛИЕНТСКИЕ моды, которые лаунчер сам скачивает с
    # Modrinth в mods/ каждому игроку (как шейдеры). Скачиваются один раз и
    # кэшируются. Сюда — только клиентские (звук, HUD, интерфейс, перф),
    # которые не нужны на сервере и не хранят состояние в мире.
    #   "slug"  — часть ссылки на страницу мода на Modrinth
    #             (modrinth.com/mod/<slug>).
    #   "label" — что увидит игрок в статусе загрузки.
    "EXTRA_CLIENT_MODS": [
        {"slug": "sound-physics-remastered", "label": "Sound Physics Remastered (реалистичное эхо)"},
        {"slug": "dynamic-fps", "label": "Dynamic FPS (экономия ресурсов в фоне)"},
        {"slug": "chat-heads", "label": "Chat Heads (лицо игрока в чате)"},
        {"slug": "searchables", "label": "Searchables (библиотека для Controlling)"},
        {"slug": "controlling", "label": "Controlling (поиск по клавишам управления)"},
    ],

    # ------------------------- ИКОНКА ОКНА САМОЙ ИГРЫ -------------------------
    # Стандартная иконка Minecraft (травяной блок) в панели задач — это уже
    # не наш лаунчер, а сама игра, и просто так её не поменять. Если True,
    # лаунчер сам скачивает мод "Custom Window Title" (популярный, открытый,
    # NeoForge/Fabric) и настраивает его на файл window_icon.png — тогда и
    # окно, и значок игры в панели задач тоже будут в цветах сборки.
    # Не критично для игры: если Modrinth недоступен или что-то пойдёт не
    # так — просто останется стандартная иконка, игра всё равно запустится.
    "SET_GAME_WINDOW_ICON": True,

    # ------------------------- СПИСОК МОДОВ (ОКНО) -------------------------
    # Чисто витринный список для окна "Список модов сборки" (кнопка со
    # значком списка в панели) — просто показывает игроку, из чего состоит
    # сборка, по категориям. Это ТОЛЬКО текст для показа, ни на что не
    # влияет и не обязано совпадать 1-в-1 с OPTIONAL_MODS. Оставьте {} или
    # уберите ключ, чтобы кнопка не показывалась.
    "MOD_SHOWCASE": {
        "Технологии": [
            "Create + аддоны", "Applied Energistics 2", "Mekanism",
            "Modern Industrialization", "Industrial Foregoing", "Extreme Reactors",
        ],
        "Логистика и хозяйство": [
            "TFMG (The Factory Must Grow)", "Productive Bees",
        ],
        "Мир и приключения": [
            "Oh The Biomes We've Gone", "YUNG's Better Structures",
            "Friends & Foes", "Creeper Overhaul", "Explorify",
        ],
        "Прогресс и квесты": [
            "FTB Quests",
        ],
    },

    # ------------------------- ОПЦИОНАЛЬНЫЕ МОДЫ -------------------------
    # Список модов, которые игрок сможет включать/выключать сам прямо в
    # лаунчере (кнопка "🧩 Моды"). Сюда годятся ТОЛЬКО чисто клиентские
    # моды, не хранящие состояние в мире (миникарты, звуки, HUD, зум и
    # т.п.) — иначе при отключении мода после того как игрок уже
    # взаимодействовал с его блоками/предметами в мире, мир может
    # скраситься. Всё, что добавляет блоки/предметы/машины, должно
    # оставаться в обязательных модах.
    #
    # КАК ЭТО РАБОТАЕТ (никуда ходить и ничего скачивать отдельно не
    # нужно — эти моды уже лежат в вашем modpack.zip вместе со всеми
    # остальными, как и сейчас):
    #   При установке/обновлении сборки лаунчер сам вынимает из mods/
    #   файлы, перечисленные ниже, и прячет их в свой служебный кэш.
    #   Дальше галочка в окне "🧩 Моды" просто перекладывает файл туда-
    #   обратно между кэшем и mods/ — включили, файл лёг в mods/;
    #   выключили — файл убрался обратно в кэш (но не удалился
    #   насовсем, так что включить обратно можно в любой момент без
    #   интернета).
    #
    # КАК ЗАПОЛНИТЬ КАЖДУЮ ЗАПИСЬ:
    #   "id"          — короткий уникальный идентификатор (латиницей, без
    #                   пробелов). Только для хранения выбора игрока.
    #   "name"        — название, которое увидит игрок.
    #   "description" — краткое пояснение под названием (необязательно).
    #   "filename"    — ТОЧНОЕ имя jar-файла этого мода, КАК ОНО ЛЕЖИТ у
    #                   вас в папке mods/ прямо сейчас (например
    #                   "journeymap-1.21.1-6.0.0-fabric.jar"). Откройте
    #                   свою папку mods/ и скопируйте настоящее имя файла
    #                   — если оно не совпадёт один-в-один, лаунчер этот
    #                   мод просто не найдёт и оставит его обязательным
    #                   (то есть ничего не сломается, просто галочка не
    #                   будет на него влиять).
    #   "default"     — включён ли мод по умолчанию (у вас — всегда True).
    #
    # ⚠️ ВАЖНОЕ ОГРАНИЧЕНИЕ: сюда НЕЛЬЗЯ добавлять моды, которые
    # регистрируют что-то новое (эффекты, блоки, предметы, зачарования
    # и т.п.) и при этом стоят у вас на СЕРВЕРЕ — сервер и клиент обязаны
    # видеть одинаковый набор такого контента, иначе при заходе на сервер
    # игра пишет "Соединение потеряно / Сервер отправил реестры с
    # неизвестными ключами" и выкидывает игрока. Именно поэтому Xaero's
    # World Map ниже убран из опциональных — он регистрирует свои эффекты
    # для управления картой на сервере. Если сомневаетесь по поводу
    # какого-то мода — оставьте его обязательным (просто не добавляйте
    # сюда), это всегда безопасный выбор.
    #
    # Если список пустой [] — кнопка "🧩 Моды" в лаунчере не показывается.
    # Готовые ресурс-паки, которые лаунчер ставит сам с Modrinth в один клик
    # (кнопка «Ресурс-паки» → «Готовые паки»). low_end=True — облегчённый пак:
    # он включается автоматически вместе с режимом для слабых ПК.
    "RECOMMENDED_RESOURCE_PACKS": [
        {
            "slug": "faithful-32x",
            "name": "Faithful 32x",
            "description": "Ванильный стиль в двойном разрешении — самый популярный пак",
        },
        {
            "slug": "better-leaves",
            "name": "Better Leaves",
            "description": "Пышная объёмная листва вместо плоских кубов",
        },
        {
            "slug": "low-on-fire",
            "name": "Low On Fire",
            "description": "Огонь не закрывает пол-экрана, когда вы горите",
        },
        {
            "slug": "fast-better-grass",
            "name": "Fast Better Grass",
            "description": "Красивая трава без потери FPS. Включается сам вместе "
                           "с режимом для слабых ПК",
            "low_end": True,
        },
    ],

    "OPTIONAL_MODS": [
        {
            "id": "emi",
            "name": "EMI",
            "slug": "emi",
            "description": "Просмотр рецептов и предметов",
            # Актуальное на момент написания имя файла для NeoForge 1.21.1.
            # Если вы поставите более новую версию EMI — обновите имя файла
            # на то, что реально лежит у вас в mods/.
            "filename": "emi-1.1.22+1.21.1+neoforge.jar",
            "default": True,
        },
        {
            "id": "invmove",
            "name": "InvMove",
            "slug": "invmove",
            "description": "Ходьба при открытом инвентаре",
            "filename": "InvMove-0.9.3+1.21.1-NeoForge.jar",
            "default": True,
        },
        {
            "id": "jade",
            "name": "Jade",
            "slug": "jade",
            "description": "Подсказки при наведении на блоки",
            "filename": "Jade-1.21.1-NeoForge-15.10.5.jar",
            "default": True,
        },
        {
            "id": "jade_addons",
            "name": "Jade Addons",
            "slug": "jade-addons",
            "description": "Дополнительные подсказки для Jade",
            "filename": "JadeAddons-1.21.1-NeoForge-6.1.0.jar",
            "default": True,
        },
        {
            "id": "appleskin",
            "name": "AppleSkin",
            "slug": "appleskin",
            "description": "Показывает сытость/насыщение на HUD",
            "filename": "appleskin-neoforge-mc1.21-3.0.9.jar",
            "default": True,
        },
        {
            "id": "ambient_sounds",
            "name": "AmbientSounds",
            "slug": "ambientsounds",
            "description": "Атмосферные звуки",
            "filename": "AmbientSounds_NEOFORGE_v6.3.8_mc1.21.1.jar",
            "default": True,
        },
        {
            "id": "mouse_tweaks",
            "name": "Mouse Tweaks",
            "slug": "mouse-tweaks",
            "description": "Удобный drag&drop в инвентаре",
            "filename": "MouseTweaks-neoforge-mc1.21-2.26.1.jar",
            "default": True,
        },
        {
            "id": "just_zoom",
            "name": "Just Zoom",
            "slug": "just-zoom",
            "description": "Зум по клавише",
            "filename": "justzoom_neoforge_2.1.0_MC_1.21.1.jar",
            "default": True,
        },
        {
            "id": "no_chat_reports",
            "name": "No Chat Reports",
            "slug": "no-chat-reports",
            "description": "Убирает отчёты о чате (приватность)",
            "filename": "NoChatReports-NEOFORGE-1.21.1-v2.9.1.jar",
            "default": True,
        },
        {
            "id": "betterf3",
            "name": "BetterF3",
            "slug": "betterf3",
            "description": "Улучшенный экран отладки (F3)",
            "filename": "BetterF3-11.0.3-NeoForge-1.21.1.jar",
            "default": True,
        },
        {
            "id": "iris",
            "name": "Iris Shaders",
            "slug": "iris",
            "description": "Поддержка шейдеров",
            "filename": "iris-neoforge-1.8.12+mc1.21.1.jar",
            "default": True,
        },
    ],
}
# ================================================================

# Цвета интерфейса — тёмная и светлая тема (индустриальный стиль:
# тёмный фон + золотисто-оранжевый акцент)
THEMES = {
    "dark": {
        "bg_grad_top": "#0d1014",
        "bg_grad_bottom": "#151a22",
        "bg_panel": "#171c24",
        "bg_field": "#212833",
        "fg": "#e8edf4",
        "fg_muted": "#8c99a8",
        # Акцент взят прямо из иконки сборки (синий), только чуть спокойнее.
        # Жёлтый/янтарный убран — он выбивался из общей гаммы.
        "accent": "#2f9fe0",
        "accent_hover": "#4fb4ef",
        "accent_dim": "#1d3a4d",
        "accent_text": "#ffffff",
        "border": "#2a323d",
        "status_online": "#5fd48b",
        "status_offline": "#ff7a6b",
    },
    "light": {
        "bg_grad_top": "#eff2f6",
        "bg_grad_bottom": "#dfe4ec",
        "bg_panel": "#ffffff",
        "bg_field": "#eef1f6",
        "fg": "#1b1f26",
        "fg_muted": "#66748a",
        "accent": "#0e7fc4",
        "accent_hover": "#0a6aa6",
        "accent_dim": "#cfe4f3",
        "accent_text": "#ffffff",
        "border": "#d5dce5",
        "status_online": "#2f9e52",
        "status_offline": "#c94a3d",
    },
}


def _hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _lerp_color(color_a: str, color_b: str, t: float) -> str:
    r1, g1, b1 = _hex_to_rgb(color_a)
    r2, g2, b2 = _hex_to_rgb(color_b)
    return "#%02x%02x%02x" % (
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    )


def _draw_vertical_gradient(canvas: tk.Canvas, width: int, height: int, top: str, bottom: str, steps: int = 60) -> None:
    canvas.delete("gradient")
    for i in range(steps):
        t0 = i / steps
        t1 = (i + 1) / steps
        color = _lerp_color(top, bottom, t0)
        y0, y1 = int(height * t0), int(height * t1) + 1
        canvas.create_rectangle(0, y0, width, y1, fill=color, outline=color, tags="gradient")


def _rounded_rect_points(x1, y1, x2, y2, radius):
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    return [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]


def _draw_rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs):
    return canvas.create_polygon(_rounded_rect_points(x1, y1, x2, y2, radius), smooth=True, **kwargs)


# Цветные пятна под стеклом — координаты и радиус заданы долями от размера
# окна, цвета взяты из иконки сборки. Именно они дают «живой» цвет, который
# просвечивает сквозь панель. Размытая фотография тут не годится: сильный блюр
# усредняет её в грязное пятно.
GLASS_BLOBS = {
    "dark": [(0.22, 0.22, 0.44, "#0092ec"), (0.82, 0.79, 0.41, "#0b3f8f"),
             (0.88, 0.18, 0.29, "#00c6ff"), (0.18, 0.88, 0.32, "#134a86")],
    "light": [(0.22, 0.22, 0.44, "#7fcbf5"), (0.82, 0.79, 0.41, "#9dbde4"),
              (0.88, 0.18, 0.29, "#bde7ff"), (0.18, 0.88, 0.32, "#aecaea")],
}
# Насколько панель прозрачна: чем меньше, тем сильнее сквозь неё виден цвет.
GLASS_PANEL_ALPHA = {"dark": 162, "light": 188}


def _render_window_backdrop(width: int, height: int, colors: dict, margin: int,
                            radius: int, theme_name: str = "dark"):
    """Рисует фон окна одной картинкой: цветные размытые пятна, мягкая тень под
    панелью, полупрозрачная матовая панель и световая грань сверху.

    Так получается эффект стекла: цвет снизу мягко просвечивает сквозь панель.
    Настоящее системное размытие (Acrylic) в tkinter не использовать — для него
    окно надо делать прозрачным, а прозрачные места становятся «сквозными» для
    мышки. Если Pillow недоступен — возвращаем None и рисуем простой градиент."""
    if not _PIL_OK:
        return None
    try:
        layer = Image.new("RGB", (width, height), _hex_to_rgb(colors["bg_grad_top"]))
        draw = ImageDraw.Draw(layer)
        for fx, fy, fr, color in GLASS_BLOBS.get(theme_name, GLASS_BLOBS["dark"]):
            cx, cy, r = width * fx, height * fy, width * fr
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hex_to_rgb(color))
        base = layer.filter(ImageFilter.GaussianBlur(max(12, width // 4))).convert("RGBA")

        # Тень под панелью — она отделяет «стекло» от фона и даёт глубину.
        shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [margin, margin + 8, width - margin, height - margin + 8],
            radius=radius, fill=(0, 0, 0, 120))
        base = Image.alpha_composite(base, shadow.filter(ImageFilter.GaussianBlur(18)))

        panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(panel).rounded_rectangle(
            [margin, margin, width - margin, height - margin], radius=radius,
            fill=_hex_to_rgb(colors["bg_panel"]) + (GLASS_PANEL_ALPHA.get(theme_name, 170),))
        base = Image.alpha_composite(base, panel)

        # Тонкая грань + блик по верхнему краю — то, что читается как стекло.
        edge = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        edge_draw = ImageDraw.Draw(edge)
        edge_draw.rounded_rectangle(
            [margin, margin, width - margin, height - margin], radius=radius,
            outline=(255, 255, 255, 90), width=1)
        edge_draw.arc([margin + 2, margin + 2, width - margin - 2, margin + radius * 2],
                      start=195, end=345, fill=(255, 255, 255, 150), width=2)
        return Image.alpha_composite(base, edge)
    except Exception:
        return None



# Домашняя папка лаунчера (всегда на системном диске): здесь лежат настройки
# и кэши. Она должна быть в фиксированном месте — именно из неё мы узнаём,
# куда пользователь поставил саму игру.
APP_DATA_DIR = Path.home() / (".%s_launcher" % CONFIG["PACK_NAME"].lower())
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
OPTIONAL_CACHE_DIR = APP_DATA_DIR / "optional_mods_cache"
MOD_ICONS_DIR = APP_DATA_DIR / "mod_icons"

# Папка с самой игрой. По умолчанию — на диске C рядом с настройками, но
# пользователь может перенести её куда угодно (например, D:\Games\IC3).
# Пути ниже меняются функцией set_install_dir() — поэтому все места в коде
# обращаются к INSTANCE_DIR во время вызова, а не сохраняют его копию.
DEFAULT_INSTANCE_DIR = APP_DATA_DIR / "instance"
INSTANCE_DIR = DEFAULT_INSTANCE_DIR
MODPACK_VERSION_FILE = INSTANCE_DIR / ".modpack_version"
INSTALL_MARKER_FILE = INSTANCE_DIR / ".install_complete.json"


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(data: dict) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_install_dir(path) -> None:
    """Переключает лаунчер на другую папку установки: пересчитывает пути,
    которые от неё зависят. Вызывается при старте и после переноса игры."""
    global INSTANCE_DIR, MODPACK_VERSION_FILE, INSTALL_MARKER_FILE
    INSTANCE_DIR = Path(path)
    MODPACK_VERSION_FILE = INSTANCE_DIR / ".modpack_version"
    INSTALL_MARKER_FILE = INSTANCE_DIR / ".install_complete.json"


def get_saved_install_dir() -> Path:
    """Папка установки из настроек (или папка по умолчанию на диске C)."""
    saved = load_settings().get("install_dir")
    if saved:
        try:
            return Path(saved)
        except Exception:
            pass
    return DEFAULT_INSTANCE_DIR


def _is_inside(child, parent) -> bool:
    """True, если child лежит внутри parent."""
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def move_installation(new_dir, status_cb=None) -> None:
    """Переносит игру в новую папку вместе со всеми пользовательскими данными
    (миры, настройки, скриншоты, ресурспаки). Если игра ещё не установлена —
    просто запоминает новое место. Бросает RuntimeError с понятным текстом,
    если перенос невозможен."""
    old_dir = INSTANCE_DIR
    new_dir = Path(new_dir)
    if new_dir == old_dir:
        return
    if _is_inside(new_dir, old_dir):
        raise RuntimeError("Нельзя перенести игру внутрь её собственной папки.")
    if new_dir.exists() and any(new_dir.iterdir()):
        raise RuntimeError(
            "Папка не пустая:\n%s\n\nВыберите пустую или новую папку." % new_dir)

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    if old_dir.exists():
        # Пустую папку-приёмник убираем: иначе shutil.move положит игру
        # ВНУТРЬ неё, а не на её место.
        if new_dir.exists():
            new_dir.rmdir()
        if status_cb:
            status_cb("Переношу игру в %s — это может занять несколько минут..." % new_dir)
        shutil.move(str(old_dir), str(new_dir))
    else:
        new_dir.mkdir(parents=True, exist_ok=True)

    update_settings(install_dir=str(new_dir))
    set_install_dir(new_dir)
    if status_cb:
        status_cb("Готово. Игра теперь в папке: %s" % new_dir)


def get_system_ram_mb():
    """Возвращает объём физической ОЗУ на компьютере игрока (в МБ),
    либо None, если определить не получилось (не Windows / ошибка)."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return int(stat.ullTotalPhys / (1024 * 1024))
    except Exception:
        return None


def set_titlebar_dark(root: tk.Tk, dark: bool) -> None:
    """Красит системную рамку окна (заголовок) в тёмный или светлый цвет на
    Windows 10/11. Если не получится (другая ОС/старый Windows) — ничего не делает."""
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (новый/старый id)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
    except Exception:
        pass


def open_folder(path: Path) -> None:
    """Открывает папку в проводнике (или его аналоге на других ОС)."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def update_settings(**kwargs) -> None:
    """Обновляет только переданные ключи в settings.json, не затирая остальные."""
    data = load_settings()
    data.update(kwargs)
    save_settings(data)


def resource_path(filename: str) -> Path:
    """Путь к файлу, лежащему рядом с launcher.py. Если это собранный
    PyInstaller-.exe (--onefile), файлы, добавленные через --add-data,
    распаковываются во временную папку sys._MEIPASS — учитываем это,
    иначе иконка/другие приложенные файлы не найдутся внутри .exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / filename


ICON_NAMES = ["folder", "chat", "grid", "wrench", "list", "sun", "moon", "gauge", "gear",
              "image", "shader", "discord"]


def load_icons(theme_name: str) -> dict:
    """Загружает набор PNG-иконок интерфейса под конкретную тему (светлый
    или тёмный глиф). Если файла нет рядом — просто пропускает эту иконку
    (кнопки останутся без картинки, но не сломаются)."""
    icons = {}
    for name in ICON_NAMES:
        path = resource_path("icons/%s_%s.png" % (name, theme_name))
        try:
            icons[name] = tk.PhotoImage(file=str(path))
        except Exception:
            icons[name] = None
    return icons


def parse_host_port(address: str, default_port: int = 25565):
    address = (address or "").strip()
    if ":" in address:
        host, _, port_str = address.rpartition(":")
        try:
            return host, int(port_str)
        except ValueError:
            return address, default_port
    return address, default_port


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _read_varint(sock) -> int:
    value = 0
    position = 0
    while True:
        data = sock.recv(1)
        if not data:
            raise ConnectionError("Соединение закрыто во время чтения ответа сервера")
        byte = data[0]
        value |= (byte & 0x7F) << position
        if not (byte & 0x80):
            break
        position += 7
    return value


def ping_server(host: str, port: int, timeout: float = 3.0) -> dict:
    """Опрашивает сервер Minecraft по протоколу Server List Ping (тем же,
    которым сама игра показывает статус в списке серверов). Не требует
    сторонних библиотек — общается напрямую по TCP-сокету. Возвращает
    {"online": bool, "players_online": int|None, "players_max": int|None}."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)

            host_bytes = host.encode("utf-8")
            handshake = bytearray()
            handshake += _write_varint(0x00)
            handshake += _write_varint(770)  # версия протокола — для статус-запроса не критична
            handshake += _write_varint(len(host_bytes))
            handshake += host_bytes
            handshake += struct.pack(">H", port)
            handshake += _write_varint(1)  # next state = status
            sock.sendall(_write_varint(len(handshake)) + bytes(handshake))

            status_request = _write_varint(0x00)
            sock.sendall(_write_varint(len(status_request)) + status_request)

            _read_varint(sock)  # длина пакета ответа — не нужна
            _read_varint(sock)  # id пакета — не нужен
            json_length = _read_varint(sock)
            raw = b""
            while len(raw) < json_length:
                chunk = sock.recv(json_length - len(raw))
                if not chunk:
                    break
                raw += chunk

            payload = json.loads(raw.decode("utf-8", errors="replace"))
            players = payload.get("players", {}) if isinstance(payload, dict) else {}
            return {
                "online": True,
                "players_online": players.get("online"),
                "players_max": players.get("max"),
            }
    except Exception:
        return {"online": False, "players_online": None, "players_max": None}


def offline_uuid(username: str) -> str:
    """Генерирует стабильный UUID для офлайн-ника (как это делает ванильный клиент)."""
    return str(uuid.uuid3(uuid.NAMESPACE_OID, "OfflinePlayer:%s" % username))


def _download_parallel(url: str, dest: Path, progress_cb=None,
                       connections: int = 4, min_size: int = 8 << 20) -> bool:
    """Качает файл в несколько потоков: каждый берёт свой кусок по HTTP Range.
    На обычном домашнем интернете это заметно быстрее, чем один поток, — именно
    так работают менеджеры загрузок.

    Возвращает True при успехе. Если сервер не умеет отдавать куски, файл
    маленький или что-то оборвалось — возвращает False, и вызывающий код
    спокойно качает обычным способом (с докачкой)."""
    part = dest.with_name(dest.name + ".part")
    try:
        head = urllib.request.Request(url, method="HEAD",
                                      headers={"User-Agent": "CheckpointLauncher"})
        with urllib.request.urlopen(head, timeout=20) as response:
            size = int(response.headers.get("Content-Length") or 0)
            ranges_ok = "bytes" in (response.headers.get("Accept-Ranges") or "").lower()
    except Exception:
        return False
    if not size or size < min_size or not ranges_ok:
        return False

    try:
        with open(part, "wb") as fh:
            fh.truncate(size)  # резервируем место, чтобы потоки писали каждый в своё
    except OSError:
        return False

    step = size // connections
    bounds = [(i * step, size - 1 if i == connections - 1 else (i + 1) * step - 1)
              for i in range(connections)]
    state = {"done": 0, "failed": False}
    lock = threading.Lock()

    def worker(start, end):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "CheckpointLauncher", "Range": "bytes=%d-%d" % (start, end)})
            # Свой файловый дескриптор на поток: общий seek() был бы гонкой.
            with urllib.request.urlopen(request, timeout=30) as response, open(part, "r+b") as fh:
                fh.seek(start)
                got = 0
                need = end - start + 1
                while got < need:
                    data = response.read(65536)
                    if not data:
                        break
                    fh.write(data)
                    got += len(data)
                    with lock:
                        state["done"] += len(data)
                        if progress_cb:
                            progress_cb(min(100, int(state["done"] * 100 / size)))
                if got != need:
                    state["failed"] = True
        except Exception:
            state["failed"] = True

    threads = [threading.Thread(target=worker, args=b, daemon=True) for b in bounds]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if state["failed"] or not part.exists() or part.stat().st_size != size:
        # Недокачанную заготовку убираем: иначе обычная докачка приняла бы её
        # за уже скачанный кусок и продолжила с неверного места.
        part.unlink(missing_ok=True)
        return False

    dest.unlink(missing_ok=True)
    part.replace(dest)
    return True


def download_file(url: str, dest: Path, progress_cb=None, retries: int = 5) -> None:
    """Качает файл с докачкой и повторами.

    Раньше здесь был urlretrieve: любой обрыв связи посреди 400-мегабайтного
    архива ронял всю установку с невнятным "IncompleteRead", и качать
    приходилось заново с нуля. Теперь недокачанное складывается в файл .part,
    при обрыве закачка продолжается с места остановки (заголовок HTTP Range),
    и делается несколько попыток с нарастающей паузой. Даже если попытки
    кончились — .part остаётся на диске, и следующий запуск продолжит с него,
    а не начнёт заново."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Сначала пробуем в несколько потоков — так сборка качается заметно
    # быстрее. Если сервер этого не умеет или что-то оборвалось — ниже
    # спокойно качаем одним потоком с докачкой.
    if _download_parallel(url, dest, progress_cb):
        return

    part = dest.with_name(dest.name + ".part")
    last_error = None

    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(url, headers={"User-Agent": "CheckpointLauncher"})
        if have:
            request.add_header("Range", "bytes=%d-" % have)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                # Сервер мог проигнорировать Range (ответил 200 вместо 206) —
                # тогда начинаем файл с нуля, иначе получим мешанину.
                if have and getattr(response, "status", 200) != 206:
                    have = 0
                    part.unlink(missing_ok=True)
                length = response.headers.get("Content-Length")
                total = (int(length) + have) if length else 0
                with open(part, "ab" if have else "wb") as fh:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        if progress_cb and total:
                            progress_cb(min(100, int(have * 100 / total)))
                # ВАЖНО: при обрыве связи read() возвращает пустоту — так же,
                # как при нормальном конце файла. Без этой проверки обрезанный
                # архив молча считался бы скачанным целиком.
                if total and have < total:
                    raise IOError("связь оборвалась: получено %d из %d байт" % (have, total))
            dest.unlink(missing_ok=True)
            part.replace(dest)
            return
        except Exception as exc:  # noqa: BLE001 — сеть падает по-разному
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(
        "Обрыв связи при скачивании — не удалось за %d попыток.\n\n"
        "Проверьте интернет и нажмите «Играть» ещё раз: закачка продолжится "
        "с места обрыва, заново качать не придётся.\n\n(%s)" % (retries, last_error))


def get_remote_modpack_version() -> int:
    """Версия сборки модов. Если в CONFIG указана MODPACK_VERSION_URL —
    скачивает и читает число оттуда (так можно обновлять моды без
    пересборки .exe). Если ссылки нет или скачать не удалось — использует
    число из CONFIG."""
    url = CONFIG.get("MODPACK_VERSION_URL")
    if not url:
        return CONFIG["MODPACK_VERSION"]
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return int(response.read().decode("utf-8").strip())
    except Exception:
        # Нет интернета / файл не отвечает — не считаем это поводом
        # переустанавливать моды, просто используем то, что уже стоит.
        local = get_local_modpack_version()
        return local if local != -1 else CONFIG["MODPACK_VERSION"]


def get_local_modpack_version() -> int:
    if MODPACK_VERSION_FILE.exists():
        try:
            return int(MODPACK_VERSION_FILE.read_text().strip())
        except Exception:
            return -1
    return -1


def install_modpack(status_cb, progress_cb) -> None:
    """Скачивает архив с модами и распаковывает поверх папки экземпляра."""
    zip_path = APP_DATA_DIR / "modpack_download.zip"

    def download_progress(pct):
        # Подпись с номером шага ставит сам progress_cb — здесь только процент.
        progress_cb(pct)

    status_cb("скачивание")
    download_file(CONFIG["MODPACK_URL"], zip_path, download_progress)

    # Чистим старые моды/конфиги, чтобы не оставалось "мусора" от старой версии.
    # resourcepacks и shaderpacks НЕ трогаем: там лежат паки, которые игрок
    # поставил сам через менеджеры — обновление сборки модов не должно их
    # удалять.
    for folder in ("mods", "config", "kubejs"):
        target = INSTANCE_DIR / folder
        if target.exists():
            shutil.rmtree(target)

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        total = len(members) or 1
        last_pct = -1
        for index, member in enumerate(members, start=1):
            zf.extract(member, INSTANCE_DIR)
            pct = int(index * 100 / total)
            if pct != last_pct:
                last_pct = pct
                progress_cb(pct)
                status_cb("распаковка — %d%% (%d/%d файлов)" % (pct, index, total))

    zip_path.unlink(missing_ok=True)

    harvest_optional_mods(status_cb)

    MODPACK_VERSION_FILE.write_text(str(get_remote_modpack_version()))
    progress_cb(100)


def _install_with_retry(func, *args, retries=4, delay_seconds=2, status_cb=None, **kwargs):
    """Выполняет func(*args, **kwargs), повторяя попытку при PermissionError/OSError.

    На Windows такие ошибки чаще всего означают, что антивирус (Защитник
    Windows и т.п.) в этот момент сканирует только что созданный файл
    Java-рантайма и на секунду-две держит его заблокированным. Это
    временная ситуация — обычно достаточно подождать и попробовать снова."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except (PermissionError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                if status_cb:
                    status_cb(
                        "Файл временно занят (вероятно, антивирус проверяет его). "
                        "Повтор через %d сек... (попытка %d/%d)" % (delay_seconds, attempt, retries)
                    )
                time.sleep(delay_seconds)
            continue
    raise last_error


def get_optional_mods_selection() -> dict:
    """Возвращает {id_мода: включён_ли} для всех модов из CONFIG["OPTIONAL_MODS"].
    Для модов, которые игрок ещё не трогал, берётся значение "default" из конфига."""
    settings = load_settings()
    saved = settings.get("optional_mods", {})
    result = {}
    for mod in CONFIG.get("OPTIONAL_MODS", []):
        result[mod["id"]] = bool(saved.get(mod["id"], mod.get("default", True)))
    return result


def save_optional_mods_selection(selection: dict) -> None:
    update_settings(optional_mods=selection)


def remove_blocked_mods(status_cb=None) -> None:
    """Удаляет из mods/ моды, перечисленные в CONFIG["REMOVED_MODS"], даже если
    они есть в архиве сборки. Благодаря этому мод можно убрать, не перезаливая
    modpack.zip — достаточно дописать кусок имени файла в список. Работает при
    каждом запуске, поэтому мод пропадёт и у тех, у кого сборка уже стоит."""
    patterns = [p.lower() for p in CONFIG.get("REMOVED_MODS", []) if p]
    if not patterns:
        return
    mods_dir = INSTANCE_DIR / "mods"
    if not mods_dir.exists():
        return
    for jar in mods_dir.glob("*.jar"):
        name = jar.name.lower()
        if any(p in name for p in patterns):
            try:
                jar.unlink()
                if status_cb:
                    status_cb("Убираю лишний мод: %s" % jar.name)
            except OSError:
                pass


def harvest_optional_mods(status_cb=None) -> None:
    """Вызывается сразу после распаковки архива сборки. Файлы модов,
    перечисленных в CONFIG["OPTIONAL_MODS"], уже лежат в mods/ вместе со
    всеми остальными (архив никак менять не нужно) — эта функция
    вынимает их оттуда и кладёт в отдельный кэш (OPTIONAL_CACHE_DIR),
    откуда дальше галочками в лаунчере их можно возвращать обратно в
    mods/ или убирать, не трогая интернет."""
    optional_mods = CONFIG.get("OPTIONAL_MODS", [])
    if not optional_mods:
        return

    mods_dir = INSTANCE_DIR / "mods"
    OPTIONAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    for mod in optional_mods:
        src = mods_dir / mod["filename"]
        cached = OPTIONAL_CACHE_DIR / mod["filename"]
        if src.exists():
            shutil.copy2(src, cached)


def _download_optional_from_modrinth(mod: dict, status_cb=None) -> bool:
    """Качает jar опционального мода напрямую с Modrinth по его slug и кладёт в
    кэш под именем mod["filename"]. Нужно для модов, которых нет в самой сборке
    (например, InvMove) — их всё равно можно включить одной галочкой.
    Возвращает True, если файл появился в кэше."""
    slug = mod.get("slug")
    if not slug:
        return False
    try:
        if status_cb:
            status_cb("Скачиваю мод: %s" % mod["name"])
        _fname, url = _find_modrinth_download(
            slug, CONFIG["MC_VERSION"], [CONFIG["MOD_LOADER"]])
        if not url:
            return False
        OPTIONAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        download_file(url, OPTIONAL_CACHE_DIR / mod["filename"])
        return (OPTIONAL_CACHE_DIR / mod["filename"]).exists()
    except Exception:
        return False


def _load_mod_icon_image(slug: str, size: int = 40):
    """Возвращает PIL.Image иконки мода (квадрат size×size) или None. Берёт
    иконку с Modrinth (icon_url) и кэширует на диск. Формат webp/png/jpeg —
    Pillow разбирает всё. Всегда вызывается из фонового потока."""
    if not (_PIL_OK and slug):
        return None
    raw = MOD_ICONS_DIR / slug
    try:
        if not raw.exists():
            meta = _modrinth_api_get(
                "https://api.modrinth.com/v2/project/%s" % slug, timeout=8)
            url = meta.get("icon_url")
            if not url:
                return None
            MOD_ICONS_DIR.mkdir(parents=True, exist_ok=True)
            download_file(url, raw)
        return Image.open(raw).convert("RGBA").resize((size, size))
    except Exception:
        return None


def _apply_one_optional_mod(mod: dict, enabled: bool, status_cb=None) -> str:
    """Перекладывает jar-файл одного опционального мода между кэшем и
    mods/ в соответствии с "enabled". Возвращает "" при успехе, иначе
    текст ошибки (например, если файла нет ни в mods/, ни в кэше — так
    бывает, если имя в CONFIG не совпадает с реальным именем файла).

    Сама операция копирования/удаления обёрнута в _install_with_retry,
    потому что сразу после распаковки сборки антивирус ещё может
    проверять эти файлы и на секунду-две держать их занятыми — без
    повтора попытки это раньше приводило к тому, что переключение мода
    тихо не срабатывало."""
    mods_dir = INSTANCE_DIR / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    dst = mods_dir / mod["filename"]
    cached = OPTIONAL_CACHE_DIR / mod["filename"]

    if enabled:
        if dst.exists():
            return ""
        if not cached.exists():
            # Файла нет в кэше — значит мода нет в самой сборке (как InvMove).
            # Пробуем скачать его напрямую с Modrinth по slug.
            if not _download_optional_from_modrinth(mod, status_cb):
                msg = "Не удалось получить %s (нет в сборке и не скачался)" % mod["name"]
                if status_cb:
                    status_cb(msg)
                return msg
        if status_cb:
            status_cb("Включаю мод: %s" % mod["name"])
        _install_with_retry(shutil.copy2, cached, dst, status_cb=status_cb)
        return ""
    else:
        if dst.exists():
            if status_cb:
                status_cb("Отключаю мод: %s" % mod["name"])
            # На всякий случай сохраняем копию в кэш перед удалением из
            # mods/ — если вдруг её там ещё не было (например, самый
            # первый запуск после появления этой функции в лаунчере).
            if not cached.exists():
                _install_with_retry(shutil.copy2, dst, cached, status_cb=status_cb)
            _install_with_retry(dst.unlink, status_cb=status_cb)
        return ""


def restore_no_longer_optional_mods(status_cb=None) -> None:
    """Если какой-то мод раньше был опциональным и игрок его выключил (файл
    осел в кэше), а потом вы убрали этот мод из CONFIG["OPTIONAL_MODS"]
    (то есть он снова должен быть обязательным для всех) — эта функция
    сама вернёт файл обратно в mods/, если его там почему-то до сих пор
    нет. Работает автоматически при каждом запуске, отдельно ничего
    делать не нужно."""
    if not OPTIONAL_CACHE_DIR.exists():
        return
    known_filenames = {mod["filename"] for mod in CONFIG.get("OPTIONAL_MODS", [])}
    mods_dir = INSTANCE_DIR / "mods"
    for cached_file in OPTIONAL_CACHE_DIR.iterdir():
        if not cached_file.is_file() or cached_file.name in known_filenames:
            continue
        target = mods_dir / cached_file.name
        if not target.exists():
            mods_dir.mkdir(parents=True, exist_ok=True)
            if status_cb:
                status_cb("Восстанавливаю обязательный мод: %s" % cached_file.name)
            shutil.copy2(cached_file, target)


def apply_optional_mods(status_cb=None, progress_cb=None) -> list:
    """Приводит папку mods/ в соответствие с сохранённым выбором игрока
    (вызывается при каждом запуске игры). Возвращает список названий
    модов, которые не удалось применить (обычно из-за несовпадения
    filename в CONFIG с реальным именем файла) — такая ошибка на одном
    моде не прерывает обработку остальных."""
    optional_mods = CONFIG.get("OPTIONAL_MODS", [])
    if not optional_mods:
        return []

    selection = get_optional_mods_selection()
    failed = []
    total = len(optional_mods) or 1
    for index, mod in enumerate(optional_mods, start=1):
        enabled = selection.get(mod["id"], mod.get("default", True))
        try:
            error = _apply_one_optional_mod(mod, enabled, status_cb)
        except (PermissionError, OSError) as exc:
            error = str(exc)
        if error:
            failed.append(mod["name"])
        if progress_cb:
            progress_cb(int(index * 100 / total))
    return failed


# ============ Ресурс-паки и шейдеры (общая механика) ============
# Оба менеджера устроены одинаково: паки — это zip-архивы (или папки) внутри
# resourcepacks/ и shaderpacks/. Отличается только то, как игра узнаёт, какой
# пак включён, — это описано в функциях ниже.

def _read_options_value(key: str, default: str = "") -> str:
    """Читает одну строку из options.txt (файл настроек Minecraft)."""
    path = INSTANCE_DIR / "options.txt"
    if not path.exists():
        return default
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            name, _, value = line.partition(":")
            if name == key:
                return value
    except OSError:
        pass
    return default


def _write_options_value(key: str, value: str) -> None:
    """Меняет одну строку в options.txt, не трогая остальные настройки игрока."""
    path = INSTANCE_DIR / "options.txt"
    lines = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
    replaced = False
    for index, line in enumerate(lines):
        if line.partition(":")[0] == key:
            lines[index] = "%s:%s" % (key, value)
            replaced = True
            break
    if not replaced:
        lines.append("%s:%s" % (key, value))
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_pack_preview(pack_path, size: int = 64):
    """Возвращает PIL.Image превью пака (файл pack.png внутри архива или папки)
    или None, если картинки нет. Используется и для ресурс-паков, и для
    шейдеров."""
    if not _PIL_OK:
        return None
    path = Path(pack_path)
    try:
        if path.is_dir():
            icon = path / "pack.png"
            if not icon.exists():
                return None
            img = Image.open(icon)
        else:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                # pack.png обычно в корне, но у некоторых паков лежит в подпапке
                candidates = [n for n in names if n.endswith("pack.png")]
                if not candidates:
                    return None
                icon_name = min(candidates, key=lambda n: n.count("/"))
                raw = zf.read(icon_name)
            img = Image.open(io.BytesIO(raw))
        return img.convert("RGBA").resize((size, size))
    except Exception:
        return None


def make_pack_placeholder(kind: str, size: int, bg_color: str, glyph_color: str):
    """Красивая стандартная картинка для пака без своего превью: скруглённый
    квадрат с простым глифом — «фото» для ресурс-паков и «искра» для шейдеров."""
    if not _PIL_OK:
        return None
    try:
        scale = 4
        big = size * scale
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=big // 6, fill=bg_color)
        width = max(2, big // 18)
        if kind == "shader":
            for cx, cy, r in ((big * 0.40, big * 0.40, big * 0.19),
                              (big * 0.68, big * 0.68, big * 0.11)):
                draw.line([(cx, cy - r), (cx, cy + r)], fill=glyph_color, width=width)
                draw.line([(cx - r, cy), (cx + r, cy)], fill=glyph_color, width=width)
                r2 = r * 0.62
                draw.line([(cx - r2, cy - r2), (cx + r2, cy + r2)], fill=glyph_color, width=width)
                draw.line([(cx + r2, cy - r2), (cx - r2, cy + r2)], fill=glyph_color, width=width)
        else:
            draw.rounded_rectangle([big * 0.17, big * 0.23, big * 0.83, big * 0.77],
                                   radius=big // 14, outline=glyph_color, width=width)
            draw.ellipse([big * 0.29, big * 0.33, big * 0.41, big * 0.45],
                         outline=glyph_color, width=width)
            draw.line([(big * 0.23, big * 0.71), (big * 0.44, big * 0.47), (big * 0.58, big * 0.64)],
                      fill=glyph_color, width=width, joint="curve")
            draw.line([(big * 0.52, big * 0.57), (big * 0.66, big * 0.41), (big * 0.79, big * 0.71)],
                      fill=glyph_color, width=width, joint="curve")
        return img.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def read_pack_description(pack_path) -> str:
    """Достаёт описание пака из pack.mcmeta (у шейдеров его обычно нет)."""
    path = Path(pack_path)
    try:
        if path.is_dir():
            raw = (path / "pack.mcmeta").read_text(encoding="utf-8", errors="ignore")
        else:
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if n.endswith("pack.mcmeta")]
                if not names:
                    return ""
                raw = zf.read(min(names, key=lambda n: n.count("/"))).decode("utf-8", "ignore")
        description = json.loads(raw).get("pack", {}).get("description", "")
        # Описание бывает строкой, объектом или списком текстовых кусков.
        if isinstance(description, dict):
            description = description.get("text", "")
        elif isinstance(description, list):
            description = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in description)
        return " ".join(str(description).split())
    except Exception:
        return ""


def _list_packs_in(folder: Path) -> list:
    """Общий обход папки с паками: zip-архивы и распакованные папки."""
    if not folder.exists():
        return []
    packs = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.name.startswith("."):
            continue
        if path.is_file() and path.suffix.lower() != ".zip":
            continue
        packs.append(path)
    return packs


# ---------------------------- Ресурс-паки ----------------------------
# Игра хранит список включённых ресурс-паков прямо в options.txt, в строке
# resourcePacks:["vanilla","file/Название.zip"]. Поэтому "включить" — это
# добавить пак в этот список, а не просто положить файл в папку.

def get_resourcepacks_dir() -> Path:
    return INSTANCE_DIR / "resourcepacks"


def get_enabled_resource_packs() -> list:
    raw = _read_options_value("resourcePacks", "[]")
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def set_enabled_resource_packs(items: list) -> None:
    _write_options_value("resourcePacks", json.dumps(items, ensure_ascii=False))


def list_resource_packs() -> list:
    """Список установленных ресурс-паков с признаком "включён"."""
    enabled = get_enabled_resource_packs()
    packs = []
    for path in _list_packs_in(get_resourcepacks_dir()):
        entry = "file/%s" % path.name
        packs.append({
            "name": path.stem if path.is_file() else path.name,
            "path": path,
            "entry": entry,
            "enabled": entry in enabled,
            "description": read_pack_description(path),
        })
    return packs


def set_resource_pack_enabled(pack: dict, enabled: bool) -> None:
    items = get_enabled_resource_packs()
    if not items:
        items = ["vanilla"]  # ванильные ресурсы всегда должны быть в списке
    entry = pack["entry"]
    if enabled:
        if entry not in items:
            items.append(entry)
    else:
        items = [item for item in items if item != entry]
    set_enabled_resource_packs(items)


def install_resource_pack(zip_path) -> str:
    """Ставит ресурс-пак из ZIP. Возвращает имя установленного файла.
    Если пак с таким именем уже есть — заменяет его."""
    src = Path(zip_path)
    if not zipfile.is_zipfile(src):
        raise RuntimeError("Это не ZIP-архив: %s" % src.name)
    with zipfile.ZipFile(src) as zf:
        if not any(n.endswith("pack.mcmeta") for n in zf.namelist()):
            raise RuntimeError(
                "В архиве нет pack.mcmeta — похоже, это не ресурс-пак:\n%s" % src.name)
    dst_dir = get_resourcepacks_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / src.name)
    return src.name


def _remember_recommended_pack(slug: str, filename: str) -> None:
    """Запоминаем, каким файлом лёг готовый пак: имя файла с Modrinth заранее
    неизвестно, а нам потом надо его найти (например, для слабого режима)."""
    data = load_settings().get("recommended_packs", {})
    data[slug] = filename
    update_settings(recommended_packs=data)


def _recommended_pack_filename(slug: str):
    return load_settings().get("recommended_packs", {}).get(slug)


def is_recommended_pack_installed(pack_cfg: dict) -> bool:
    filename = _recommended_pack_filename(pack_cfg["slug"])
    return bool(filename) and (get_resourcepacks_dir() / filename).exists()


def install_recommended_resource_pack(pack_cfg: dict, status_cb=None) -> str:
    """Скачивает готовый ресурс-пак с Modrinth по slug и кладёт в resourcepacks."""
    if status_cb:
        status_cb("Скачиваю ресурс-пак: %s" % pack_cfg["name"])
    filename, url = _find_modrinth_download(
        pack_cfg["slug"], CONFIG["MC_VERSION"], ["minecraft"])
    if not url:
        raise RuntimeError("Не удалось найти «%s» для Minecraft %s"
                           % (pack_cfg["name"], CONFIG["MC_VERSION"]))
    dst_dir = get_resourcepacks_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / (filename or (pack_cfg["slug"] + ".zip"))
    download_file(url, target)
    _remember_recommended_pack(pack_cfg["slug"], target.name)
    return target.name


def _apply_low_end_resource_pack(enabled: bool, status_cb=None) -> None:
    """Вместе с режимом для слабых ПК включает облегчённый ресурс-пак (и
    выключает обратно). Если пака ещё нет — скачивает. Любая ошибка (нет
    интернета) не должна мешать запуску игры, поэтому просто пропускаем."""
    for pack_cfg in CONFIG.get("RECOMMENDED_RESOURCE_PACKS", []):
        if not pack_cfg.get("low_end"):
            continue
        try:
            filename = _recommended_pack_filename(pack_cfg["slug"])
            if not (filename and (get_resourcepacks_dir() / filename).exists()):
                if not enabled:
                    continue  # выключать нечего — пак и не ставили
                filename = install_recommended_resource_pack(pack_cfg, status_cb)
            set_resource_pack_enabled({"entry": "file/%s" % filename}, enabled)
            if status_cb:
                status_cb("%s облегчённый ресурс-пак: %s"
                          % ("включён" if enabled else "выключен", pack_cfg["name"]))
        except Exception:
            pass


def delete_resource_pack(pack: dict) -> None:
    """Удаляет пак и убирает его из списка включённых."""
    set_resource_pack_enabled(pack, False)
    path = Path(pack["path"])
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def ensure_pinned_server(status_cb=None) -> None:
    """Добавляет сервер из CONFIG["PINNED_SERVER"] в список серверов игрока
    (файл servers.dat), если его там ещё нет — сравнение по IP. Остальные
    серверы, которые игрок мог добавить сам, не трогаются и не удаляются.
    Вызывается при каждом запуске игры, поэтому даже если игрок случайно
    удалит сервер из списка внутри игры, он появится заново при следующем
    запуске лаунчера."""
    pinned = CONFIG.get("PINNED_SERVER") or {}
    pinned_ip = (pinned.get("ip") or "").strip()
    if not pinned_ip:
        return

    servers_path = INSTANCE_DIR / "servers.dat"
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

    root = None
    if servers_path.exists():
        try:
            tag_type, _name, value = _NBTReader(servers_path.read_bytes()).read_named_tag()
            if tag_type == _NBT_COMPOUND:
                root = value
        except Exception:
            root = None  # повреждённый/нестандартный файл — создадим заново

    if root is None:
        root = NBTCompound()

    field = root.fields.get("servers")
    if field and field[0] == _NBT_LIST:
        _elem_type, servers_list = field[1]
    else:
        servers_list = []

    for server in servers_list:
        if not isinstance(server, NBTCompound):
            continue
        existing_ip = (server.get_value("ip", "") or "").strip().lower()
        if existing_ip == pinned_ip.lower():
            return  # уже в списке — ничего делать не нужно

    new_entry = NBTCompound()
    new_entry.set("name", _NBT_STRING, pinned.get("name") or "Server")
    new_entry.set("ip", _NBT_STRING, pinned_ip)

    updated_list = [new_entry] + list(servers_list)
    root.set("servers", _NBT_LIST, (_NBT_COMPOUND, updated_list))

    writer = _NBTWriter()
    writer.write_named_tag(_NBT_COMPOUND, "", root)
    servers_path.write_bytes(bytes(writer.out))

    if status_cb:
        status_cb("Сервер %s добавлен в список серверов." % (pinned.get("name") or "Server"))


def _modrinth_api_get(url: str, timeout: float = 15.0) -> object:
    request = urllib.request.Request(
        url, headers={"User-Agent": "%s-launcher/1.0 (github.com)" % CONFIG["PACK_NAME"]}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _version_tuple(version_str: str):
    """'1.2.10' -> (1, 2, 10) — чтобы сравнивать версии как числа, а не
    как текст (иначе "1.10" считалось бы меньше "1.9")."""
    parts = []
    for chunk in (version_str or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check_for_launcher_update():
    """Спрашивает у GitHub (открытое API, без ключей), какая последняя
    версия лаунчера опубликована в репозитории из CONFIG["GITHUB_REPO"].
    Если она новее текущей — возвращает {"version": ..., "url": ...},
    иначе None. Любая ошибка (нет интернета, репозиторий не настроен,
    релизов ещё не было) тоже даёт None — это не критичная функция."""
    repo = CONFIG.get("GITHUB_REPO")
    if not repo:
        return None
    try:
        # Берём СПИСОК релизов, а не /releases/latest — потому что релиз со
        # сборкой модов (тег "modpack") помечен как Latest и иначе перебил бы
        # версии лаунчера. Ищем самый свежий тег-версию (v1.2.3) с .exe.
        releases = _modrinth_api_get(
            "https://api.github.com/repos/%s/releases?per_page=30" % repo, timeout=8)
        if isinstance(releases, dict):
            releases = [releases]
        best = None
        for rel in (releases or []):
            if rel.get("draft") or rel.get("prerelease"):
                continue
            ver = (rel.get("tag_name") or "").lstrip("vV")
            if not ver or not ver[0].isdigit():
                continue  # пропускаем не-версионные теги вроде "modpack"
            exe_url = None
            for asset in (rel.get("assets") or []):
                if (asset.get("name") or "").lower().endswith(".exe"):
                    exe_url = asset.get("browser_download_url")
                    break
            vt = _version_tuple(ver)
            if best is None or vt > best[0]:
                best = (vt, ver, exe_url,
                        rel.get("html_url") or ("https://github.com/%s/releases" % repo))
        if best and best[0] > _version_tuple(CONFIG["LAUNCHER_VERSION"]):
            return {"version": best[1], "exe_url": best[2], "url": best[3]}
    except Exception:
        pass
    return None


def _find_modrinth_download(slug: str, mc_version: str, loaders: list, prefer_keyword: str = None):
    """Спрашивает у Modrinth (открытое API, без ключей) актуальный файл под
    нужную версию Minecraft для одного из указанных загрузчиков. Возвращает
    (имя_файла, прямая_ссылка) или (None, None), если ничего не нашлось."""
    versions = None
    for use_loader_filter in (True, False):
        params = {"game_versions": json.dumps([mc_version])}
        if use_loader_filter:
            params["loaders"] = json.dumps(loaders)
        url = "https://api.modrinth.com/v2/project/%s/version?%s" % (
            urllib.parse.quote(slug), urllib.parse.urlencode(params),
        )
        try:
            versions = _modrinth_api_get(url)
        except Exception:
            versions = None
        if versions:
            break

    if not versions:
        return None, None

    version = versions[0]  # первая в списке — самая свежая подходящая
    files = version.get("files") or []
    if not files:
        return None, None

    chosen = None
    if prefer_keyword:
        chosen = next(
            (f for f in files if prefer_keyword.lower() in (f.get("filename") or "").lower()),
            None,
        )
    if chosen is None:
        chosen = next((f for f in files if f.get("primary")), files[0])

    return chosen.get("filename"), chosen.get("url")


def install_extra_shaderpacks(status_cb=None, progress_cb=None) -> None:
    """Скачивает шейдеры из CONFIG["EXTRA_SHADERPACKS"] напрямую с
    Modrinth в папку shaderpacks/. Каждый шейдер скачивается только один
    раз — лаунчер запоминает, что уже скачал (метка лежит прямо в
    shaderpacks/, поэтому кнопка "Починить" её тоже сбрасывает и шейдеры
    докачаются заново). Это не критичная для игры часть: если что-то не
    скачалось — игра всё равно запустится, просто без этого шейдера."""
    entries = CONFIG.get("EXTRA_SHADERPACKS", [])
    if not entries:
        return

    shaderpacks_dir = INSTANCE_DIR / "shaderpacks"
    shaderpacks_dir.mkdir(parents=True, exist_ok=True)
    marker_file = shaderpacks_dir / ".launcher_installed_shaders.json"

    installed = set()
    if marker_file.exists():
        try:
            installed = set(json.loads(marker_file.read_text(encoding="utf-8")))
        except Exception:
            installed = set()

    pending = [e for e in entries if e.get("slug") and e["slug"] not in installed]
    if not pending:
        return

    changed = False
    for entry in pending:
        slug = entry["slug"]
        label = entry.get("label", slug)
        if status_cb:
            status_cb("Ищу шейдер «%s»..." % label)
        try:
            filename, url = _find_modrinth_download(
                slug, CONFIG["MC_VERSION"], ["iris"], entry.get("prefer_keyword")
            )
            if not filename or not url:
                if status_cb:
                    status_cb("Шейдер «%s» недоступен для %s — пропускаю." % (label, CONFIG["MC_VERSION"]))
                continue

            def _progress(pct, label=label):
                if status_cb:
                    status_cb("Скачиваю шейдер «%s» — %d%%" % (label, pct))
                if progress_cb:
                    progress_cb(pct)

            download_file(url, shaderpacks_dir / filename, _progress)
            installed.add(slug)
            changed = True
        except Exception:
            if status_cb:
                status_cb("Не удалось скачать шейдер «%s» — пропускаю, это не критично." % label)
            continue

    if changed:
        marker_file.write_text(json.dumps(sorted(installed)), encoding="utf-8")


def install_extra_client_mods(status_cb=None, progress_cb=None) -> None:
    """Скачивает доп. клиентские моды из CONFIG["EXTRA_CLIENT_MODS"] с
    Modrinth и кладёт в mods/. Скачивается каждый один раз в постоянный кэш
    (APP_DATA), а в mods/ просто копируется при каждом запуске — поэтому
    даже после переустановки/теста повторно из интернета не тянется.
    Некритично: если мод недоступен или Modrinth молчит — тихо пропускаем,
    игра всё равно запустится."""
    entries = CONFIG.get("EXTRA_CLIENT_MODS", [])
    if not entries:
        return

    cache = APP_DATA_DIR / "extra_client_mods_cache"
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / ".installed.json"
    installed = {}
    if marker.exists():
        try:
            installed = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            installed = {}

    changed = False
    for entry in entries:
        slug = entry.get("slug")
        if not slug:
            continue
        label = entry.get("label", slug)
        have = installed.get(slug)
        if have and (cache / have).exists():
            continue  # уже скачан в кэш
        if status_cb:
            status_cb("Ищу мод «%s»..." % label)
        try:
            filename, url = _find_modrinth_download(
                slug, CONFIG["MC_VERSION"], [CONFIG["MOD_LOADER"]]
            )
            if not filename or not url:
                if status_cb:
                    status_cb("Мод «%s» недоступен для %s — пропускаю." % (label, CONFIG["MC_VERSION"]))
                continue
            if status_cb:
                status_cb("Скачиваю мод «%s»..." % label)
            download_file(url, cache / filename)
            installed[slug] = filename
            changed = True
        except Exception:
            if status_cb:
                status_cb("Не удалось скачать мод «%s» — пропускаю, это не критично." % label)
            continue

    if changed:
        marker.write_text(json.dumps(installed), encoding="utf-8")

    # Копируем всё из кэша в mods/ (быстро, из интернета уже не тянем)
    mods_dir = INSTANCE_DIR / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    for slug, filename in installed.items():
        src = cache / filename
        if src.exists() and not (mods_dir / filename).exists():
            shutil.copy2(src, mods_dir / filename)


WINDOW_ICON_MOD_SLUG = "custom-window-title"


def install_game_window_icon(status_cb=None) -> None:
    """Скачивает мод Custom Window Title и настраивает его на нашу иконку —
    это меняет то, что показывается в панели задач и заголовке окна, когда
    запущена уже САМА игра (не лаунчер). Полностью необязательная,
    некритичная часть: любая ошибка здесь тихо пропускается, стандартная
    иконка Minecraft просто останется на месте, игра всё равно запустится."""
    if not CONFIG.get("SET_GAME_WINDOW_ICON"):
        return

    try:
        mods_dir = INSTANCE_DIR / "mods"
        config_dir = INSTANCE_DIR / "config"
        mods_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)

        marker = mods_dir / ".launcher_installed_window_icon_mod.json"
        already_filename = None
        if marker.exists():
            try:
                already_filename = json.loads(marker.read_text(encoding="utf-8")).get("filename")
            except Exception:
                already_filename = None

        if not already_filename or not (mods_dir / already_filename).exists():
            if status_cb:
                status_cb("Ищу мод для иконки окна игры...")
            filename, url = _find_modrinth_download(
                WINDOW_ICON_MOD_SLUG, CONFIG["MC_VERSION"], [CONFIG["MOD_LOADER"]],
            )
            if not filename or not url:
                return  # для этой версии/загрузчика мода нет — просто пропускаем
            if status_cb:
                status_cb("Скачиваю мод для иконки окна игры...")
            download_file(url, mods_dir / filename)
            marker.write_text(json.dumps({"filename": filename}), encoding="utf-8")

        # Кладём саму картинку туда, где её ожидает мод
        icon_rel_path = "checkpoint_launcher/icon.png"
        icon_dest = config_dir / "checkpoint_launcher" / "icon.png"
        icon_dest.parent.mkdir(parents=True, exist_ok=True)
        bundled_icon = resource_path("window_icon.png")
        if bundled_icon.exists():
            shutil.copy2(bundled_icon, icon_dest)

        # Прописываем/обновляем конфиг мода (создаётся модом при первом
        # запуске — но мы можем и создать/дополнить его заранее, до этого)
        config_path = config_dir / "customwindowtitle-client.toml"
        icon_line = "icon = '%s'" % icon_rel_path
        title_line = "title = '%s {mcversion}'" % CONFIG["PACK_NAME"]

        lines = []
        icon_written = False
        title_written = False
        if config_path.exists():
            for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("icon"):
                    lines.append(icon_line)
                    icon_written = True
                elif stripped.startswith("title"):
                    lines.append(title_line)
                    title_written = True
                else:
                    lines.append(line)
        if not icon_written:
            lines.append(icon_line)
        if not title_written:
            lines.append(title_line)

        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if status_cb:
            status_cb("Иконка окна игры настроена.")
    except Exception:
        if status_cb:
            status_cb("Не удалось настроить иконку окна игры — пропускаю, это не критично.")


# Настройки графики, которые применяются в режиме "для слабых ПК". Ключи
# продублированы для старого и нового форматов options.txt (в разных
# версиях Minecraft часть настроек называется по-разному) — лишний ключ
# игра просто проигнорирует, это безопасно.
LOW_END_OPTIONS = {
    "renderDistance": "6",
    "simulationDistance": "6",
    "graphicsMode": "0",       # 0 = Быстрая (новые версии)
    "fancyGraphics": "false",  # то же самое, старое название ключа
    "ao": "0",                 # плавное освещение выключено
    "particles": "2",          # 2 = минимум частиц
    "cloudStatus": "0",        # 0 = облака выключены (новые версии)
    "clouds": "false",         # то же самое, старое название ключа
    "entityShadows": "false",
    "biomeBlendRadius": "0",
}

OPTIONS_BACKUP_FILE = APP_DATA_DIR / "options_backup_before_low_end.txt"
IRIS_CONFIG_BACKUP_FILE = APP_DATA_DIR / "iris_backup_before_low_end.properties"


def _read_options_txt(path: Path) -> dict:
    data = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                data[key] = value
    return data


def _write_options_txt(path: Path, data: dict) -> None:
    lines = ["%s:%s" % (key, value) for key, value in data.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _apply_low_end_shaders(enabled: bool, status_cb=None) -> None:
    """Шейдеры — самая прожорливая часть графики, и хранятся отдельно от
    options.txt (свой файл у мода Iris), поэтому одних только настроек
    графики недостаточно — если игрок хоть раз включил шейдер вручную, он
    так и останется висеть, даже если включить "слабый ПК". Эта функция
    явно выключает шейдеры при включении режима и возвращает как было при
    выключении — точно так же, как apply_low_end_mode делает для
    options.txt."""
    config_dir = INSTANCE_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    iris_config_path = config_dir / "iris.properties"

    if enabled:
        if iris_config_path.exists() and not IRIS_CONFIG_BACKUP_FILE.exists():
            shutil.copy2(iris_config_path, IRIS_CONFIG_BACKUP_FILE)

        lines = []
        found = False
        if iris_config_path.exists():
            for line in iris_config_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("enableShaders"):
                    lines.append("enableShaders=false")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append("enableShaders=false")

        iris_config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if status_cb:
            status_cb("Шейдеры выключены (режим для слабых ПК).")
    else:
        if IRIS_CONFIG_BACKUP_FILE.exists():
            shutil.copy2(IRIS_CONFIG_BACKUP_FILE, iris_config_path)
            if status_cb:
                status_cb("Настройки шейдеров восстановлены.")


def apply_low_end_mode(enabled: bool, status_cb=None) -> None:
    """Включает или выключает упрощённую графику для слабых ПК. Настройки
    "до включения" сохраняются один раз в отдельный файл — при выключении
    режима они восстанавливаются как было. Применяется сразу, даже если
    это самый первый запуск игры и options.txt ещё не существует —
    отсутствующие в файле ключи Minecraft и так использует по умолчанию,
    так что создание файла только с нужными нам ключами ничего не ломает."""
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    options_path = INSTANCE_DIR / "options.txt"

    _apply_low_end_shaders(enabled, status_cb)

    if enabled:
        if options_path.exists() and not OPTIONS_BACKUP_FILE.exists():
            shutil.copy2(options_path, OPTIONS_BACKUP_FILE)
        current = _read_options_txt(options_path)  # {} если файла ещё нет — это ок
        current.update(LOW_END_OPTIONS)
        if status_cb:
            status_cb("Применяю настройки для слабых ПК...")
        _write_options_txt(options_path, current)
    else:
        # Список ресурс-паков запоминаем отдельно: восстановление резервной
        # копии вернуло бы старый список и стёрло паки, которые игрок включил
        # уже после включения слабого режима.
        saved_packs = _read_options_value("resourcePacks", None)
        if OPTIONS_BACKUP_FILE.exists():
            if status_cb:
                status_cb("Возвращаю обычные настройки графики...")
            shutil.copy2(OPTIONS_BACKUP_FILE, options_path)
            if saved_packs is not None:
                _write_options_value("resourcePacks", saved_packs)
        elif options_path.exists():
            # Резервной копии нет — значит "слабый ПК" включили ещё до
            # самого первого запуска игры, и сохранять было нечего. В этом
            # случае просто убираем из файла ровно те ключи, которые сами
            # туда добавили — Minecraft подставит для них свои дефолты,
            # остальные настройки (звук, управление и т.д.) не трогаем.
            current = _read_options_txt(options_path)
            for key in LOW_END_OPTIONS:
                current.pop(key, None)
            _write_options_txt(options_path, current)

    # Облегчённый ресурс-пак применяем ПОСЛЕДНИМ: выше options.txt мог быть
    # перезаписан целиком, и более ранняя правка списка паков потерялась бы.
    _apply_low_end_resource_pack(enabled, status_cb)


# Папки внутри экземпляра, которые можно спокойно удалять и качать заново —
# это только "система" (сама игра/моды/конфиги). saves (миры), screenshots
# и options.txt сюда специально не входят, чтобы кнопка "Починить" никогда
# не задевала то, что жалко потерять.
REPAIRABLE_FOLDERS = ["versions", "libraries", "assets", "mods", "config",
                      "resourcepacks", "shaderpacks", "kubejs"]


def repair_installation(status_cb=None, progress_cb=None) -> None:
    """Полностью удаляет файлы установки Minecraft/NeoForge/модов и сбрасывает
    все внутренние метки лаунчера, чтобы при следующем запуске всё
    поставилось заново с нуля. Миры (saves), скриншоты и настройки
    (options.txt) не трогает."""
    total = len(REPAIRABLE_FOLDERS) or 1
    for index, folder in enumerate(REPAIRABLE_FOLDERS, start=1):
        target = INSTANCE_DIR / folder
        if target.exists():
            if status_cb:
                status_cb("Удаляю старые файлы: %s..." % folder)
            shutil.rmtree(target, ignore_errors=True)
        if progress_cb:
            progress_cb(int(index * 100 / total))

    if OPTIONAL_CACHE_DIR.exists():
        shutil.rmtree(OPTIONAL_CACHE_DIR, ignore_errors=True)

    MODPACK_VERSION_FILE.unlink(missing_ok=True)
    INSTALL_MARKER_FILE.unlink(missing_ok=True)

    if status_cb:
        status_cb("Старые файлы удалены, ставлю всё заново...")
    if progress_cb:
        progress_cb(0)


LOADER_DISPLAY_NAMES = {
    "neoforge": "NeoForge",
    "forge": "Forge",
    "fabric": "Fabric",
    "quilt": "Quilt",
}


def _install_signature() -> dict:
    return {
        "mc_version": CONFIG["MC_VERSION"],
        "mod_loader": CONFIG["MOD_LOADER"],
        "loader_version": CONFIG["LOADER_VERSION"],
    }


def _read_install_marker():
    if INSTALL_MARKER_FILE.exists():
        try:
            return json.loads(INSTALL_MARKER_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _write_install_marker(version_id: str) -> None:
    data = _install_signature()
    data["version_id"] = version_id
    INSTALL_MARKER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_bundled_java(status_cb=None):
    """Пытается найти Java, которую minecraft-launcher-lib уже сама
    скачала вместе с игрой (у Mojang есть своя версия Java под каждую
    версию Minecraft, отдельно от системной). Это нужно, чтобы установщик
    NeoForge/Forge не полагался на то, стоит ли Java в самой системе —
    раньше без этого установка падала с ошибкой на компьютерах, где Java
    не была установлена отдельно. Если что-то пойдёт не так — просто
    возвращает None, и всё продолжит работать как раньше."""
    try:
        info = mll.runtime.get_version_runtime_information(CONFIG["MC_VERSION"], str(INSTANCE_DIR))
        if not info or not info.get("name"):
            return None
        jvm_version = info["name"]
        _install_with_retry(
            mll.runtime.install_jvm_runtime, jvm_version, str(INSTANCE_DIR), status_cb=status_cb,
        )
        return mll.runtime.get_executable_path(jvm_version, str(INSTANCE_DIR))
    except Exception:
        return None


class LaunchProgress:
    """Сквозная полоса загрузки на весь запуск.

    Раньше каждый установщик гнал полосу от 0 до 100 по-своему: Minecraft
    доезжал до 100%, потом с нуля стартовал NeoForge, потом моды — со стороны
    это выглядело как дублирующаяся загрузка, и было непонятно, на каком ты
    этапе. Теперь этапы идут друг за другом, каждый занимает свой участок
    общей шкалы, а в подписи всегда виден номер шага."""

    def __init__(self, status_cb, progress_cb, stages):
        self._status_cb = status_cb
        self._progress_cb = progress_cb
        self._stages = list(stages)  # [(название, вес), ...]
        self._total = sum(weight for _, weight in self._stages) or 1

    def scoped(self, title):
        """Пара колбэков (status, progress) для одного этапа: проценты этапа
        пересчитываются в общий отрезок шкалы."""
        index, done, weight = 1, 0, 1
        for position, (name, stage_weight) in enumerate(self._stages):
            if name == title:
                index = position + 1
                weight = stage_weight
                done = sum(w for _, w in self._stages[:position])
                break
        prefix = "Шаг %d/%d · %s" % (index, len(self._stages), title)

        def stage_status(text=""):
            text = str(text or "").strip()
            self._status_cb("%s — %s" % (prefix, text) if text else prefix + "...")

        def stage_progress(pct):
            pct = min(100, max(0, int(pct or 0)))
            overall = (done + weight * pct / 100.0) * 100.0 / self._total
            self._progress_cb(int(overall))
            self._status_cb("%s — %d%%" % (prefix, pct))

        return stage_status, stage_progress


def install_minecraft_and_modloader(progress: "LaunchProgress") -> str:
    """Ставит ванильный Minecraft нужной версии + модлоадер (NeoForge и т.д.).
    Возвращает id версии, которую нужно передать в запуск игры.

    Если версия Minecraft/модлоадера в CONFIG не менялась с прошлого
    успешного запуска и нужные файлы всё ещё на месте — скачивание и
    установка полностью пропускаются, лаунчер сразу переходит дальше."""
    loader_id = CONFIG["MOD_LOADER"]
    loader_name = LOADER_DISPLAY_NAMES.get(loader_id, loader_id.capitalize())

    java_status, _java_progress = progress.scoped("Java")
    mc_status, mc_progress = progress.scoped("Minecraft")
    loader_status, loader_progress = progress.scoped(loader_name)

    marker = _read_install_marker()
    if marker and marker.get("version_id") and all(
        marker.get(key) == value for key, value in _install_signature().items()
    ):
        version_id = marker["version_id"]
        version_json = INSTANCE_DIR / "versions" / version_id / ("%s.json" % version_id)
        if version_json.exists():
            loader_status("уже установлено")
            loader_progress(100)
            return version_id
        # Файл почему-то пропал (например, папку версий удалили руками) —
        # доверять метке нельзя, ставим заново как обычно.

    def callback_dict(stage_progress):
        # Библиотека minecraft-launcher-lib шлёт технические сообщения вроде
        # "Install java runtime" — они не переведены и не нужны игроку.
        # Вместо них считаем честный процент по факту скачанных файлов
        # (setMax/setProgress); подпись со шагом ставит сам stage_progress.
        state = {"max": 0, "last_pct": -1}

        def set_status(_text):
            pass  # технический текст библиотеки скрываем

        def set_progress(value):
            max_value = state["max"] or 1
            pct = min(100, max(0, int(value * 100 / max_value)))
            if pct != state["last_pct"]:
                state["last_pct"] = pct
                stage_progress(pct)

        def set_max(value):
            state["max"] = value or 1

        return {
            "setStatus": set_status,
            "setProgress": set_progress,
            "setMax": set_max,
        }

    mc_status("подготовка")
    _install_with_retry(
        mll.install.install_minecraft_version,
        CONFIG["MC_VERSION"], str(INSTANCE_DIR),
        callback=callback_dict(mc_progress),
        status_cb=mc_status,
    )

    loader_status("подготовка")
    mod_loader = mll.mod_loader.get_mod_loader(loader_id)

    if not mod_loader.is_minecraft_version_supported(CONFIG["MC_VERSION"]):
        raise RuntimeError(
            "%s не поддерживает Minecraft %s" % (loader_id, CONFIG["MC_VERSION"])
        )

    loader_version = CONFIG["LOADER_VERSION"] or None
    java_path = _find_bundled_java(java_status)

    # install() сам ставит модлоадер (и ванильную версию, если её вдруг нет)
    # и возвращает id версии, который нужно передать в get_minecraft_command.
    # java=java_path — используем Java, которую уже скачал сам
    # minecraft-launcher-lib для игры, а не полагаемся на системную Java
    # (если её на компьютере нет — установка NeoForge/Forge падает с
    # ошибкой "returned non-zero exit status 1").
    version_id = _install_with_retry(
        mod_loader.install,
        CONFIG["MC_VERSION"],
        str(INSTANCE_DIR),
        loader_version=loader_version,
        callback=callback_dict(loader_progress),
        java=java_path,
        status_cb=loader_status,
    )
    _write_install_marker(version_id)
    return version_id


def deploy_test_mods(status_cb, progress_cb) -> bool:
    """Тестовый режим (кнопка "Играть (тест)"): разворачивает в клиент РОВНО
    те моды, что лежат в папке локального тестового сервера (плюс папка
    client_only_mods с чисто клиентскими вроде шейдеров) — чтобы клиент и
    сервер точно совпадали. Возвращает False, если тест-папок нет на диске
    (тогда вызывающий код откатывается на обычную установку сборки)."""
    base = Path.home() / "Desktop" / "Checkpoint Launcher-Server"
    src_dirs = [base / "TestServer" / "mods", base / "client_only_mods"]
    src_dirs = [d for d in src_dirs if d.is_dir()]
    if not src_dirs:
        return False

    jars = []
    for d in src_dirs:
        jars += sorted(d.glob("*.jar"))
    if not jars:
        return False

    mods_dst = INSTANCE_DIR / "mods"
    if mods_dst.exists():
        shutil.rmtree(mods_dst)
    mods_dst.mkdir(parents=True, exist_ok=True)

    total = len(jars)
    for index, jar in enumerate(jars, start=1):
        shutil.copy2(jar, mods_dst / jar.name)
        progress_cb(int(index * 100 / total))
        status_cb("Тест-сборка: копирую моды %d/%d" % (index, total))

    # Помечаем, что в клиенте сейчас ТЕСТОВЫЕ моды, чтобы обычная кнопка
    # "Играть" потом переустановила настоящую сборку с сервера-хостинга.
    MODPACK_VERSION_FILE.write_text("TEST")
    return True


def launch_game(username: str, memory_mb: int, low_end_enabled: bool, status_cb, progress_cb, server_override=None, test_mode=False):
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

    loader_name = LOADER_DISPLAY_NAMES.get(
        CONFIG["MOD_LOADER"], CONFIG["MOD_LOADER"].capitalize())
    # Одна полоса на весь запуск: этапы идут по очереди, каждый занимает свой
    # участок шкалы. Вес — примерная доля времени этапа.
    progress = LaunchProgress(status_cb, progress_cb, [
        ("Java", 8),
        ("Minecraft", 20),
        (loader_name, 18),
        ("Сборка модов", 34),
        ("Моды и дополнения", 20),
    ])

    version_id = install_minecraft_and_modloader(progress)

    pack_status, pack_progress = progress.scoped("Сборка модов")
    extras_status, extras_progress = progress.scoped("Моды и дополнения")

    if test_mode and deploy_test_mods(pack_status, pack_progress):
        # Тест-режим: моды взяты один-в-один с локального сервера,
        # обычную установку сборки и опциональные моды пропускаем.
        pack_status("тестовая сборка развёрнута (моды с локального сервера)")
    else:
        if get_local_modpack_version() != get_remote_modpack_version():
            install_modpack(pack_status, pack_progress)
        else:
            pack_status("сборка уже актуальна")
            pack_progress(100)

        remove_blocked_mods(extras_status)
        harvest_optional_mods(extras_status)
        restore_no_longer_optional_mods(extras_status)
        apply_optional_mods(extras_status, extras_progress)

    ensure_pinned_server(extras_status)
    apply_low_end_mode(low_end_enabled, extras_status)
    install_extra_shaderpacks(extras_status, extras_progress)
    install_game_window_icon(extras_status)
    install_extra_client_mods(extras_status, extras_progress)

    progress_cb(100)
    status_cb("Запуск игры...")

    options = {
        "username": username,
        "uuid": offline_uuid(username),
        "token": "",
        "jvmArguments": ["-Xmx%dM" % memory_mb, "-Xms1024M"],
    }

    # Автоподключение к серверу — это штатная возможность самого
    # Minecraft (флаги --server/--port), заходит сразу в игру на сервере,
    # минуя главное меню.
    pinned = CONFIG.get("PINNED_SERVER") or {}
    # server_override — заход на конкретный сервер (кнопка "Играть (тест)").
    # Если не задан — обычное поведение: авто-заход на PINNED_SERVER.
    join_ip = server_override or (pinned.get("ip") if CONFIG.get("AUTO_JOIN_SERVER") else None)
    if join_ip:
        host, port = parse_host_port(join_ip)
        options["server"] = host
        options["port"] = str(port)

    command = mll.command.get_minecraft_command(version_id, str(INSTANCE_DIR), options)

    # Java — консольное приложение. Если запустить её "как есть" из .exe,
    # собранного с --noconsole, Windows сама откроет для неё отдельное
    # чёрное окно консоли — и если игрок его закроет, закроется и игра.
    # Поэтому прячем консоль явно и пишем весь вывод в лог-файл (пригодится
    # для отладки, если игра будет вылетать).
    log_path = INSTANCE_DIR / "latest_launch.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")

    popen_kwargs = {"cwd": str(INSTANCE_DIR), "stdout": log_file, "stderr": subprocess.STDOUT}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    process = subprocess.Popen(command, **popen_kwargs)
    status_cb("Готово! Игра запускается отдельным окном.")
    return process


# ============================ GUI ============================

class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.resizable(False, False)

        try:
            root.iconbitmap(str(resource_path("icon.ico")))
        except Exception:
            pass  # иконки нет рядом или ОС не Windows — не критично, используем стандартную

        settings = load_settings()
        self.theme_name = settings.get("theme") if settings.get("theme") in THEMES else "dark"

        # Определяем реальный объём ОЗУ игрока, чтобы не дать выставить
        # больше, чем физически есть в компьютере.
        system_ram = get_system_ram_mb()
        configured_max = CONFIG.get("MEMORY_MAX_MB", 16384)
        if system_ram:
            # Оставляем системе минимум 2 ГБ "про запас"
            self.memory_max = max(CONFIG.get("MEMORY_MIN_MB", 1024), min(configured_max, system_ram - 2048))
        else:
            self.memory_max = configured_max
        self.memory_min = min(CONFIG.get("MEMORY_MIN_MB", 1024), self.memory_max)

        saved_memory = settings.get("memory_mb", CONFIG["MEMORY_MB"])
        saved_memory = max(self.memory_min, min(self.memory_max, saved_memory))

        # Переменные создаются один раз и переживают перерисовку интерфейса
        # (например, при переключении темы) — введённый ник и выбранная
        # память не сбрасываются.
        self.nick_var = tk.StringVar(value=settings.get("username", ""))
        self.memory_var = tk.IntVar(value=saved_memory)
        self.low_end_var = tk.BooleanVar(value=settings.get("low_end_mode", False))
        self.status_var = tk.StringVar(value="Готово к запуску")
        self.progress_var = tk.IntVar(value=0)

        self.server_status_var = tk.StringVar(value="○  Проверка сервера...")
        self.server_status_color_key = "fg_muted"
        self.server_status_label = None
        self.game_process = None

        self.update_banner_var = tk.StringVar(value="")
        self.update_info = None
        self.update_banner = None
        self.version_row = None

        # Закрытие окна крестиком должно гарантированно завершать процесс.
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._geometry_set = False
        self._build_ui()
        self.refresh_server_status()
        self._check_launcher_update_async()
        self._serve_single_instance()

    # ------------------------------------------------------------------
    # Окно: показать, вернуть на экран, второй запуск, корректный выход

    def _center_window(self) -> None:
        """Ставит окно по центру экрана. Без явной позиции Windows иногда
        открывала его на координатах отключённого второго монитора — окно
        как будто "пропадало"."""
        try:
            self.root.update_idletasks()
            width, height = self.root.winfo_width(), self.root.winfo_height()
            screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.root.geometry("+%d+%d" % (
                max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 3)))
        except tk.TclError:
            pass

    def _ensure_on_screen(self) -> None:
        """Если окно оказалось за пределами видимой области (сменилось
        разрешение, отключили второй монитор) — возвращает его на экран."""
        try:
            self.root.update_idletasks()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            if x < 0 or y < 0 or x + 60 > screen_w or y + 60 > screen_h:
                self._center_window()
        except tk.TclError:
            pass

    def show_window(self) -> None:
        """Показывает окно и выводит его вперёд — даже если оно свёрнуто,
        скрыто или уехало за экран. Вызывается и второй копией лаунчера,
        когда пользователь снова кликает по ярлыку."""
        try:
            self.root.deiconify()
            self.root.state("normal")
            self._ensure_on_screen()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(400, self._drop_topmost)
            self.root.focus_force()
        except tk.TclError:
            pass

    def _drop_topmost(self) -> None:
        # Держим "поверх всех окон" только миг, чтобы окно выскочило вперёд,
        # но не мешало потом.
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _serve_single_instance(self) -> None:
        """Слушает локальный порт: если пользователь снова запустит лаунчер
        через ярлык, вторая копия постучится сюда и закроется, а мы просто
        покажем уже открытое окно."""
        server = get_single_instance_server()
        if server is None:
            return  # порт занять не удалось — работаем без этой функции

        def worker():
            while True:
                try:
                    conn, _addr = server.accept()
                except OSError:
                    return  # сокет закрыт (выходим) — завершаем поток
                try:
                    conn.settimeout(2)
                    data = conn.recv(64)
                    conn.sendall(SINGLE_INSTANCE_TOKEN + b"\n")
                    if data.strip() == b"SHOW":
                        self.root.after(0, self.show_window)
                    # Ждём, пока закроется клиент, и только потом закрываемся
                    # сами: тогда "остаточное" состояние TIME_WAIT достаётся
                    # его временному порту, а не нашему 49517 — иначе
                    # следующий запуск лаунчера мог не занять порт обратно.
                    conn.recv(1)
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self) -> None:
        """Закрытие окна. Освобождаем порт и гасим интерфейс — процесс затем
        завершается принудительно в main(), чтобы exe не оставался висеть."""
        server = get_single_instance_server()
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Построение интерфейса (вызывается заново при смене темы)
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        colors = THEMES[self.theme_name]
        root = self.root

        for child in root.winfo_children():
            child.destroy()

        # Ссылки на PhotoImage должны жить, пока живут кнопки — иначе
        # Python соберёт их мусором и иконки пропадут с экрана.
        self.icons = load_icons(self.theme_name)

        width, height = 680, 680
        root.title(CONFIG["PACK_NAME"])
        if getattr(self, "_geometry_set", False):
            # Перестройка интерфейса (смена темы) — позицию окна не трогаем.
            root.geometry("%dx%d" % (width, height))
        else:
            # Позицию считаем ЗАРАНЕЕ и ставим одним вызовом вместе с размером.
            # Если центрировать после отрисовки, окно успевает мигнуть у левого
            # края экрана и только потом прыгает в центр.
            x = max(0, (root.winfo_screenwidth() - width) // 2)
            y = max(0, (root.winfo_screenheight() - height) // 3)
            root.geometry("%dx%d+%d+%d" % (width, height, x, y))
            self._geometry_set = True
        root.configure(bg=colors["bg_grad_top"])
        set_titlebar_dark(root, self.theme_name == "dark")
        self._setup_style(colors)

        # Фон окна: мягкий градиент + лёгкое свечение + матовая панель.
        # Всё это одна картинка, отрисованная Pillow (см. _render_window_backdrop) —
        # сам tkinter размытие и полупрозрачность не умеет.
        margin = 20
        radius = 30
        bg_canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, bd=0,
                              bg=colors["bg_grad_top"])
        bg_canvas.place(x=0, y=0, width=width, height=height)

        backdrop = _render_window_backdrop(width, height, colors, margin, radius,
                                           self.theme_name)
        if backdrop is not None:
            # Ссылку держим на self, иначе картинку соберёт сборщик мусора.
            self._backdrop_photo = ImageTk.PhotoImage(backdrop)
            bg_canvas.create_image(0, 0, image=self._backdrop_photo, anchor="nw")
        else:
            # Запасной вариант без Pillow — как было раньше.
            _draw_vertical_gradient(bg_canvas, width, height,
                                    colors["bg_grad_top"], colors["bg_grad_bottom"])
            _draw_rounded_rect(
                bg_canvas, margin, margin, width - margin, height - margin, radius,
                fill=colors["bg_panel"], outline=colors["border"], width=1,
            )

        content = tk.Frame(bg_canvas, bg=colors["bg_panel"])
        bg_canvas.create_window(
            margin + 2, margin + 2, window=content, anchor="nw",
            width=width - 2 * margin - 4, height=height - 2 * margin - 4,
        )

        inner = tk.Frame(content, bg=colors["bg_panel"])
        inner.pack(fill="both", expand=True, padx=48, pady=22)

        # Заголовок
        header_row = tk.Frame(inner, bg=colors["bg_panel"])
        header_row.pack(fill="x")

        title_col = tk.Frame(header_row, bg=colors["bg_panel"])
        title_col.pack(side="left", anchor="w")

        title_line = tk.Frame(title_col, bg=colors["bg_panel"])
        title_line.pack(anchor="w")
        if self.icons.get("gear"):
            tk.Label(title_line, image=self.icons["gear"], bg=colors["bg_panel"]).pack(side="left", padx=(0, 8))
        tk.Label(title_line, text=CONFIG["PACK_NAME"].upper(), font=("Segoe UI", 23, "bold"),
                 bg=colors["bg_panel"], fg=colors["fg"]).pack(side="left")
        if self.icons.get("gear"):
            tk.Label(title_line, image=self.icons["gear"], bg=colors["bg_panel"]).pack(side="left", padx=(8, 0))

        tk.Label(
            title_col,
            text="MINECRAFT %s  ·  %s" % (
                CONFIG["MC_VERSION"],
                LOADER_DISPLAY_NAMES.get(CONFIG["MOD_LOADER"], CONFIG["MOD_LOADER"].capitalize()).upper(),
            ),
            font=("Segoe UI", 8, "bold"), bg=colors["bg_panel"], fg=colors["accent"],
        ).pack(anchor="w", pady=(4, 0))

        theme_icon_img = self.icons["sun"] if self.theme_name == "dark" else self.icons["moon"]
        theme_btn = self._make_icon_button(header_row, theme_icon_img, colors, self.on_toggle_theme)
        theme_btn.pack(side="right", anchor="n")
        self._add_tooltip(theme_btn, "Светлая/тёмная тема", colors)

        tk.Frame(inner, bg=colors["accent_dim"], height=1).pack(fill="x", pady=(14, 10))

        self.server_status_label = tk.Label(
            inner, textvariable=self.server_status_var, font=("Segoe UI", 9, "bold"),
            bg=colors["bg_panel"], fg=colors[self.server_status_color_key], anchor="w",
        )
        self.server_status_label.pack(fill="x", pady=(0, 8))

        # Панель кнопок: Папка / Discord / Моды / Список модов / Починить
        toolbar = tk.Frame(inner, bg=colors["bg_panel"])
        toolbar.pack(anchor="w", pady=(0, 14))

        folder_btn = self._make_icon_button(toolbar, self.icons["folder"], colors, self.on_open_folder)
        folder_btn.pack(side="left")
        self._add_tooltip(folder_btn, "Папка с игрой", colors)

        if CONFIG.get("DISCORD_URL"):
            discord_btn = self._make_icon_button(
                toolbar, self.icons.get("discord") or self.icons["chat"],
                colors, self.on_open_discord)
            discord_btn.pack(side="left", padx=(8, 0))
            self._add_tooltip(discord_btn, "Discord", colors)

        if CONFIG.get("OPTIONAL_MODS"):
            mods_btn = self._make_icon_button(toolbar, self.icons["grid"], colors, self.on_open_optional_mods)
            mods_btn.pack(side="left", padx=(8, 0))
            self._add_tooltip(mods_btn, "Опциональные моды", colors)

        if CONFIG.get("MOD_SHOWCASE"):
            showcase_btn = self._make_icon_button(toolbar, self.icons["list"], colors, self.on_show_mod_list)
            showcase_btn.pack(side="left", padx=(8, 0))
            self._add_tooltip(showcase_btn, "Список модов сборки", colors)

        packs_btn = self._make_icon_button(
            toolbar, self.icons["image"], colors, self.on_open_resource_packs)
        packs_btn.pack(side="left", padx=(8, 0))
        self._add_tooltip(packs_btn, "Ресурс-паки (текстуры)", colors)

        repair_btn = self._make_icon_button(toolbar, self.icons["wrench"], colors, self.on_repair)
        repair_btn.pack(side="left", padx=(8, 0))
        self._add_tooltip(repair_btn, "Починить / переустановить", colors)

        settings_btn = self._make_icon_button(
            toolbar, self.icons["gear"], colors, self.on_open_install_settings)
        settings_btn.pack(side="left", padx=(8, 0))
        self._add_tooltip(settings_btn, "Папка установки игры", colors)

        # Ник
        tk.Label(inner, text="ВАШ НИК", font=("Segoe UI", 8, "bold"),
                 bg=colors["bg_panel"], fg=colors["fg_muted"]).pack(anchor="w")
        nick_entry = tk.Entry(
            inner, textvariable=self.nick_var, font=("Segoe UI", 13),
            bg=colors["bg_field"], fg=colors["fg"], insertbackground=colors["fg"],
            relief="flat", highlightthickness=1,
            highlightbackground=colors["border"], highlightcolor=colors["accent"],
        )
        nick_entry.pack(fill="x", ipady=7, pady=(5, 12))

        # Ползунок ОЗУ и «слабый режим» переехали в окно настроек (шестерёнка),
        # чтобы главное окно оставалось простым. Здесь — только короткая сводка
        # текущих значений, кликом открывается то же окно настроек.
        self.ram_value_label = None
        summary = tk.Frame(inner, bg=colors["bg_panel"], cursor="hand2")
        summary.pack(fill="x", pady=(0, 12))
        self.settings_summary_var = tk.StringVar(value=self._settings_summary_text())
        summary_label = tk.Label(
            summary, textvariable=self.settings_summary_var, font=("Segoe UI", 9),
            bg=colors["bg_panel"], fg=colors["fg_muted"], anchor="w", cursor="hand2",
        )
        summary_label.pack(side="left")
        change_link = tk.Label(
            summary, text="настроить", font=("Segoe UI", 9, "underline"),
            bg=colors["bg_panel"], fg=colors["accent"], cursor="hand2",
        )
        change_link.pack(side="right")
        for widget in (summary, summary_label, change_link):
            widget.bind("<Button-1>", lambda e: self.on_open_install_settings())

        # Кнопка играть — золотая "пилюля" на Canvas
        self.play_button = self._make_pill_button(
            inner, "ИГРАТЬ", colors, self.on_play,
            bg=colors["accent"], hover_bg=colors["accent_hover"],
            disabled_bg=colors["accent_dim"], fg=colors["accent_text"],
            height=50, font_size=14,
        )
        self.play_button.pack(fill="x", pady=(2, 6))

        # Вторая кнопка — быстрый заход на локальный тестовый сервер (localhost).
        self.play_test_button = self._make_pill_button(
            inner, "ИГРАТЬ (ТЕСТ — localhost)", colors, self.on_play_test,
            bg=colors["bg_field"], hover_bg=colors["accent_hover"],
            disabled_bg=colors["accent_dim"], fg=colors["fg"],
            height=38, font_size=11,
        )
        self.play_test_button.pack(fill="x", pady=(0, 14))

        # Статус + прогрессбар
        tk.Label(inner, textvariable=self.status_var, font=("Segoe UI", 9),
                 bg=colors["bg_panel"], fg=colors["fg_muted"], wraplength=540, justify="left",
                 anchor="w").pack(fill="x")

        self.progress = ttk.Progressbar(inner, mode="determinate", maximum=100,
                                         variable=self.progress_var,
                                         style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(8, 0))

        # Баннер "вышла новая версия лаунчера" — виден, только если
        # check_for_launcher_update() уже что-то нашёл к этому моменту
        self.update_banner = tk.Label(
            inner, textvariable=self.update_banner_var, font=("Segoe UI", 9, "bold"),
            bg=colors["bg_panel"], fg=colors["accent"], cursor="hand2", anchor="w",
        )
        self.update_banner.bind("<Button-1>", lambda e: self.on_open_update())
        if self.update_info:
            self.update_banner.pack(fill="x", pady=(10, 0))

        # Версия лаунчера + "что нового"
        version_row = tk.Frame(inner, bg=colors["bg_panel"])
        version_row.pack(fill="x", pady=(10, 0))
        self.version_row = version_row

        tk.Label(
            version_row, text="%s launcher v%s" % (CONFIG["PACK_NAME"], CONFIG.get("LAUNCHER_VERSION", "?")),
            font=("Segoe UI", 8), bg=colors["bg_panel"], fg=colors["fg_muted"],
        ).pack(side="left")

        if CONFIG.get("LAUNCHER_CHANGELOG"):
            changelog_link = tk.Label(
                version_row, text="что нового", font=("Segoe UI", 8, "underline"),
                bg=colors["bg_panel"], fg=colors["accent"], cursor="hand2",
            )
            changelog_link.pack(side="right")
            changelog_link.bind("<Button-1>", lambda e: self.on_show_changelog())

    def _make_icon_button(self, parent, image, colors, command, size=40):
        # Кнопку оборачиваем в Frame фиксированного пиксельного размера
        # (pack_propagate(False) не даёт ему сжаться/растянуться под
        # содержимое). Это специально, чтобы если картинка иконки вдруг
        # не загрузится (image=None), кнопка не "взорвалась" в размере —
        # у tk.Button ширина/высота без картинки считаются в символах, а
        # не в пикселях, и получается гигантская кнопка на всё окно.
        holder = tk.Frame(parent, width=size, height=size, bg=colors["bg_field"])
        holder.pack_propagate(False)

        btn = tk.Button(
            holder, image=image, bd=0,
            bg=colors["bg_field"], activebackground=colors["accent"],
            relief="flat", cursor="hand2", command=command,
        )
        btn.pack(fill="both", expand=True)
        btn.bind("<Enter>", lambda e: btn.configure(bg=colors["accent"]))
        btn.bind("<Leave>", lambda e: btn.configure(bg=colors["bg_field"]))
        return holder

    @staticmethod
    def _add_tooltip(widget, text, colors):
        """Маленькая подсказка-табличка при наведении на иконку без текста —
        чтобы было понятно, что каждая кнопка делает, не загромождая
        панель длинными подписями."""
        state = {"win": None}

        def show(_e):
            if state["win"] is not None:
                return
            win = tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            win.attributes("-topmost", True)
            x = widget.winfo_rootx() + widget.winfo_width() // 2
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            win.wm_geometry("+%d+%d" % (x, y))
            tk.Label(
                win, text=text, font=("Segoe UI", 8), bg=colors["fg"], fg=colors["bg_panel"],
                padx=8, pady=3,
            ).pack()
            state["win"] = win

        def hide(_e):
            if state["win"] is not None:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<Destroy>", hide, add="+")

    @staticmethod
    def _make_pill_button(parent, text, colors, command, bg, hover_bg, disabled_bg, fg,
                           height=48, font_size=13):
        """Кнопка в виде золотой "пилюли" со скруглёнными краями (рисуется
        на Canvas, обычный tk.Button не умеет в скруглённые углы)."""
        canvas = tk.Canvas(parent, height=height, highlightthickness=0, bd=0, bg=colors["bg_panel"])
        state = {"bg": bg, "enabled": True}

        def redraw():
            canvas.delete("all")
            w, h = canvas.winfo_width(), canvas.winfo_height()
            if w <= 1 or h <= 1:
                return
            fill = state["bg"] if state["enabled"] else disabled_bg
            _draw_rounded_rect(canvas, 1, 1, w - 1, h - 1, h // 2, fill=fill, outline="")
            canvas.create_text(w // 2, h // 2, text=text, fill=fg, font=("Segoe UI", font_size, "bold"))

        def on_enter(_e):
            if state["enabled"]:
                state["bg"] = hover_bg
                redraw()

        def on_leave(_e):
            if state["enabled"]:
                state["bg"] = bg
                redraw()

        def on_click(_e):
            if state["enabled"]:
                command()

        def set_enabled(value: bool) -> None:
            state["enabled"] = value
            state["bg"] = bg
            canvas.configure(cursor="hand2" if value else "arrow")
            redraw()

        canvas.bind("<Configure>", lambda e: redraw())
        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", on_click)
        canvas.configure(cursor="hand2")
        canvas.set_enabled = set_enabled  # прикрепляем метод для on_play()
        return canvas

    @staticmethod
    def _format_gb(mb: int) -> str:
        return "%.1f ГБ" % (mb / 1024)

    def _settings_summary_text(self) -> str:
        """Короткая строка на главном окне: сколько ОЗУ и включён ли слабый режим."""
        parts = ["Память: %s" % self._format_gb(self.memory_var.get())]
        if self.low_end_var.get():
            parts.append("режим для слабых ПК включён")
        return "  ·  ".join(parts)

    def _refresh_settings_summary(self) -> None:
        try:
            self.settings_summary_var.set(self._settings_summary_text())
        except (AttributeError, tk.TclError):
            pass

    def _on_ram_change(self, value) -> None:
        # Ползунок живёт в окне настроек и существует не всегда, поэтому
        # подпись обновляем только если она сейчас на экране.
        if self.ram_value_label is not None:
            try:
                self.ram_value_label.configure(text=self._format_gb(int(float(value))))
            except tk.TclError:
                pass
        self._refresh_settings_summary()

    def _setup_style(self, colors) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=colors["bg_field"], background=colors["accent"],
            bordercolor=colors["bg_field"], lightcolor=colors["accent"], darkcolor=colors["accent"],
            thickness=8,
        )

    def set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def set_progress(self, value: int) -> None:
        self.root.after(0, lambda: self.progress_var.set(value))

    def on_toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        update_settings(theme=self.theme_name)
        self._build_ui()

    def on_open_folder(self) -> None:
        try:
            open_folder(INSTANCE_DIR)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Не удалось открыть папку", str(exc))

    def on_open_discord(self) -> None:
        url = CONFIG.get("DISCORD_URL")
        if not url:
            return
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Не удалось открыть Discord", str(exc))

    def _open_recommended_packs(self, parent, refresh) -> None:
        """Окошко с готовыми паками: ставятся с Modrinth в один клик."""
        colors = THEMES[self.theme_name]
        dialog = tk.Toplevel(parent)
        dialog.title("Готовые ресурс-паки")
        dialog.configure(bg=colors["bg_panel"])
        dialog.resizable(False, False)
        dialog.transient(parent)
        dialog.geometry("560x420")
        set_titlebar_dark(dialog, self.theme_name == "dark")

        outer = tk.Frame(dialog, bg=colors["bg_panel"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(outer, text="Готовые ресурс-паки", font=("Segoe UI", 13, "bold"),
                 bg=colors["bg_panel"], fg=colors["fg"]).pack(anchor="w")
        tk.Label(outer, text="Проверенные паки — скачиваются с Modrinth одной кнопкой.",
                 font=("Segoe UI", 9), bg=colors["bg_panel"],
                 fg=colors["fg_muted"]).pack(anchor="w", pady=(2, 10))

        buttons = {}

        def make_handler(cfg, var):
            def handler():
                button = buttons.get(cfg["slug"])
                if button is not None:
                    button.configure(state="disabled")
                var.set("Скачиваю...")

                def worker():
                    try:
                        install_recommended_resource_pack(
                            cfg, status_cb=lambda t: dialog.after(0, lambda: var.set(t)))
                    except Exception as exc:  # noqa: BLE001
                        dialog.after(0, lambda e=exc: var.set("Ошибка: %s" % e))
                    else:
                        dialog.after(0, lambda: var.set("Установлен"))
                        dialog.after(0, refresh)
                    finally:
                        if button is not None:
                            dialog.after(0, lambda: button.configure(state="normal"))

                threading.Thread(target=worker, daemon=True).start()
            return handler

        for pack_cfg in CONFIG.get("RECOMMENDED_RESOURCE_PACKS", []):
            card = tk.Frame(outer, bg=colors["bg_field"],
                            highlightbackground=colors["border"], highlightthickness=1)
            card.pack(fill="x", pady=4)
            body = tk.Frame(card, bg=colors["bg_field"])
            body.pack(fill="x", padx=10, pady=8)

            state = tk.StringVar(
                value="Установлен" if is_recommended_pack_installed(pack_cfg) else "")

            get_btn = tk.Button(
                body, text="Установить", command=make_handler(pack_cfg, state),
                font=("Segoe UI", 9),
                bg=colors["accent"], fg=colors["accent_text"],
                activebackground=colors["accent_hover"], activeforeground=colors["accent_text"],
                relief="flat", cursor="hand2", bd=0, padx=12, pady=4)
            get_btn.pack(side="right")
            buttons[pack_cfg["slug"]] = get_btn

            tk.Label(body, textvariable=state, font=("Segoe UI", 8),
                     bg=colors["bg_field"], fg=colors["fg_muted"]).pack(side="right", padx=(0, 10))

            mid = tk.Frame(body, bg=colors["bg_field"])
            mid.pack(side="left", fill="x", expand=True)
            tk.Label(mid, text=pack_cfg["name"], font=("Segoe UI", 11, "bold"),
                     bg=colors["bg_field"], fg=colors["fg"], anchor="w").pack(anchor="w")
            tk.Label(mid, text=pack_cfg.get("description", ""), font=("Segoe UI", 8),
                     bg=colors["bg_field"], fg=colors["fg_muted"], anchor="w",
                     justify="left", wraplength=340).pack(anchor="w")

        dialog.grab_set()

    def _open_pack_manager(self, title, subtitle, empty_hint, kind,
                           list_fn, toggle_fn, delete_fn, install_fn,
                           show_recommended=False) -> None:
        """Общее окно менеджера паков — используется и для ресурс-паков, и для
        шейдеров. Отличаются только функции работы с файлами, которые
        передаются аргументами."""
        colors = THEMES[self.theme_name]

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=colors["bg_panel"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.geometry("640x640")
        set_titlebar_dark(dialog, self.theme_name == "dark")

        outer = tk.Frame(dialog, bg=colors["bg_panel"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        head = tk.Frame(outer, bg=colors["bg_panel"])
        head.pack(fill="x")
        tk.Label(head, text=title, font=("Segoe UI", 14, "bold"),
                 bg=colors["bg_panel"], fg=colors["fg"]).pack(side="left")

        tk.Label(outer, text=subtitle, font=("Segoe UI", 9), justify="left",
                 bg=colors["bg_panel"], fg=colors["fg_muted"]).pack(anchor="w", pady=(2, 12))

        list_container = tk.Frame(outer, bg=colors["bg_panel"],
                                  highlightbackground=colors["border"], highlightthickness=1)
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, bg=colors["bg_panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=colors["bg_panel"])
        scroll_frame.bind("<Configure>",
                          lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)
        dialog.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # Ссылки на картинки держим на self, иначе tkinter не хранит их сам.
        self._pack_icon_refs = {}

        def build_row(pack):
            card = tk.Frame(scroll_frame, bg=colors["bg_field"],
                            highlightbackground=colors["border"], highlightthickness=1)
            card.pack(fill="x", padx=8, pady=4)
            body = tk.Frame(card, bg=colors["bg_field"])
            body.pack(fill="x", padx=10, pady=8)

            # Превью пака (pack.png) или красивая стандартная картинка.
            holder = tk.Frame(body, bg=colors["bg_panel"], width=64, height=64)
            holder.pack(side="left", padx=(0, 12))
            holder.pack_propagate(False)
            preview = load_pack_preview(pack["path"], 64)
            if preview is None:
                preview = make_pack_placeholder(kind, 64, colors["bg_panel"], colors["accent"])
            icon_label = tk.Label(holder, bg=colors["bg_panel"])
            if preview is not None and _PIL_OK:
                try:
                    photo = ImageTk.PhotoImage(preview)
                    self._pack_icon_refs[str(pack["path"])] = photo
                    icon_label.configure(image=photo)
                except Exception:
                    icon_label.configure(text=pack["name"][:1].upper(),
                                         fg=colors["accent"], font=("Segoe UI", 20, "bold"))
            else:
                icon_label.configure(text=pack["name"][:1].upper(),
                                     fg=colors["accent"], font=("Segoe UI", 20, "bold"))
            icon_label.pack(fill="both", expand=True)

            actions = tk.Frame(body, bg=colors["bg_field"])
            actions.pack(side="right")

            def on_delete(p=pack):
                if not messagebox.askyesno(
                        "Удалить", "Удалить «%s»?\n\nФайл будет удалён с диска." % p["name"],
                        parent=dialog):
                    return
                try:
                    delete_fn(p)
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror("Не удалось удалить", str(exc), parent=dialog)
                refresh()

            tk.Button(actions, text="Удалить", command=on_delete,
                      font=("Segoe UI", 9), bg=colors["bg_panel"], fg=colors["status_offline"],
                      activebackground=colors["border"], activeforeground=colors["status_offline"],
                      relief="flat", cursor="hand2", bd=0, padx=10, pady=4).pack(side="right")

            var = tk.BooleanVar(value=pack["enabled"])

            def on_toggle(p=pack, v=var):
                try:
                    toggle_fn(p, v.get())
                except Exception as exc:  # noqa: BLE001
                    v.set(not v.get())
                    messagebox.showerror("Не удалось переключить", str(exc), parent=dialog)

            tk.Checkbutton(actions, text="Включён", variable=var, command=on_toggle,
                           font=("Segoe UI", 9), bg=colors["bg_field"], fg=colors["fg"],
                           activebackground=colors["bg_field"], activeforeground=colors["fg"],
                           selectcolor=colors["bg_panel"], highlightthickness=0, bd=0,
                           cursor="hand2").pack(side="right", padx=(0, 8))

            mid = tk.Frame(body, bg=colors["bg_field"])
            mid.pack(side="left", fill="x", expand=True)
            tk.Label(mid, text=pack["name"], font=("Segoe UI", 11, "bold"),
                     bg=colors["bg_field"], fg=colors["fg"], anchor="w").pack(anchor="w")
            if pack.get("description"):
                tk.Label(mid, text=pack["description"], font=("Segoe UI", 8),
                         bg=colors["bg_field"], fg=colors["fg_muted"], anchor="w",
                         justify="left", wraplength=330).pack(anchor="w")

        def build_available_row(cfg):
            """Строка готового пака, который ещё не установлен: превью с
            Modrinth, описание и кнопка «Установить»."""
            card = tk.Frame(scroll_frame, bg=colors["bg_panel"],
                            highlightbackground=colors["border"], highlightthickness=1)
            card.pack(fill="x", padx=8, pady=4)
            body = tk.Frame(card, bg=colors["bg_panel"])
            body.pack(fill="x", padx=10, pady=8)

            holder = tk.Frame(body, bg=colors["bg_field"], width=64, height=64)
            holder.pack(side="left", padx=(0, 12))
            holder.pack_propagate(False)
            icon_label = tk.Label(holder, bg=colors["bg_field"],
                                  text=cfg["name"][:1].upper(), fg=colors["accent"],
                                  font=("Segoe UI", 20, "bold"))
            icon_label.pack(fill="both", expand=True)

            # Картинку пака тянем с Modrinth в фоне, чтобы окно не подвисало.
            if _PIL_OK and cfg.get("slug"):
                def load_icon(c=cfg, label=icon_label):
                    pil = _load_mod_icon_image(c["slug"], 64)
                    if pil is None:
                        return
                    def show():
                        try:
                            photo = ImageTk.PhotoImage(pil)
                            self._pack_icon_refs["rec:" + c["slug"]] = photo
                            label.configure(image=photo, text="")
                        except Exception:
                            pass
                    dialog.after(0, show)
                threading.Thread(target=load_icon, daemon=True).start()

            state = tk.StringVar(value="")
            btn_holder = tk.Frame(body, bg=colors["bg_panel"])
            btn_holder.pack(side="right")

            def on_get(c=cfg, var=state, box=btn_holder):
                for child in box.winfo_children():
                    child.configure(state="disabled")
                var.set("Скачиваю...")

                def worker():
                    try:
                        install_recommended_resource_pack(
                            c, status_cb=lambda t: dialog.after(0, lambda: var.set(t)))
                    except Exception as exc:  # noqa: BLE001
                        dialog.after(0, lambda e=exc: var.set("Ошибка: %s" % e))
                        dialog.after(0, lambda: [w.configure(state="normal")
                                                 for w in box.winfo_children()])
                    else:
                        dialog.after(0, refresh)

                threading.Thread(target=worker, daemon=True).start()

            tk.Button(btn_holder, text="Установить", command=on_get, font=("Segoe UI", 9),
                      bg=colors["accent"], fg=colors["accent_text"],
                      activebackground=colors["accent_hover"],
                      activeforeground=colors["accent_text"],
                      relief="flat", cursor="hand2", bd=0, padx=12, pady=4).pack()

            mid = tk.Frame(body, bg=colors["bg_panel"])
            mid.pack(side="left", fill="x", expand=True)
            title_row = tk.Frame(mid, bg=colors["bg_panel"])
            title_row.pack(anchor="w", fill="x")
            tk.Label(title_row, text=cfg["name"], font=("Segoe UI", 11, "bold"),
                     bg=colors["bg_panel"], fg=colors["fg"]).pack(side="left")
            tk.Label(title_row, text="  готовый пак", font=("Segoe UI", 8),
                     bg=colors["bg_panel"], fg=colors["accent"]).pack(side="left")
            tk.Label(mid, text=cfg.get("description", ""), font=("Segoe UI", 8),
                     bg=colors["bg_panel"], fg=colors["fg_muted"], anchor="w",
                     justify="left", wraplength=330).pack(anchor="w")
            tk.Label(mid, textvariable=state, font=("Segoe UI", 8),
                     bg=colors["bg_panel"], fg=colors["fg_muted"], anchor="w").pack(anchor="w")

        def refresh():
            for child in scroll_frame.winfo_children():
                child.destroy()
            self._pack_icon_refs = {}
            packs = list_fn()
            for pack in packs:
                build_row(pack)

            # Готовые паки показываем сразу в этом же списке — отдельное окно
            # для них не нужно.
            if show_recommended:
                installed = {p["path"].name for p in packs}
                available = [cfg for cfg in CONFIG.get("RECOMMENDED_RESOURCE_PACKS", [])
                             if (_recommended_pack_filename(cfg["slug"]) or "") not in installed]
                if available:
                    tk.Label(scroll_frame, text="ГОТОВЫЕ ПАКИ — СТАВЯТСЯ В ОДИН КЛИК",
                             font=("Segoe UI", 8, "bold"), bg=colors["bg_panel"],
                             fg=colors["fg_muted"]).pack(anchor="w", padx=14, pady=(12, 4))
                    for cfg in available:
                        build_available_row(cfg)

            if not packs and not show_recommended:
                tk.Label(scroll_frame, text=empty_hint, font=("Segoe UI", 9),
                         bg=colors["bg_panel"], fg=colors["fg_muted"],
                         justify="left", wraplength=560).pack(anchor="w", padx=14, pady=18)

        def on_install():
            paths = filedialog.askopenfilenames(
                title="Выберите ZIP-архив(ы)",
                filetypes=[("ZIP-архивы", "*.zip"), ("Все файлы", "*.*")],
                parent=dialog)
            installed = 0
            for path in paths:
                try:
                    install_fn(path)
                    installed += 1
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror("Не удалось установить", str(exc), parent=dialog)
            if installed:
                refresh()

        tk.Button(head, text="Установить из ZIP...", command=on_install,
                  font=("Segoe UI", 10), bg=colors["accent"], fg=colors["accent_text"],
                  activebackground=colors["accent_hover"], activeforeground=colors["accent_text"],
                  relief="flat", cursor="hand2", bd=0, padx=14, pady=6).pack(side="right")

        refresh()
        dialog.grab_set()

    def on_open_resource_packs(self) -> None:
        self._open_pack_manager(
            title="Ресурс-паки",
            subtitle="Готовые паки ставятся в один клик прямо из списка ниже.\n"
                     "Галочка «Включён» сразу включает пак в самой игре.",
            empty_hint="Пока ничего не установлено.",
            kind="image",
            list_fn=list_resource_packs,
            toggle_fn=set_resource_pack_enabled,
            delete_fn=delete_resource_pack,
            install_fn=install_resource_pack,
            show_recommended=True,
        )

    def on_open_install_settings(self) -> None:
        """Окно выбора папки установки: показывает текущий путь и позволяет
        перенести игру в другое место вместе со всеми данными."""
        colors = THEMES[self.theme_name]

        dialog = tk.Toplevel(self.root)
        dialog.title("Настройки")
        dialog.configure(bg=colors["bg_panel"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.geometry("560x560")
        set_titlebar_dark(dialog, self.theme_name == "dark")

        outer = tk.Frame(dialog, bg=colors["bg_panel"])
        outer.pack(fill="both", expand=True, padx=18, pady=18)

        # ---- Оперативная память (переехала сюда с главного окна) ----
        ram_row = tk.Frame(outer, bg=colors["bg_panel"])
        ram_row.pack(fill="x")
        tk.Label(ram_row, text="ОПЕРАТИВНАЯ ПАМЯТЬ", font=("Segoe UI", 8, "bold"),
                 bg=colors["bg_panel"], fg=colors["fg_muted"]).pack(side="left")
        self.ram_value_label = tk.Label(
            ram_row, text=self._format_gb(self.memory_var.get()), font=("Segoe UI", 9, "bold"),
            bg=colors["bg_panel"], fg=colors["accent"])
        self.ram_value_label.pack(side="right")

        tk.Scale(
            outer, from_=self.memory_min, to=self.memory_max,
            orient="horizontal", variable=self.memory_var,
            showvalue=False, resolution=256, sliderlength=18,
            bg=colors["bg_panel"], fg=colors["fg"], troughcolor=colors["bg_field"],
            highlightthickness=0, bd=0, activebackground=colors["accent_hover"],
            command=self._on_ram_change,
        ).pack(fill="x", pady=(6, 2))

        range_row = tk.Frame(outer, bg=colors["bg_panel"])
        range_row.pack(fill="x", pady=(0, 12))
        tk.Label(range_row, text=self._format_gb(self.memory_min), font=("Segoe UI", 8),
                 bg=colors["bg_panel"], fg=colors["fg_muted"]).pack(side="left")
        tk.Label(range_row, text=self._format_gb(self.memory_max), font=("Segoe UI", 8),
                 bg=colors["bg_panel"], fg=colors["fg_muted"]).pack(side="right")

        # ---- Режим для слабых ПК ----
        def on_low_end_toggle():
            update_settings(low_end_mode=self.low_end_var.get())
            self._refresh_settings_summary()

        tk.Checkbutton(
            outer, text=" Режим для слабых ПК — проще графика, выше FPS,\n"
                        " автоматически включается облегчённый ресурс-пак",
            image=self.icons.get("gauge"), compound="left",
            variable=self.low_end_var, font=("Segoe UI", 9), command=on_low_end_toggle,
            bg=colors["bg_panel"], fg=colors["fg"], activebackground=colors["bg_panel"],
            activeforeground=colors["fg"], selectcolor=colors["bg_field"],
            highlightthickness=0, bd=0, cursor="hand2", anchor="w", justify="left",
        ).pack(fill="x", pady=(0, 6))

        # При закрытии окна запоминаем выбранный объём памяти.
        dialog.bind("<Destroy>", lambda e: (
            update_settings(memory_mb=int(self.memory_var.get())),
            self._refresh_settings_summary()) if e.widget is dialog else None)

        tk.Frame(outer, bg=colors["border"], height=1).pack(fill="x", pady=(6, 14))

        tk.Label(outer, text="Папка установки игры", font=("Segoe UI", 14, "bold"),
                 bg=colors["bg_panel"], fg=colors["fg"]).pack(anchor="w")
        tk.Label(
            outer,
            text="Здесь лежат Minecraft, моды и ваши миры. Игру можно перенести на\n"
                 "другой диск — все миры, настройки и скриншоты переедут вместе с ней.",
            font=("Segoe UI", 9), bg=colors["bg_panel"], fg=colors["fg_muted"],
            justify="left",
        ).pack(anchor="w", pady=(2, 14))

        tk.Label(outer, text="ТЕКУЩАЯ ПАПКА", font=("Segoe UI", 8, "bold"),
                 bg=colors["bg_panel"], fg=colors["fg_muted"]).pack(anchor="w")
        path_var = tk.StringVar(value=str(INSTANCE_DIR))
        path_box = tk.Entry(
            outer, textvariable=path_var, font=("Segoe UI", 10), state="readonly",
            readonlybackground=colors["bg_field"], fg=colors["fg"],
            relief="flat", highlightthickness=1,
            highlightbackground=colors["border"], highlightcolor=colors["accent"],
        )
        path_box.pack(fill="x", ipady=6, pady=(5, 14))

        status_var = tk.StringVar(value="")
        status_label = tk.Label(
            outer, textvariable=status_var, font=("Segoe UI", 9), wraplength=510,
            justify="left", bg=colors["bg_panel"], fg=colors["fg_muted"], anchor="w")
        status_label.pack(fill="x", pady=(0, 12))

        buttons = tk.Frame(outer, bg=colors["bg_panel"])
        buttons.pack(fill="x")

        def set_busy(busy: bool):
            state = "disabled" if busy else "normal"
            change_btn.configure(state=state)
            default_btn.configure(state=state)

        def do_move(target: Path):
            set_busy(True)

            def worker():
                try:
                    move_installation(
                        target,
                        status_cb=lambda text: dialog.after(0, lambda t=text: status_var.set(t)),
                    )
                except Exception as exc:  # noqa: BLE001
                    dialog.after(0, lambda e=exc: status_var.set("Не удалось: %s" % e))
                    dialog.after(0, lambda e=exc: messagebox.showerror(
                        "Не удалось перенести", str(e), parent=dialog))
                else:
                    dialog.after(0, lambda: path_var.set(str(INSTANCE_DIR)))
                finally:
                    dialog.after(0, lambda: set_busy(False))

            threading.Thread(target=worker, daemon=True).start()

        def pick_target(target: Path):
            if self.game_process is not None and self.game_process.poll() is None:
                messagebox.showinfo(
                    "Игра запущена",
                    "Закройте Minecraft, прежде чем переносить игру.", parent=dialog)
                return
            if target == INSTANCE_DIR:
                status_var.set("Игра уже находится в этой папке.")
                return
            if not messagebox.askyesno(
                    "Перенос игры",
                    "Перенести игру сюда?\n\n%s\n\nВсе миры и настройки сохранятся.\n"
                    "Перенос на другой диск может занять несколько минут." % target,
                    parent=dialog):
                return
            do_move(target)

        def on_change():
            chosen = filedialog.askdirectory(
                title="Выберите папку для установки игры", parent=dialog)
            if not chosen:
                return
            target = Path(chosen)
            # В чужую непустую папку не мусорим — создаём внутри неё подпапку
            # с названием сборки.
            if target.exists() and any(target.iterdir()) and target != INSTANCE_DIR:
                target = target / CONFIG["PACK_NAME"]
            pick_target(target)

        def on_default():
            pick_target(DEFAULT_INSTANCE_DIR)

        change_btn = tk.Button(
            buttons, text="Выбрать другую папку...", command=on_change,
            font=("Segoe UI", 10), bg=colors["accent"], fg=colors["accent_text"],
            activebackground=colors["accent_hover"], activeforeground=colors["accent_text"],
            relief="flat", cursor="hand2", bd=0, padx=14, pady=7,
        )
        change_btn.pack(side="left")

        default_btn = tk.Button(
            buttons, text="Вернуть на диск C", command=on_default,
            font=("Segoe UI", 10), bg=colors["bg_field"], fg=colors["fg"],
            activebackground=colors["border"], activeforeground=colors["fg"],
            relief="flat", cursor="hand2", bd=0, padx=14, pady=7,
        )
        default_btn.pack(side="left", padx=(8, 0))

        tk.Button(
            buttons, text="Открыть папку", command=lambda: open_folder(INSTANCE_DIR),
            font=("Segoe UI", 10), bg=colors["bg_field"], fg=colors["fg"],
            activebackground=colors["border"], activeforeground=colors["fg"],
            relief="flat", cursor="hand2", bd=0, padx=14, pady=7,
        ).pack(side="right")

        dialog.grab_set()

    def on_open_optional_mods(self) -> None:
        colors = THEMES[self.theme_name]
        current = get_optional_mods_selection()

        dialog = tk.Toplevel(self.root)
        dialog.title("Опциональные моды")
        dialog.configure(bg=colors["bg_panel"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.geometry("470x610")
        set_titlebar_dark(dialog, self.theme_name == "dark")

        outer = tk.Frame(dialog, bg=colors["bg_panel"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        tk.Label(
            outer, text="Опциональные моды", font=("Segoe UI", 14, "bold"),
            bg=colors["bg_panel"], fg=colors["fg"],
        ).pack(anchor="w")
        tk.Label(
            outer,
            text="Эти моды безопасно включать и выключать в любой момент —\nвключили галочку, мод сразу скачается; убрали — сразу удалится.",
            font=("Segoe UI", 9), bg=colors["bg_panel"], fg=colors["fg_muted"],
            justify="left",
        ).pack(anchor="w", pady=(2, 12))

        # Прокручиваемый список (модов может быть много)
        list_container = tk.Frame(outer, bg=colors["bg_panel"], highlightbackground=colors["border"],
                                   highlightthickness=1)
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, bg=colors["bg_panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=colors["bg_panel"])

        scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)
        dialog.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        checkbox_vars = {}
        status_labels = {}
        checkboxes = {}

        def set_row_status(mod_id, text, color=None):
            label = status_labels.get(mod_id)
            if label is not None:
                label.configure(text=text, fg=color or colors["fg_muted"])

        def on_toggle(mod):
            var = checkbox_vars[mod["id"]]
            cb = checkboxes[mod["id"]]
            enabled = var.get()

            # Сохраняем выбор игрока НА ДИСК СРАЗУ, ещё до попытки
            # физически переложить файл. Раньше порядок был обратный —
            # если попытка ниже падала с ошибкой (например, антивирус на
            # секунду заблокировал только что распакованный файл), выбор
            # вообще не сохранялся, хотя галочка на экране уже выглядела
            # снятой. Теперь ваше намерение в любом случае не потеряется:
            # если применить прямо сейчас не получится, оно всё равно
            # точно применится при следующем запуске игры.
            selection = get_optional_mods_selection()
            selection[mod["id"]] = enabled
            save_optional_mods_selection(selection)

            cb.configure(state="disabled")
            set_row_status(mod["id"], "Применяю...")

            def worker():
                try:
                    error = _apply_one_optional_mod(
                        mod, enabled,
                        status_cb=lambda text: dialog.after(
                            0, lambda t=text: set_row_status(mod["id"], t)
                        ),
                    )
                except (PermissionError, OSError):
                    # Временная блокировка файла (обычно антивирус). Выбор
                    # уже сохранён выше, файл доустановится сам при
                    # следующем запуске игры — откатывать галочку не нужно.
                    def show_retry_later():
                        set_row_status(
                            mod["id"], "Применится при следующем запуске игры"
                        )
                        cb.configure(state="normal")

                    dialog.after(0, show_retry_later)
                    return

                def finish():
                    if error:
                        # А вот это уже не временная ситуация, а реальная
                        # проблема конфигурации (имя файла в CONFIG не
                        # совпадает с тем, что реально лежит в mods/) —
                        # здесь откат галочки и выбора оправдан.
                        var.set(not enabled)
                        selection[mod["id"]] = not enabled
                        save_optional_mods_selection(selection)
                        set_row_status(mod["id"], "Ошибка: файл не найден", "#e05555")
                    else:
                        set_row_status(mod["id"], "Включено" if enabled else "Выключено")
                    cb.configure(state="normal")

                dialog.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        # Ссылки на картинки держим на self, иначе tkinter не хранит их сам и
        # иконки пропадут после сборки мусора.
        self._opt_icon_refs = {}
        icon_labels = {}

        def apply_icon(mod_id, pil_img):
            lbl = icon_labels.get(mod_id)
            if lbl is None or pil_img is None:
                return
            try:
                photo = ImageTk.PhotoImage(pil_img)
                self._opt_icon_refs[mod_id] = photo
                lbl.configure(image=photo, text="")
            except Exception:
                pass

        for mod in CONFIG["OPTIONAL_MODS"]:
            var = tk.BooleanVar(value=current.get(mod["id"], mod.get("default", True)))
            checkbox_vars[mod["id"]] = var

            # Карточка мода: иконка | название+описание+статус | переключатель
            card = tk.Frame(scroll_frame, bg=colors["bg_field"],
                            highlightbackground=colors["border"], highlightthickness=1)
            card.pack(fill="x", padx=8, pady=4)
            body = tk.Frame(card, bg=colors["bg_field"])
            body.pack(fill="x", padx=10, pady=8)

            # Иконка 40×40 (пока не загрузилась — первая буква названия)
            icon_holder = tk.Frame(body, bg=colors["bg_panel"], width=40, height=40)
            icon_holder.pack(side="left", padx=(0, 10))
            icon_holder.pack_propagate(False)
            icon_lbl = tk.Label(icon_holder, bg=colors["bg_panel"],
                                text=mod["name"][:1].upper(), font=("Segoe UI", 15, "bold"),
                                fg=colors["accent"])
            icon_lbl.pack(fill="both", expand=True)
            icon_labels[mod["id"]] = icon_lbl

            cb = tk.Checkbutton(
                body, text="", variable=var,
                bg=colors["bg_field"], activebackground=colors["bg_field"],
                selectcolor=colors["bg_panel"], highlightthickness=0, bd=0,
                cursor="hand2", command=lambda m=mod: on_toggle(m),
            )
            cb.pack(side="right", padx=(6, 0))
            checkboxes[mod["id"]] = cb

            mid = tk.Frame(body, bg=colors["bg_field"])
            mid.pack(side="left", fill="x", expand=True)
            tk.Label(mid, text=mod["name"], font=("Segoe UI", 10, "bold"),
                     bg=colors["bg_field"], fg=colors["fg"], anchor="w").pack(anchor="w")
            description = mod.get("description")
            if description:
                tk.Label(mid, text=description, font=("Segoe UI", 8),
                         bg=colors["bg_field"], fg=colors["fg_muted"], anchor="w",
                         justify="left", wraplength=300).pack(anchor="w")
            status_label = tk.Label(mid, text="", font=("Segoe UI", 8),
                                    bg=colors["bg_field"], fg=colors["fg_muted"], anchor="w")
            status_label.pack(anchor="w")
            status_labels[mod["id"]] = status_label

            # Иконку тянем с Modrinth в фоне, чтобы окно не подвисало.
            if _PIL_OK and mod.get("slug"):
                def load_icon(m=mod):
                    pil = _load_mod_icon_image(m["slug"], 40)
                    if pil is not None:
                        dialog.after(0, lambda mm=m, p=pil: apply_icon(mm["id"], p))
                threading.Thread(target=load_icon, daemon=True).start()

        dialog.grab_set()

    def _run_in_background(self, work_fn) -> None:
        """Общая обёртка для 'Играть' и 'Починить': блокирует кнопку,
        запускает work_fn в фоновом потоке, показывает понятные сообщения
        об ошибках вместо падения лаунчера.

        Если work_fn вернёт запущенный процесс игры (subprocess.Popen) —
        кнопка "Играть" остаётся заблокированной, пока игрок не закроет
        Minecraft. Раньше кнопка разблокировалась сразу после запуска
        процесса (а не после его завершения), и если игрок в нетерпении
        нажимал "Играть" ещё раз, пока окно игры ещё грузилось — запускалась
        вторая, третья и т.д. копия игры одновременно.

        Также прячет окно лаунчера, пока запущена игра, и возвращает его
        обратно, когда игрок закрывает Minecraft."""
        if self.game_process is not None and self.game_process.poll() is None:
            messagebox.showinfo(
                "Игра уже запущена",
                "Minecraft уже открыт. Закройте игру, прежде чем запускать заново.",
            )
            return

        self.play_button.set_enabled(False)
        self.progress_var.set(0)

        def worker():
            game_started = False
            try:
                process = work_fn()
                if process is not None:
                    game_started = True
                    self.game_process = process
                    self.root.after(0, self._on_game_started)
                    process.wait()  # ждём здесь, в фоновом потоке — интерфейс не подвисает
            except (urllib.error.URLError, socket.timeout, ConnectionError):
                friendly = (
                    "Не получилось скачать файлы. Проверьте подключение к "
                    "интернету и попробуйте ещё раз."
                )
                self.set_status(friendly)
                self.root.after(0, lambda: messagebox.showerror("Нет соединения", friendly))
            except PermissionError as exc:
                friendly = (
                    "Не удалось получить доступ к файлу:\n%s\n\n"
                    "Скорее всего, антивирус (Защитник Windows) заблокировал файл "
                    "во время проверки. Попробуйте:\n\n"
                    "1. Закрыть Minecraft/Java, если они ещё запущены, и нажать "
                    "\"Играть\" ещё раз.\n"
                    "2. Добавить папку .%s_launcher в исключения антивируса.\n"
                    "3. Запустить лаунчер от имени администратора.\n"
                    "4. Перезагрузить компьютер и попробовать снова."
                    % (exc, CONFIG["PACK_NAME"].lower())
                )
                self.set_status("Файл заблокирован (см. окно с ошибкой) — см. подсказку выше.")
                self.root.after(0, lambda: messagebox.showerror("Доступ запрещён", friendly))
            except Exception as exc:  # noqa: BLE001
                self.set_status("Ошибка: %s" % exc)
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        "Ошибка запуска",
                        "Что-то пошло не так:\n\n%s\n\n"
                        "Если ошибка повторяется — покажите это сообщение автору сборки." % exc,
                    ),
                )
            finally:
                self.game_process = None
                self.root.after(0, lambda: self._on_game_ended(game_started))

        threading.Thread(target=worker, daemon=True).start()

    def _on_game_started(self) -> None:
        self.set_status("Игра запущена! Лаунчер откроется снова, когда вы закроете Minecraft.")
        # Сворачиваем в панель задач, а НЕ прячем полностью (withdraw):
        # раньше окно исчезало отовсюду и вернуть его было нечем — со стороны
        # это и выглядело как "процесс есть, окна нет".
        self._hide_during_game()
        # Окно игры в этот момент перехватывает фокус, и Windows может
        # проигнорировать наше сворачивание — поэтому повторяем ещё пару раз,
        # пока игра точно запущена.
        self.root.after(700, self._hide_during_game)
        self.root.after(2500, self._hide_during_game)

    def _hide_during_game(self) -> None:
        """Полностью убирает окно с экрана на время игры. Раньше это было
        опасно (вернуть окно было нечем), но теперь оно возвращается само при
        закрытии игры и по клику на ярлык — поэтому прячем целиком, как и
        ожидается. Скрываем только пока игра реально работает, иначе повторный
        вызов спрятал бы уже вернувшийся лаунчер."""
        process = self.game_process
        if process is None or process.poll() is not None:
            return
        try:
            self.root.withdraw()
        except tk.TclError:
            pass

    def _on_game_ended(self, game_started: bool) -> None:
        self.play_button.set_enabled(True)
        self.show_window()
        if game_started:
            self.set_status("Игра закрыта. Готово к новому запуску.")

    def on_play(self) -> None:
        username = self.nick_var.get().strip()
        if not username or not username.isalnum():
            messagebox.showerror(
                "Некорректный ник",
                "Введите ник латинскими буквами и цифрами (без пробелов и спецсимволов).",
            )
            return

        memory_mb = int(self.memory_var.get())
        low_end_enabled = self.low_end_var.get()
        update_settings(username=username, memory_mb=memory_mb, low_end_mode=low_end_enabled)

        self._run_in_background(
            lambda: launch_game(username, memory_mb, low_end_enabled, self.set_status, self.set_progress)
        )

    def on_play_test(self) -> None:
        """Как 'Играть', но сразу заходит на локальный сервер localhost —
        для проверки сборки на своём ПК перед заливкой на хостинг."""
        username = self.nick_var.get().strip()
        if not username or not username.isalnum():
            messagebox.showerror(
                "Некорректный ник",
                "Введите ник латинскими буквами и цифрами (без пробелов и спецсимволов).",
            )
            return

        memory_mb = int(self.memory_var.get())
        low_end_enabled = self.low_end_var.get()
        update_settings(username=username, memory_mb=memory_mb, low_end_mode=low_end_enabled)

        self._run_in_background(
            lambda: launch_game(
                username, memory_mb, low_end_enabled,
                self.set_status, self.set_progress,
                server_override="localhost:25565", test_mode=True,
            )
        )

    def on_show_mod_list(self) -> None:
        colors = THEMES[self.theme_name]
        categories = CONFIG.get("MOD_SHOWCASE", {})

        dialog = tk.Toplevel(self.root)
        dialog.title("Моды сборки")
        dialog.configure(bg=colors["bg_panel"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.geometry("420x520")
        set_titlebar_dark(dialog, self.theme_name == "dark")

        outer = tk.Frame(dialog, bg=colors["bg_panel"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        header = tk.Frame(outer, bg=colors["bg_panel"])
        header.pack(fill="x")
        if self.icons.get("gear"):
            tk.Label(header, image=self.icons["gear"], bg=colors["bg_panel"]).pack(side="left", padx=(0, 8))
        tk.Label(
            header, text="%s — из чего сделана сборка" % CONFIG["PACK_NAME"],
            font=("Segoe UI", 13, "bold"), bg=colors["bg_panel"], fg=colors["fg"],
            wraplength=340, justify="left",
        ).pack(side="left", anchor="w")

        tk.Frame(outer, bg=colors["accent_dim"], height=1).pack(fill="x", pady=(12, 12))

        list_container = tk.Frame(outer, bg=colors["bg_panel"], highlightbackground=colors["border"],
                                   highlightthickness=1)
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, bg=colors["bg_panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=colors["bg_panel"])

        scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)
        dialog.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        if not categories:
            tk.Label(
                scroll_frame, text="Список пока пуст.", font=("Segoe UI", 9),
                bg=colors["bg_panel"], fg=colors["fg_muted"],
            ).pack(padx=10, pady=10, anchor="w")

        category_names = list(categories.keys())
        for index, category in enumerate(category_names):
            mods = categories[category]
            block = tk.Frame(scroll_frame, bg=colors["bg_panel"])
            block.pack(fill="x", padx=12, pady=(14 if index == 0 else 8, 0))

            tk.Label(
                block, text=category.upper(), font=("Segoe UI", 10, "bold"),
                bg=colors["bg_panel"], fg=colors["accent"],
            ).pack(anchor="w")

            chips = tk.Frame(block, bg=colors["bg_panel"])
            chips.pack(fill="x", pady=(6, 0))

            row = tk.Frame(chips, bg=colors["bg_panel"])
            row.pack(fill="x", anchor="w")
            row_width = 0
            max_row_width = 360
            char_px = 6.5  # грубая оценка ширины символа для переноса строк

            for mod_name in mods:
                chip_width = len(mod_name) * char_px + 20
                if row_width > 0 and row_width + chip_width > max_row_width:
                    row = tk.Frame(chips, bg=colors["bg_panel"])
                    row.pack(fill="x", anchor="w", pady=(6, 0))
                    row_width = 0
                chip = tk.Label(
                    row, text=mod_name, font=("Segoe UI", 9),
                    bg=colors["bg_field"], fg=colors["fg"], padx=8, pady=4,
                )
                chip.pack(side="left", padx=(0, 6))
                row_width += chip_width

            if index < len(category_names) - 1:
                tk.Frame(scroll_frame, bg=colors["border"], height=1).pack(
                    fill="x", padx=12, pady=(14, 0)
                )

        dialog.grab_set()

    def on_show_changelog(self) -> None:
        colors = THEMES[self.theme_name]
        entries = CONFIG.get("LAUNCHER_CHANGELOG", [])

        dialog = tk.Toplevel(self.root)
        dialog.title("Что нового")
        dialog.configure(bg=colors["bg_panel"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.geometry("420x480")
        set_titlebar_dark(dialog, self.theme_name == "dark")

        outer = tk.Frame(dialog, bg=colors["bg_panel"])
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        tk.Label(
            outer, text="История изменений", font=("Segoe UI", 14, "bold"),
            bg=colors["bg_panel"], fg=colors["fg"],
        ).pack(anchor="w")
        tk.Label(
            outer, text="%s launcher" % CONFIG["PACK_NAME"], font=("Segoe UI", 9),
            bg=colors["bg_panel"], fg=colors["fg_muted"],
        ).pack(anchor="w", pady=(2, 12))

        list_container = tk.Frame(outer, bg=colors["bg_panel"], highlightbackground=colors["border"],
                                   highlightthickness=1)
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, bg=colors["bg_panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=colors["bg_panel"])

        scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)
        dialog.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        if not entries:
            tk.Label(
                scroll_frame, text="Пока пусто.", font=("Segoe UI", 9),
                bg=colors["bg_panel"], fg=colors["fg_muted"],
            ).pack(padx=10, pady=10, anchor="w")

        for index, entry in enumerate(entries):
            block = tk.Frame(scroll_frame, bg=colors["bg_panel"])
            block.pack(fill="x", padx=12, pady=(12 if index == 0 else 6, 0))

            header = tk.Frame(block, bg=colors["bg_panel"])
            header.pack(fill="x")
            tk.Label(
                header, text="v%s" % entry.get("version", "?"), font=("Segoe UI", 10, "bold"),
                bg=colors["bg_panel"], fg=colors["accent"],
            ).pack(side="left")
            if entry.get("date"):
                tk.Label(
                    header, text=entry["date"], font=("Segoe UI", 8),
                    bg=colors["bg_panel"], fg=colors["fg_muted"],
                ).pack(side="right")

            for change in entry.get("changes", []):
                tk.Label(
                    block, text="•  %s" % change, font=("Segoe UI", 9),
                    bg=colors["bg_panel"], fg=colors["fg"], justify="left", anchor="w",
                    wraplength=340,
                ).pack(fill="x", pady=(4, 0), anchor="w")

            if index < len(entries) - 1:
                tk.Frame(scroll_frame, bg=colors["border"], height=1).pack(
                    fill="x", padx=12, pady=(12, 0)
                )

        dialog.grab_set()

    def on_repair(self) -> None:
        confirmed = messagebox.askyesno(
            "Переустановить сборку?",
            "Это удалит и заново скачает Minecraft, NeoForge, моды и конфиги — "
            "используйте, если что-то сломалось и обычный запуск не помогает.\n\n"
            "Ваши миры (сохранения) и скриншоты не пострадают.\n\n"
            "Продолжить?",
        )
        if not confirmed:
            return

        username = self.nick_var.get().strip()
        if not username or not username.isalnum():
            messagebox.showerror(
                "Некорректный ник",
                "Введите ник латинскими буквами и цифрами перед переустановкой.",
            )
            return

        memory_mb = int(self.memory_var.get())
        low_end_enabled = self.low_end_var.get()
        update_settings(username=username, memory_mb=memory_mb, low_end_mode=low_end_enabled)

        def work():
            repair_installation(self.set_status, self.set_progress)
            return launch_game(username, memory_mb, low_end_enabled, self.set_status, self.set_progress)

        self._run_in_background(work)

    def refresh_server_status(self) -> None:
        pinned = CONFIG.get("PINNED_SERVER") or {}
        address = (pinned.get("ip") or "").strip()
        if address:
            def worker():
                host, port = parse_host_port(address)
                result = ping_server(host, port)
                self.root.after(0, lambda: self._apply_server_status(result))

            threading.Thread(target=worker, daemon=True).start()

        # Проверяем снова через 30 секунд, пока окно лаунчера открыто —
        # чтобы статус не "застревал" устаревшим, если сервер упал/поднялся
        # уже после того как игрок открыл лаунчер.
        self.root.after(30000, self.refresh_server_status)

    def _apply_server_status(self, result: dict) -> None:
        if result.get("online"):
            online = result.get("players_online")
            maxp = result.get("players_max")
            if online is not None and maxp is not None:
                text = "●  Сервер онлайн · %d/%d игроков" % (online, maxp)
            else:
                text = "●  Сервер онлайн"
            color_key = "status_online"
        else:
            text = "●  Сервер сейчас недоступен"
            color_key = "status_offline"

        self.server_status_var.set(text)
        self.server_status_color_key = color_key
        try:
            if self.server_status_label is not None:
                self.server_status_label.configure(fg=THEMES[self.theme_name][color_key])
        except tk.TclError:
            pass  # окно как раз перерисовывается (смена темы) — не страшно

    def _check_launcher_update_async(self) -> None:
        """Проверяет один раз при старте, не вышла ли новая версия
        лаунчера (см. CONFIG["GITHUB_REPO"]). Не критично: если ничего не
        настроено или сети нет — просто ничего не покажется."""
        def worker():
            info = check_for_launcher_update()
            if info:
                self.root.after(0, lambda: self._apply_update_info(info))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_update_info(self, info: dict) -> None:
        self.update_info = info
        self.update_banner_var.set(
            "🔔  Доступна версия %s — нажмите, чтобы обновить" % info.get("version", "?")
        )
        try:
            if self.update_banner is not None and not self.update_banner.winfo_ismapped():
                if self.version_row is not None:
                    self.update_banner.pack(fill="x", pady=(10, 0), before=self.version_row)
                else:
                    self.update_banner.pack(fill="x", pady=(10, 0))
        except tk.TclError:
            pass  # окно как раз перерисовывается (смена темы) — не страшно

    def on_open_update(self) -> None:
        info = self.update_info or {}
        exe_url = info.get("exe_url")
        # Если запущены как обычный .py (разработка) или у релиза нет .exe —
        # ведём себя как раньше: открываем страницу релиза в браузере.
        if not exe_url or not getattr(sys, "frozen", False):
            if info.get("url"):
                webbrowser.open(info["url"])
            return
        if getattr(self, "_updating", False):
            return  # уже качаем — второй клик игнорируем
        self._updating = True

        def worker():
            try:
                cur_exe = Path(sys.executable)
                new_exe = cur_exe.with_name(cur_exe.stem + "_new.exe")

                def prog(pct):
                    self.root.after(0, lambda: self.update_banner_var.set(
                        "⬇  Скачивание обновления %s… %d%%"
                        % (info.get("version", "?"), pct)))

                download_file(exe_url, new_exe, prog)
                # Проверяем, что скачался целый .exe (заголовок PE "MZ" и вменяемый
                # размер), а не обрывок или HTML-страница ошибки. Иначе НЕ трогаем
                # рабочий файл — так не окажемся с битым лаунчером, который не
                # может загрузить python-DLL.
                ok = False
                try:
                    with open(new_exe, "rb") as fh:
                        head = fh.read(2)
                    ok = (new_exe.stat().st_size > 3_000_000 and head == b"MZ")
                except Exception:
                    ok = False
                if not ok:
                    try:
                        new_exe.unlink()
                    except Exception:
                        pass
                    raise RuntimeError("скачанный файл повреждён")
                self.root.after(0, self._apply_downloaded_update, cur_exe, new_exe)
            except Exception:
                self._updating = False
                # при ошибке откатываемся к «открыть сайт»
                if isinstance(self.update_info, dict):
                    self.update_info["exe_url"] = None
                self.root.after(0, lambda: self.update_banner_var.set(
                    "⚠  Не удалось скачать — нажмите, чтобы открыть страницу"))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_downloaded_update(self, cur_exe: Path, new_exe: Path) -> None:
        """Заменяет .exe новым после закрытия лаунчера.

        Скрипт кладём во ВРЕМЕННУЮ папку системы, а не рядом с .exe: лаунчер
        часто лежит на рабочем столе, и файл _update.bat мозолил там глаза.
        Пути передаём скрипту аргументами (%1/%2), а не вписываем в текст —
        тогда кириллица в пути пользователя не ломает .bat.

        Автозапуск новой копии убран намеренно. Трижды повторялось одно и то
        же: onefile-exe распаковывает себя в temp, антивирус мешает свежему
        файлу, и запущенная сразу после замены копия падала с ошибкой
        python-DLL. Сама замена при этом проходила успешно. Обычный запуск с
        ярлыка работает всегда — поэтому просим открыть лаунчер заново."""
        bat = Path(tempfile.gettempdir()) / ("checkpoint_update_%d.bat" % os.getpid())
        script = (
            "@echo off\r\n"
            ":wait\r\n"
            "ping -n 2 127.0.0.1 >nul\r\n"
            'del %1 >nul 2>&1\r\n'
            'if exist %1 goto wait\r\n'
            'move /y %2 %1 >nul\r\n'
            'del "%~f0" >nul 2>&1\r\n'
        )
        try:
            bat.write_text(script, encoding="ascii")
            CREATE_NO_WINDOW = 0x08000000  # скрипт работает без чёрного окна
            subprocess.Popen(["cmd", "/c", str(bat), str(cur_exe), str(new_exe)],
                             creationflags=CREATE_NO_WINDOW, close_fds=True)
            self.update_banner_var.set("✅  Обновление установлено")
            messagebox.showinfo(
                "Обновление установлено",
                "Новая версия установлена.\n\nЛаунчер сейчас закроется — "
                "откройте его снова с ярлыка.")
            self.root.after(200, self.root.destroy)
        except Exception:
            self._updating = False
            self.update_banner_var.set(
                "⚠  Не удалось применить обновление — нажмите, чтобы открыть страницу")
            if isinstance(self.update_info, dict):
                self.update_info["exe_url"] = None


# ================= Единственный экземпляр приложения =================
# Раньше здесь был именованный mutex: он просто НЕ давал запуститься второй
# копии и показывал "проверьте панель задач". Но если окно было скрыто
# (например, свёрнуто на время игры), достать его было уже нечем — со стороны
# это выглядело как "процесс запущен, а окна нет" и "ярлык не работает".
#
# Теперь вместо замка используется локальный порт: занять его может только
# один процесс (это и есть замок), и одновременно это канал связи — вторая
# копия стучится сюда, просит показать окно и молча закрывается.
SINGLE_INSTANCE_PORT = 49517
SINGLE_INSTANCE_TOKEN = b"CHECKPOINT-LAUNCHER"

_single_instance_server = None


def get_single_instance_server():
    """Слушающий сокет первой копии (или None, если занять порт не вышло)."""
    return _single_instance_server


def _ask_running_instance_to_show() -> bool:
    """Просит уже запущенную копию показать своё окно. Возвращает True, если
    на том конце действительно наш лаунчер и он ответил."""
    try:
        with socket.create_connection(
                ("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2) as conn:
            conn.sendall(b"SHOW\n")
            reply = conn.recv(64)
        return reply.strip() == SINGLE_INSTANCE_TOKEN
    except OSError:
        return False


def acquire_single_instance_lock() -> bool:
    """True — мы единственная копия и можем работать дальше.
    False — лаунчер уже запущен, мы попросили его показать окно и уходим."""
    global _single_instance_server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        server.listen(5)
    except OSError:
        server.close()
        # Порт занят. Проверяем, что это правда наш лаунчер, а не чужая
        # программа — иначе мы бы навсегда отказывались запускаться.
        return not _ask_running_instance_to_show()
    _single_instance_server = server
    return True


def main():
    if not acquire_single_instance_lock():
        # Лаунчер уже открыт: мы попросили его показать окно и просто уходим.
        return

    # Игра может стоять не на диске C — подхватываем выбранную папку до того,
    # как что-либо начнёт обращаться к файлам установки.
    set_install_dir(get_saved_install_dir())

    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()
    # Жёстко завершаем процесс: иначе после закрытия окна exe иногда
    # оставался висеть в диспетчере задач. Запущенная игра при этом
    # продолжает работать — это отдельный процесс.
    os._exit(0)


if __name__ == "__main__":
    main()
