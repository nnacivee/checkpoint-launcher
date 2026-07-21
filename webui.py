# -*- coding: utf-8 -*-
"""Новый интерфейс Industrial Horizon на webview (HTML/CSS/JS в ui/index.html).

Вся логика берётся из launcher.py — здесь только мост: методы класса Api
вызываются из JS как window.pywebview.api.<method>(), а обновления статуса и
прогресса лаунчер шлёт обратно в страницу через window.evaluate_js(...).

Старый tkinter-интерфейс (launcher.py, main()) остаётся рабочим и нетронутым —
этот файл запускается отдельно, чтобы обкатать новый вид, ничего не ломая.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

import launcher as L

# pywebview импортируется ВНУТРИ main() (в try), а не здесь: если на ПК нет
# WebView2/pythonnet, импорт упадёт — и мы должны откатиться на старый лаунчер,
# а не уронить exe на старте.


def _res(rel: str) -> str:
    try:
        return str(L.resource_path(rel))
    except Exception:  # noqa: BLE001
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def _q(text) -> str:
    """Безопасная строка/значение для вставки в JS-код evaluate_js."""
    return json.dumps("" if text is None else text, ensure_ascii=False)


def _open_link(url) -> None:
    if not url:
        return
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def _mods_count():
    try:
        md = L.INSTANCE_DIR / "mods"
        n = len(list(md.glob("*.jar")))
        return n or None
    except Exception:  # noqa: BLE001
        return None


class Api:
    def __init__(self):
        self.window = None
        self._launching = False
        self._busy = False  # общий флаг для длительных операций (repair/установка)
        self._pack_icon_cache = {}  # slug -> icon_url с Modrinth

    def _pack_icons(self, slugs):
        """Иконки паков/шейдеров с Modrinth ПАЧКОЙ (один запрос), с кэшем.
        Неизвестные Modrinth slug'и (наш кастомный пак) просто без картинки."""
        need = [s for s in dict.fromkeys(slugs) if s and s not in self._pack_icon_cache]
        if need:
            try:
                data = L._modrinth_api_get(
                    "https://api.modrinth.com/v2/projects?ids="
                    + urllib.parse.quote(json.dumps(need)))
                found = {p.get("slug"): (p.get("icon_url") or "") for p in data}
                for s in need:
                    self._pack_icon_cache[s] = found.get(s, "")
            except Exception:  # noqa: BLE001
                for s in need:
                    self._pack_icon_cache.setdefault(s, "")
        return {s: self._pack_icon_cache.get(s, "") for s in slugs}

    @staticmethod
    def _pack_thumb(path):
        """Родная иконка установленного пака (pack.png в корне архива/папки),
        завёрнутая в data-URI для показа прямо в карточке. Нет иконки — ''."""
        try:
            p = Path(path)
            data = None
            if p.is_file() and zipfile.is_zipfile(p):
                with zipfile.ZipFile(p) as zf:
                    names = zf.namelist()
                    for cand in ("pack.png", "pack.PNG"):
                        if cand in names:
                            data = zf.read(cand)
                            break
            elif p.is_dir():
                f = p / "pack.png"
                if f.exists():
                    data = f.read_bytes()
            if data and len(data) < 400_000:
                return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
        return ""

    # --- отправка данных обратно в страницу ---
    def _js(self, code: str) -> None:
        try:
            if self.window is not None:
                self.window.evaluate_js(code)
        except Exception:  # noqa: BLE001
            pass

    def _toast(self, text, kind="ok") -> None:
        self._js("window.toast && window.toast(%s, %s)" % (_q(str(text)), _q(kind)))

    # ================= ГЛАВНАЯ =================
    def get_boot(self):
        s = L.load_settings()
        loader_key = L.CONFIG.get("MOD_LOADER", "")
        loader = L.LOADER_DISPLAY_NAMES.get(loader_key, (loader_key or "").capitalize())
        try:
            news = L.fetch_server_news()
        except Exception:  # noqa: BLE001
            news = []
        sys_ram = L.get_system_ram_mb()
        cap = 16384
        ram_max = max(4096, min((sys_ram - 1024) if sys_ram else cap, cap))
        rec = L.recommended_memory_mb(sys_ram, ram_max)
        server_ip = (L.CONFIG.get("PINNED_SERVER") or {}).get("ip", "")
        return {
            "server_ip": server_ip,
            "launcher_version": L.CONFIG.get("LAUNCHER_VERSION", ""),
            "modpack_version": str(L.CONFIG.get("MODPACK_VERSION", "")),
            "mc": L.CONFIG.get("MC_VERSION", ""),
            "loader": loader,
            "mods_count": _mods_count(),
            "nick": s.get("username", ""),
            "memory_mb": int(s.get("memory_mb", L.CONFIG.get("MEMORY_MB", 4096))),
            "low_end": bool(s.get("low_end_mode")),
            "no_sodium": bool(s.get("no_sodium")),
            "ram_min": 2048,
            "ram_max": ram_max,
            "ram_rec": rec,
            "sys_ram": sys_ram,
            "install_dir": str(L.INSTANCE_DIR),
            "news": news,
            "status": "Готово к запуску",
        }

    def get_server(self):
        try:
            pinned = L.CONFIG.get("PINNED_SERVER") or {}
            host, port = L.parse_host_port(pinned.get("ip", ""))
            st = L.ping_server(host, port)
            return {
                "online": bool(st.get("online")),
                "players": st.get("players_online"),
                "max": st.get("players_max"),
                "ping": st.get("ping_ms"),
            }
        except Exception:  # noqa: BLE001
            return {"online": False}

    def save_nick(self, nick):
        try:
            L.update_settings(username=(nick or "").strip())
        except Exception:  # noqa: BLE001
            pass

    # ================= ЗАПУСК =================
    def play(self, nick):
        if self._launching or self._busy:
            return
        nick = (nick or "").strip()
        if not nick or not nick.isalnum():
            self._js("window.onLaunchState('error', %s)"
                     % _q("Ник — только латинские буквы и цифры"))
            return
        self._launching = True
        s = L.load_settings()
        mem = int(s.get("memory_mb", L.CONFIG.get("MEMORY_MB", 4096)))
        low = bool(s.get("low_end_mode"))
        L.update_settings(username=nick)

        def status_cb(text):
            self._js("window.onLaunchState('busy', %s)" % _q(str(text)))

        def progress_cb(pct):
            try:
                self._js("window.onProgress(%d, '')" % int(pct))
            except Exception:  # noqa: BLE001
                pass

        def worker():
            proc = None
            try:
                proc = L.launch_game(nick, mem, low, status_cb, progress_cb)
                self._js("window.onLaunchState('busy', %s)" % _q("Игра запущена"))
                if proc is not None:
                    proc.wait()
                rc = getattr(proc, "returncode", 0) or 0
                if rc:
                    self._js("window.onLaunchState('error', %s)" % _q("Игра закрылась с ошибкой"))
                else:
                    self._js("window.onLaunchState('idle', %s)" % _q("Готово к запуску"))
            except Exception as exc:  # noqa: BLE001
                self._js("window.onLaunchState('error', %s)" % _q(str(exc)))
            finally:
                self._launching = False

        threading.Thread(target=worker, daemon=True).start()

    # ================= МОДЫ =================
    def get_mods(self):
        try:
            mods = L.scan_installed_mods(None)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "mods": [], "categories": []}
        cats = sorted({m.get("category", "") for m in mods if m.get("category")})
        # Отдаём только нужные поля (без размера в байтах и хэша не жалко, но
        # лишнее не гоняем).
        slim = [{
            "title": m.get("title", ""),
            "description": m.get("description", ""),
            "icon": m.get("icon", ""),
            "author": m.get("author", ""),
            "version": m.get("version", ""),
            "url": m.get("url", ""),
            "category": m.get("category", ""),
            "file": m.get("file", ""),
        } for m in mods]
        return {"mods": slim, "categories": cats}

    # ================= НАСТРОЙКИ =================
    def set_memory(self, mb):
        try:
            L.update_settings(memory_mb=int(mb))
        except Exception:  # noqa: BLE001
            pass

    def set_low_end(self, flag):
        try:
            L.update_settings(low_end_mode=bool(flag))
        except Exception:  # noqa: BLE001
            pass

    def set_no_sodium(self, flag):
        try:
            L.update_settings(no_sodium=bool(flag))
        except Exception:  # noqa: BLE001
            pass

    def repair(self):
        if self._busy or self._launching:
            self._toast("Дождитесь окончания текущей операции", "err")
            return
        self._busy = True

        def status_cb(text):
            self._js("window.onProgress(-1, %s)" % _q(str(text)))

        def progress_cb(pct):
            try:
                self._js("window.onProgress(%d, '')" % int(pct))
            except Exception:  # noqa: BLE001
                pass

        def worker():
            try:
                L.repair_installation(status_cb, progress_cb)
                self._js("window.onProgress(100, %s)" % _q("Готово — файлы переустановлены"))
                self._toast("Файлы переустановлены. Можно запускать игру.", "ok")
            except Exception as exc:  # noqa: BLE001
                self._toast("Не удалось переустановить: %s" % exc, "err")
            finally:
                self._busy = False
                self._js("window.onProgress(0, '')")

        threading.Thread(target=worker, daemon=True).start()

    def collect_logs(self):
        def worker():
            try:
                desktop = Path.home() / "Desktop"
                if not desktop.exists():
                    desktop = Path.home()
                out = desktop / ("IH_логи_%s.zip" % time.strftime("%d.%m_%H-%M"))
                found = 0
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                    latest = L.INSTANCE_DIR / "logs" / "latest.log"
                    if latest.exists():
                        zf.write(latest, "latest.log")
                        found += 1
                    crash_dir = L.INSTANCE_DIR / "crash-reports"
                    if crash_dir.exists():
                        crashes = sorted(crash_dir.glob("crash-*.txt"),
                                         key=lambda p: p.stat().st_mtime)
                        for p in crashes[-2:]:
                            zf.write(p, "crash-reports/" + p.name)
                            found += 1
                if not found:
                    out.unlink(missing_ok=True)
                    self._toast("Логов пока нет — игра ещё не запускалась.", "err")
                    return
                self._toast("Логи собраны на рабочий стол: %s. Открываю Discord — "
                            "перетащите файл в канал помощи." % out.name, "ok")
                _help = "channels/1485760114307760260/1528068363123953734"
                try:
                    os.startfile("discord://-/" + _help)  # noqa: S606
                except Exception:  # noqa: BLE001
                    _open_link("https://discord.com/" + _help)
            except Exception as exc:  # noqa: BLE001
                self._toast("Не удалось собрать логи: %s" % exc, "err")

        threading.Thread(target=worker, daemon=True).start()

    # ================= ВНЕШНИЙ ВИД =================
    def graphics_preset(self, kind):
        def worker():
            try:
                L.apply_graphics_preset(kind, lambda t: self._toast(t, "ok"))
                self._toast("Пресет графики применён. Меняйте при закрытой игре.", "ok")
            except Exception as exc:  # noqa: BLE001
                self._toast("Не получилось применить пресет: %s" % exc, "err")

        threading.Thread(target=worker, daemon=True).start()

    def get_shaders(self):
        try:
            installed = [{"name": p.get("name", ""), "enabled": bool(p.get("enabled")),
                          "icon": self._pack_thumb(p.get("path"))}
                         for p in L.list_shader_packs()]
        except Exception:  # noqa: BLE001
            installed = []
        cfgs = L.CONFIG.get("RECOMMENDED_SHADER_PACKS", [])
        icons = self._pack_icons([c.get("slug", "") for c in cfgs])
        rec = [{"slug": c.get("slug", ""), "name": c.get("name", ""),
                "weight": c.get("weight", ""), "description": c.get("description", ""),
                "icon": icons.get(c.get("slug", ""), "")}
               for c in cfgs]
        return {"installed": installed, "recommended": rec}

    def toggle_shader(self, name, enabled):
        try:
            for p in L.list_shader_packs():
                if p.get("name") == name:
                    L.set_shader_enabled(p, bool(enabled))
                    return True
        except Exception as exc:  # noqa: BLE001
            self._toast("Не удалось переключить шейдер: %s" % exc, "err")
        return False

    def install_shader(self, slug):
        cfg = next((c for c in L.CONFIG.get("RECOMMENDED_SHADER_PACKS", [])
                    if c.get("slug") == slug), None)
        if not cfg:
            return

        def worker():
            try:
                L.install_recommended_shader_pack(cfg, lambda t: self._toast(t, "ok"))
                for p in L.list_shader_packs():
                    if cfg.get("name", "").split(" —")[0].lower() in p.get("name", "").lower():
                        L.set_shader_enabled(p, True)
                        break
                self._toast("Шейдер «%s» установлен и включён." % cfg.get("name"), "ok")
                self._js("window.refreshShaders && window.refreshShaders()")
            except Exception as exc:  # noqa: BLE001
                self._toast("Не удалось установить шейдер: %s" % exc, "err")

        threading.Thread(target=worker, daemon=True).start()

    def get_resource_packs(self):
        try:
            installed = [{"name": p.get("name", ""), "enabled": bool(p.get("enabled")),
                          "icon": self._pack_thumb(p.get("path"))}
                         for p in L.list_resource_packs()]
        except Exception:  # noqa: BLE001
            installed = []
        cfgs = L.CONFIG.get("RECOMMENDED_RESOURCE_PACKS", [])
        icons = self._pack_icons([c.get("slug", "") for c in cfgs])
        rec = [{"slug": c.get("slug", ""), "name": c.get("name", ""),
                "description": c.get("description", ""),
                "icon": icons.get(c.get("slug", ""), "")}
               for c in cfgs]
        return {"installed": installed, "recommended": rec}

    def toggle_resource(self, name, enabled):
        try:
            for p in L.list_resource_packs():
                if p.get("name") == name:
                    L.set_resource_pack_enabled(p, bool(enabled))
                    return True
        except Exception as exc:  # noqa: BLE001
            self._toast("Не удалось переключить пак: %s" % exc, "err")
        return False

    def install_resource(self, slug):
        cfg = next((c for c in L.CONFIG.get("RECOMMENDED_RESOURCE_PACKS", [])
                    if c.get("slug") == slug), None)
        if not cfg:
            return

        def worker():
            try:
                L.install_recommended_resource_pack(cfg, lambda t: self._toast(t, "ok"))
                self._toast("Ресурс-пак «%s» установлен." % cfg.get("name"), "ok")
                self._js("window.refreshResources && window.refreshResources()")
            except Exception as exc:  # noqa: BLE001
                self._toast("Не удалось установить пак: %s" % exc, "err")

        threading.Thread(target=worker, daemon=True).start()

    def open_skins_folder(self):
        try:
            L.open_folder(L.INSTANCE_DIR)
        except Exception:  # noqa: BLE001
            pass

    # ================= СЛУЖЕБНОЕ / ССЫЛКИ =================
    def open_folder(self):
        try:
            L.open_folder(L.INSTANCE_DIR)
        except Exception:  # noqa: BLE001
            try:
                os.startfile(str(L.INSTANCE_DIR))  # noqa: S606
            except Exception:  # noqa: BLE001
                pass

    def open_telegram(self):
        _open_link(L.CONFIG.get("TELEGRAM_URL"))

    def open_discord(self):
        _open_link(L.CONFIG.get("DISCORD_URL"))

    def open_map(self):
        _open_link(L.CONFIG.get("MAP_URL"))

    def open_news(self):
        _open_link(L.CONFIG.get("NEWS_URL"))

    def open_url(self, url):
        _open_link(url)

    # ================= АВТООБНОВЛЕНИЕ =================
    # Логика та же, что в старом лаунчере: качаем свежий установщик С ЗЕРКАЛА
    # (в РФ GitHub заблокирован), ставим его тихо, лаунчер перезапускается сам.
    # На GitHub НЕ уходим никогда.
    def check_update(self):
        try:
            if not getattr(sys, "frozen", False):
                return None  # в режиме разработки (.py) самообновления нет
            return L.check_for_launcher_update()  # {version, exe_url, url} | None
        except Exception:  # noqa: BLE001
            return None

    def apply_update(self):
        if getattr(self, "_updating", False):
            return
        self._updating = True
        try:
            info = L.check_for_launcher_update() or {}
        except Exception:  # noqa: BLE001
            info = {}
        exe_url = info.get("exe_url") or L.CONFIG.get("LAUNCHER_EXE_MIRROR_URL")
        if not (getattr(sys, "frozen", False) and exe_url):
            self._updating = False
            return

        def worker():
            try:
                new_exe = Path(tempfile.gettempdir()) / "CheckpointSetup_new.exe"

                def prog(pct):
                    self._js("window.updBanner && window.updBanner('dl', %d)" % int(pct))

                L.download_file(exe_url, new_exe, prog)
                with open(new_exe, "rb") as fh:
                    head = fh.read(2)
                if not (new_exe.stat().st_size > 3_000_000 and head == b"MZ"):
                    new_exe.unlink(missing_ok=True)
                    raise RuntimeError("скачанный файл повреждён")
                bat = Path(tempfile.gettempdir()) / ("ih_update_%d.bat" % os.getpid())
                script = (
                    "@echo off\r\n"
                    "ping -n 3 127.0.0.1 >nul\r\n"
                    "%1 /VERYSILENT /NORESTART /SUPPRESSMSGBOXES /NOCANCEL\r\n"
                    "ping -n 12 127.0.0.1 >nul\r\n"
                    "del %1 >nul 2>&1\r\n"
                    'del "%~f0" >nul 2>&1\r\n'
                )
                bat.write_text(script, encoding="ascii")
                subprocess.Popen(["cmd", "/c", str(bat), str(new_exe)],
                                 creationflags=0x08000000, close_fds=True)
                self._js("window.updBanner && window.updBanner('done', 100)")
                time.sleep(0.4)
                self.close()
            except Exception:  # noqa: BLE001
                self._updating = False
                self._js("window.updBanner && window.updBanner('err', 0)")

        threading.Thread(target=worker, daemon=True).start()

    def play_menu(self):
        pass

    # --- управление окном (frameless) ---
    def minimize(self):
        try:
            self.window.minimize()
        except Exception:  # noqa: BLE001
            pass

    def maximize(self):
        try:
            self.window.toggle_fullscreen()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass


