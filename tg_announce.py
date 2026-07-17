# -*- coding: utf-8 -*-
"""Разовый пост в Telegram-канал: картинка + текст.

Зачем отдельно от tg_notify.py: тот привязан к выходу версии лаунчера и
собирает текст из LAUNCHER_CHANGELOG. Иногда же нужно просто рассказать
о чём-то (новое меню, событие на сервере) с готовой картинкой — вот для
этого и есть announce.

Запуск — вручную из GitHub: Actions -> "Post to Telegram" -> Run workflow,
там же вводится текст. Токен и chat_id берутся из секретов репозитория,
поэтому в файле их нет и быть не должно.

Локально:
    set TELEGRAM_BOT_TOKEN=...
    set TELEGRAM_CHAT_ID=...
    python tg_announce.py "<b>Заголовок</b>\nтекст" docs/radial_menu.png
"""

import os
import sys
from pathlib import Path

# Переиспользуем готовые куски: send_photo умеет multipart без requests,
# send — обычный текст, force_utf8_output чинит вывод на Windows-раннерах.
from tg_notify import send, send_photo, force_utf8_output, CAPTION_LIMIT


def main() -> None:
    force_utf8_output()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        # Не ошибка: без секретов шаг просто ничего не делает, как в tg_notify.
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — пропускаю.")
        return

    if len(sys.argv) < 2:
        raise SystemExit("Нужен текст поста: python tg_announce.py \"текст\" [картинка]")

    text = sys.argv[1].replace("\\n", "\n")
    photo = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if photo and not photo.exists():
        raise SystemExit("Картинка не найдена: %s" % photo)

    if photo:
        # Телеграм режет подпись под фото на 1024 символах (у нас лимит с
        # запасом). Если текст длиннее — шлём фото с коротким заголовком,
        # а полный текст отдельным сообщением: так ничего не потеряется.
        if len(text) <= CAPTION_LIMIT:
            send_photo(token, chat_id, photo, text)
            print("Отправлено: фото с подписью.")
        else:
            head = text.split("\n", 1)[0]
            send_photo(token, chat_id, photo, head)
            send(token, chat_id, text)
            print("Отправлено: фото + отдельный текст (подпись была длинной).")
    else:
        send(token, chat_id, text)
        print("Отправлено: текст.")


if __name__ == "__main__":
    main()
