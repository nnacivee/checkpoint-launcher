"""Пост в Telegram-канал о новой версии лаунчера.

Запускается из GitHub Actions сразу после публикации релиза. Текст берётся из
LAUNCHER_CHANGELOG в launcher.py — то есть пишется один раз и попадает и в
окно «что нового», и в канал. Дублировать руками ничего не нужно.

Токен приходит только через переменные окружения (GitHub Secrets) и нигде не
печатается: в логах Actions видно текст поста, но не ключи.

Использование:
    python tg_notify.py <версия> [ссылка_на_релиз]
"""

import ast
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org/bot%s/sendMessage"
API_PHOTO = "https://api.telegram.org/bot%s/sendPhoto"

# Лимит Telegram на подпись к фото. У обычного сообщения — 4096, поэтому
# длинный список изменений уходит вторым сообщением вслед за карточкой.
CAPTION_LIMIT = 1000


def read_changelog(launcher_path: Path) -> list:
    """Достаёт LAUNCHER_CHANGELOG из launcher.py, не исполняя его.

    Через ast, а не import: импорт потянул бы tkinter, создал папки в AppData
    и вообще запустил бы кучу кода на сервере сборки. Нам нужен один список.
    """
    tree = ast.parse(launcher_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "LAUNCHER_CHANGELOG":
                return ast.literal_eval(value)
    raise SystemExit("LAUNCHER_CHANGELOG не найден в launcher.py")


def build_message(version: str, changelog: list, release_url: str = "") -> str:
    entry = next((e for e in changelog if str(e.get("version")) == version), None)

    title = "🚀 <b>Industrial Horizon — лаунчер %s</b>" % html.escape(version)
    lines = [title]

    if entry:
        if entry.get("date"):
            lines.append("<i>%s</i>" % html.escape(str(entry["date"])))
        lines.append("")
        for change in entry.get("changes", []):
            lines.append("• " + html.escape(str(change)))
    else:
        # Версии нет в списке изменений — не повод молчать.
        lines.append("")
        lines.append("• Обновление лаунчера.")

    if release_url:
        lines.append("")
        lines.append('<a href="%s">Скачать</a>' % html.escape(release_url, quote=True))

    text = "\n".join(lines)

    # У Telegram лимит 4096 символов на сообщение. Режем по строкам, чтобы не
    # разорвать тег посередине — иначе Telegram отвергнет весь пост.
    if len(text) > 4000:
        cut = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > 3900:
                cut.append("…")
                break
            cut.append(line)
            size += len(line) + 1
        text = "\n".join(cut)
    return text


def render_card(version: str, date: str, changes=None) -> Path | None:
    """Карточка для поста: арт сборки, логотип, версия и СПИСОК ИЗМЕНЕНИЙ.

    Изменения печатаются прямо на картинке — пост показывает, что нового,
    ещё до чтения подписи. Берётся первое предложение каждого пункта,
    максимум пять пунктов, остальное — строкой «и ещё N изменений».

    Рисуется теми же файлами, что и главное окно лаунчера (background.png,
    logo.png, fonts/Lato-*.ttf) — пост выглядит продолжением лаунчера, а не
    чужой картинкой. Любая проблема (нет Pillow, нет арта, нет шрифтов) —
    возвращаем None и бот шлёт обычный текст: пост важнее красоты.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        return None
    here = Path(__file__).parent
    try:
        def font(size, bold=False):
            name = "Lato-Bold.ttf" if bold else "Lato-Regular.ttf"
            return ImageFont.truetype(str(here / "fonts" / name), size)

        # --- готовим строки изменений заранее: от них зависит высота ---
        f_item = font(28)
        W = 1200
        MAXW = W - 170          # поле текста: отступ слева 110 + справа 60
        MAX_ITEMS = 5

        def first_sentence(s: str) -> str:
            s = " ".join(str(s).split())
            for stop in (". ", "! ", "? "):
                if stop in s:
                    return s.split(stop, 1)[0] + stop.strip()
            return s

        def wrap(s: str, fnt, maxw: int, max_lines: int = 2):
            words, lines, cur = s.split(), [], ""
            for w_ in words:
                probe = (cur + " " + w_).strip()
                if fnt.getlength(probe) <= maxw:
                    cur = probe
                    continue
                lines.append(cur)
                cur = w_
                if len(lines) == max_lines:
                    lines[-1] = lines[-1].rstrip(",.;") + "…"
                    return lines
            if cur:
                lines.append(cur)
            return lines[:max_lines]

        changes = [c for c in (changes or []) if str(c).strip()]
        items = []
        for c in changes[:MAX_ITEMS]:
            items.append(wrap(first_sentence(c), f_item, MAXW))
        rest = len(changes) - MAX_ITEMS
        if rest > 0:
            word = "изменение" if rest == 1 else (
                "изменения" if rest in (2, 3, 4) else "изменений")
            items.append(["… и ещё %d %s" % (rest, word)])

        LINE_H, ITEM_GAP = 38, 14
        list_h = sum(len(ls) * LINE_H + ITEM_GAP for ls in items)
        HEAD_H = 300            # арт с логотипом сверху
        TITLE_H = 130           # «Обновление 1.xx.0» + дата
        H = max(630, HEAD_H + TITLE_H + list_h + 60)

        art = Image.open(here / "background.png").convert("RGB")
        k = max(W / art.width, H / art.height)
        art = art.resize((round(art.width * k), round(art.height * k)),
                         Image.LANCZOS)
        x = (art.width - W) // 2
        img = art.crop((x, 0, x + W, H)).convert("RGBA")

        # Тёмная панель под текст: от логотипа и до низа, с мягким верхом.
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(shade)
        top = HEAD_H - 80
        for i in range(120):
            d.line([(0, top + i), (W, top + i)],
                   fill=(8, 11, 16, int(242 * i / 120)))
        d.rectangle([0, top + 120, W, H], fill=(8, 11, 16, 242))
        img.alpha_composite(shade)

        logo = Image.open(here / "logo.png").convert("RGBA")
        k = 230 / max(logo.size)
        logo = logo.resize((round(logo.width * k), round(logo.height * k)),
                           Image.LANCZOS)
        img.alpha_composite(logo, ((W - logo.width) // 2, 26))

        d = ImageDraw.Draw(img)
        y = HEAD_H + 26
        d.text((W // 2, y), "Обновление  %s" % version,
               font=font(56, bold=True), fill=(63, 169, 245), anchor="mm")
        if date:
            d.text((W // 2, y + 52), date, font=font(24),
                   fill=(150, 162, 178), anchor="mm")

        y = HEAD_H + TITLE_H
        for lines in items:
            d.text((70, y + LINE_H // 2), "•", font=font(28, bold=True),
                   fill=(63, 169, 245), anchor="lm")
            for line in lines:
                d.text((110, y + LINE_H // 2), line, font=f_item,
                       fill=(232, 238, 246), anchor="lm")
                y += LINE_H
            y += ITEM_GAP

        out = here / "tg_card.png"
        img.convert("RGB").save(out, "PNG")
        return out
    except Exception as exc:  # noqa: BLE001
        print("Карточка не собралась (%s) — пост уйдёт текстом." % exc)
        return None


def send_photo(token: str, chat_id: str, photo: Path, caption: str) -> dict:
    """sendPhoto — multipart/form-data руками: requests в сборке нет,
    а тянуть зависимость ради одного запроса не хочется."""
    boundary = "----tg%s" % uuid.uuid4().hex
    parts = []
    for name, value in (("chat_id", chat_id), ("caption", caption),
                        ("parse_mode", "HTML")):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\""
                      "\r\n\r\n%s\r\n" % (boundary, name, value)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"photo\"; "
                  "filename=\"card.png\"\r\nContent-Type: image/png\r\n\r\n"
                  % boundary).encode("utf-8"))
    parts.append(photo.read_bytes())
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)

    request = urllib.request.Request(
        API_PHOTO % urllib.parse.quote(token, safe=":"), data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        raise SystemExit("Telegram ответил %s: %s" % (exc.code, body_text))


def send(token: str, chat_id: str, text: str) -> dict:
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    # quote() настоящий токен (цифры, двоеточие, буквы, "-", "_") не меняет, но
    # если в секрет затесался лишний символ — Telegram ответит внятной ошибкой
    # вместо UnicodeEncodeError где-то в недрах http.client.
    request = urllib.request.Request(API % urllib.parse.quote(token, safe=":"), data=data)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Тело ошибки Telegram объясняет причину ("chat not found", "bot is not
        # a member of the channel chat" и т.п.) — без него отладка вслепую.
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit("Telegram ответил %s: %s" % (exc.code, body))


def force_utf8_output() -> None:
    """Заставляет print() выдавать UTF-8, чем бы ни был stdout.

    Windows на серверах GitHub отдаёт вывод шага в cp1252. Первый же print()
    с кириллицей падал с UnicodeEncodeError — и ронял всю сборку, хотя
    Telegram тут вообще ни при чём. На Linux этого не видно: там UTF-8 по
    умолчанию, поэтому тесты молчали.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> None:
    force_utf8_output()

    if len(sys.argv) < 2:
        raise SystemExit("нужна версия: python tg_notify.py 1.20.0 [ссылка]")
    version = sys.argv[1]
    release_url = sys.argv[2] if len(sys.argv) > 2 else ""

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        # Секреты не заведены (или это чужой форк) — это не повод валить сборку.
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — пост пропущен.")
        return

    # Частая ошибка: боты-«узнай id» показывают число без знака, а Telegram
    # ждёт id группы/канала с минусом. Без этой проверки ответ был бы просто
    # "chat not found" — верно, но непонятно, что чинить.
    if chat_id.isdigit():
        raise SystemExit(
            "TELEGRAM_CHAT_ID = %s — это id без знака, Telegram такой не примет.\n"
            "У группы и канала id отрицательный:\n"
            "  обычная группа        -> -%s\n"
            "  супергруппа или канал -> -100%s\n"
            "Точное значение видно в https://api.telegram.org/bot<токен>/getUpdates "
            "в поле \"chat\":{\"id\":...}" % (chat_id, chat_id, chat_id)
        )

    changelog = read_changelog(Path(__file__).with_name("launcher.py"))
    text = build_message(version, changelog, release_url)
    print("Текст поста:\n" + text)

    entry = next((e for e in changelog if str(e.get("version")) == version), {})
    card = render_card(version, str(entry.get("date", "")),
                       entry.get("changes"))

    if card is None:
        # Красиво не вышло — шлём как раньше, текстом. Пост важнее карточки.
        result = send(token, chat_id, text)
    elif len(text) <= CAPTION_LIMIT:
        result = send_photo(token, chat_id, card, text)
    else:
        # Подпись к фото — максимум 1024 символа, длинный список изменений
        # туда не влезает. Карточка уходит с короткой шапкой, полный текст —
        # следом обычным сообщением: ничего не теряется.
        short = "🚀 <b>Industrial Horizon — лаунчер %s</b>" % html.escape(version)
        if release_url:
            short += '\n<a href="%s">Скачать</a>' % html.escape(release_url, quote=True)
        result = send_photo(token, chat_id, card, short)
        if result.get("ok"):
            result = send(token, chat_id, text)

    if not result.get("ok"):
        raise SystemExit("Telegram не принял сообщение: %s" % result)
    print("Отправлено, message_id=%s" % result.get("result", {}).get("message_id"))


if __name__ == "__main__":
    main()