def main():
    # Игра может стоять не на диске C — как и в старом лаунчере, подхватываем
    # выбранную папку установки до обращения к файлам.
    try:
        L.set_install_dir(L.get_saved_install_dir())
    except Exception:  # noqa: BLE001
        pass

    try:
        import webview  # pywebview — может отсутствовать/не завестись без WebView2
        api = Api()
        # Обычное системное окно (не frameless): его можно свободно растягивать
        # за края, есть родные «свернуть/развернуть/закрыть», и ничего не
        # перехватывает клики. min_size не даёт сжать до нечитаемого.
        window = webview.create_window(
            L.CONFIG.get("WINDOW_TITLE") or "Industrial Horizon",
            url=_res("ui/index.html"),
            js_api=api,
            width=1160, height=740, min_size=(900, 600),
            resizable=True, background_color="#0a0e14",
        )
        api.window = window
        webview.start()
    except Exception:  # noqa: BLE001
        # Новый интерфейс не поднялся (чаще всего — на ПК нет среды WebView2).
        # Падать нельзя: откатываемся на проверенный старый tkinter-лаунчер,
        # чтобы у человека в любом случае работала кнопка «Играть».
        try:
            import traceback
            traceback.print_exc()
        except Exception:  # noqa: BLE001
            pass
        L.main()


if __name__ == "__main__":
    main()
