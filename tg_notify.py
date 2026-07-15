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
from pathlib import Path

API = "https://api.telegram.org/bot%s/sendMessage"


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

    result = send(token, chat_id, text)
    if not result.get("ok"):
        raise SystemExit("Telegram не принял сообщение: %s" % result)
    print("Отправлено, message_id=%s" % result.get("result", {}).get("message_id"))


if __name__ == "__main__":
    main()
