# -*- coding: utf-8 -*-
"""Новый интерфейс Industrial Horizon на webview (HTML/CSS/JS в ui/index.html).

Вся логика берётся из launcher.py — здесь только мост: методы класса Api
вызываются из JS как window.pywebview.api.<method>(), а обновления статуса и
прогресса лаунчер шлёт обратно в страницу через window.evaluate_js(...).

Старый tkinter-интерфейс (launcher.py, main()) остаётся рабочим и нетронутым —
этот файл запускается отдельно, чтобы обкатать новый вид, ничего не ломая.
"""
import base64
import io
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


def _as_bool(value) -> bool:
    """Coerce values coming from JavaScript without treating "false" as true."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clean_nick(value) -> str:
    return "".join(
        char for char in str(value or "").strip()
        if char.isascii() and (char.isalnum() or char == "_")
    )[:16]


def _release_single_instance_lock() -> None:
    """Close the launcher-owned localhost socket after the web window exits."""
    try:
        server = L.get_single_instance_server()
        if server is not None:
            server.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        L._single_instance_server = None
    except Exception:  # noqa: BLE001
        pass


CENTER_PREFERRED_SIZE = (1320, 820)
CENTER_MIN_SIZE = (960, 600)
# Per-edge breathing room inside the monitor working area.  The working area
# already excludes the Windows taskbar; these margins keep native borders away
# from screen edges on compact displays.
CENTER_SCREEN_MARGIN = (32, 32)


def _webview_screens(webview_module):
    try:
        screens = getattr(webview_module, "screens", ())
        # Older pywebview releases exposed screens as a function, while 6.x
        # exposes a module property.
        if callable(screens):
            screens = screens()
        return tuple(screens or ())
    except Exception:  # noqa: BLE001
        return ()


def _primary_webview_screen(webview_module):
    screens = _webview_screens(webview_module)
    return screens[0] if screens else None


def _rect_value(rect, name):
    value = getattr(rect, name)
    return int(value() if callable(value) else value)


def _screen_work_area(screen):
    """Return pywebview screen working bounds in logical pixels."""
    fallback = (
        int(screen.x), int(screen.y), int(screen.width), int(screen.height)
    )
    frame = getattr(screen, "frame", None)
    if frame is None:
        return fallback

    candidates = []
    for method_name in ("availableGeometry", "geometry"):
        method = getattr(frame, method_name, None)
        if callable(method):
            try:
                candidates.append(method())
            except Exception:  # noqa: BLE001
                pass
    candidates.append(frame)

    for rect in candidates:
        for names in (
            ("X", "Y", "Width", "Height"),
            ("x", "y", "width", "height"),
        ):
            try:
                result = tuple(_rect_value(rect, name) for name in names)
                if result[2] > 0 and result[3] > 0:
                    return result
            except Exception:  # noqa: BLE001
                pass
        try:  # Cocoa NSRect
            result = (
                int(frame.origin.x),
                int(frame.origin.y),
                int(frame.size.width),
                int(frame.size.height),
            )
            if result[2] > 0 and result[3] > 0:
                return result
        except Exception:  # noqa: BLE001
            pass
    return fallback


def _center_control_window_size(screen):
    """Scale the preferred window to the real monitor working area."""
    preferred_width, preferred_height = CENTER_PREFERRED_SIZE
    if screen is None:
        return CENTER_PREFERRED_SIZE
    _x, _y, work_width, work_height = _screen_work_area(screen)
    margin_x, margin_y = CENTER_SCREEN_MARGIN
    safe_width = max(1, work_width - 2 * margin_x)
    safe_height = max(1, work_height - 2 * margin_y)
    scale = min(
        1.0,
        safe_width / preferred_width,
        safe_height / preferred_height,
    )
    min_width, min_height = CENTER_MIN_SIZE
    return (
        max(min_width, int(preferred_width * scale)),
        max(min_height, int(preferred_height * scale)),
    )


def _normalize_native_window(
        window, webview_module, width: int, height: int, screen=None) -> None:
    """Force one normal, centered startup geometry after the native form is shown.

    pywebview 6.2.1 centers a WinForms window when x/y are unset, but only sets
    WindowState when ``maximized`` or ``minimized`` is true.  A process started
    by a shortcut configured as maximized can therefore still inherit that
    native state.  Restoring once after ``shown`` makes startup deterministic;
    the window remains resizable and may be maximized normally afterwards.
    """
    try:
        window.restore()
    except Exception:  # noqa: BLE001
        pass
    try:
        window.resize(int(width), int(height))
    except Exception:  # noqa: BLE001
        pass

    try:
        screen = screen or _primary_webview_screen(webview_module)
        if screen is None:
            return
        work_x, work_y, work_width, work_height = _screen_work_area(screen)
        x = work_x + max(0, (work_width - int(width)) // 2)
        y = work_y + max(0, (work_height - int(height)) // 2)
        window.move(x, y)
    except Exception:  # noqa: BLE001
        # WinForms already uses CenterScreen for x/y=None, so failure to query
        # monitor metadata must not prevent the launcher from opening.
        pass


def _start_web_runtime(
        api, window, webview_module, width: int, height: int, screen=None) -> None:
    """Run one-time window setup, then keep serving second-instance requests."""
    try:
        shown = getattr(getattr(window, "events", None), "shown", None)
        if shown is None or shown.wait(10):
            _normalize_native_window(
                window, webview_module, width, height, screen=screen
            )
    finally:
        # This listener used to be the direct webview.start callback.  Keeping
        # it in finally preserves single-instance behaviour even if native
        # geometry APIs are unavailable on a particular backend.
        api._serve_single_instance()


UI_SETTING_DEFAULTS = {
    "auto_updates": True,
    "minimize_on_launch": True,
    "show_news": True,
    "language": "ru",
    "graphics_profile": "balanced",
    "gpu_mode": "auto",
}


class Api:
    def __init__(self):
        # pywebview публикует public-атрибуты js_api в JavaScript. Если хранить
        # Window в self.window, его инспектор уходит в рекурсивный обход
        # native.AccessibilityObject и окно зависает ещё до первого кадра.
        # Приватное имя не экспортируется и разрывает этот цикл.
        self._window = None
        self._launching = False
        self._busy = False  # общий флаг для длительных операций (repair/установка)
        self._hidden_for_game = False
        self._catalog_jobs = set()
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
            if self._window is not None:
                self._window.evaluate_js(code)
        except Exception:  # noqa: BLE001
            pass

    def _toast(self, text, kind="ok") -> None:
        self._js("window.toast && window.toast(%s, %s)" % (_q(str(text)), _q(kind)))

    def _launch_telemetry(self, state, text="", progress=None, phase=None) -> None:
        """Send structured launch state, falling back for older bundled pages."""
        payload = {"state": str(state), "text": str(text or "")}
        if progress is not None:
            payload["progress"] = max(0, min(100, int(progress)))
        if phase:
            payload["phase"] = str(phase)
        self._js(
            "(function(p){if(typeof window.onLaunchTelemetry==='function'){"
            "window.onLaunchTelemetry(p)}else{"
            "window.onLaunchState&&window.onLaunchState(p.state,p.text);"
            "if(p.progress!==undefined)window.onProgress&&"
            "window.onProgress(p.progress,p.text)}})(%s)" % _q(payload)
        )

    def _repair_state(self, state, text="", progress=None) -> None:
        """Publish an explicit maintenance terminal state with legacy fallback."""
        payload = {"state": str(state), "text": str(text or "")}
        if progress is not None:
            payload["progress"] = max(0, min(100, int(progress)))
        self._js(
            "(function(p){if(typeof window.onRepairState==='function'){"
            "window.onRepairState(p)}else if(p.state==='progress'){"
            "window.onProgress&&window.onProgress(p.progress??-1,p.text)"
            "}else if(p.state==='complete'){"
            "window.onProgress&&window.onProgress(100,p.text);"
            "window.onProgress&&window.onProgress(0,'')"
            "}else if(p.state==='error'){"
            "window.onLaunchState&&window.onLaunchState('error',p.text);"
            "window.onProgress&&window.onProgress(0,'')}}})(%s)" % _q(payload)
        )

    def _catalog_install_state(
            self, kind, slug, state, text="", progress=None) -> None:
        payload = {
            "kind": str(kind),
            "slug": str(slug),
            "state": str(state),
            "text": str(text or ""),
        }
        if progress is not None:
            payload["progress"] = max(0, min(100, int(progress)))
        self._js(
            "window.onCatalogInstallState&&window.onCatalogInstallState(%s)"
            % _q(payload)
        )

    def _serve_single_instance(self) -> None:
        """Bring the existing web window forward when its shortcut is reopened."""
        server = L.get_single_instance_server()
        if server is None:
            return

        def worker():
            while True:
                try:
                    conn, _addr = server.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(2)
                    data = conn.recv(64)
                    conn.sendall(L.SINGLE_INSTANCE_TOKEN + b"\n")
                    if data.strip() == b"SHOW" and self._window is not None:
                        try:
                            self._window.restore()
                            self._window.show()
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        conn.recv(1)
                    except OSError:
                        pass
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _memory_profile():
        """Return detected RAM limits and the safe automatic allocation."""
        sys_ram = L.get_system_ram_mb()
        cap = 16384
        if sys_ram:
            # Always leave Windows and background apps at least 2 GB and roughly
            # a quarter of physical RAM.  On a 4 GB PC Auto therefore selects
            # 2 GB instead of starving the operating system with a 4 GB heap.
            reserve = max(2048, int(sys_ram * 0.25))
            ram_max = max(2048, min(sys_ram - reserve, cap))
        else:
            ram_max = cap
        recommended = L.recommended_memory_mb(sys_ram, ram_max)
        return sys_ram, ram_max, recommended

    def _choose_path(self, folder=False, file_types=None):
        """Open a native pywebview picker and return one selected path."""
        if self._window is None:
            return None
        import webview

        # Prefer the current pywebview 6 API. Accessing OPEN_DIALOG /
        # FOLDER_DIALOG first emits a deprecation warning on every file picker.
        dialog_class = getattr(webview, "FileDialog", None)
        dialog_type = getattr(
            dialog_class, "FOLDER" if folder else "OPEN", None)
        if dialog_type is None:
            legacy_name = "FOLDER_DIALOG" if folder else "OPEN_DIALOG"
            dialog_type = getattr(webview, legacy_name, None)
        if dialog_type is None:
            raise RuntimeError("File dialog is unavailable")

        options = {"directory": str(L.INSTANCE_DIR.parent)}
        if file_types and not folder:
            options["file_types"] = tuple(file_types)
        try:
            result = self._window.create_file_dialog(dialog_type, **options)
        except TypeError:
            # Older pywebview builds do not accept file_types/directory on all
            # backends.  The native dialog is still safe without those hints.
            result = self._window.create_file_dialog(dialog_type)
        if isinstance(result, (tuple, list)):
            result = result[0] if result else None
        return str(result) if result else None

    @staticmethod
    def _ui_settings():
        saved = L.load_settings()
        result = dict(UI_SETTING_DEFAULTS)
        for key in result:
            if key in saved:
                result[key] = saved[key]
        result["auto_updates"] = _as_bool(result["auto_updates"])
        result["minimize_on_launch"] = _as_bool(result["minimize_on_launch"])
        result["show_news"] = _as_bool(result["show_news"])
        if result["language"] not in {"ru", "uk", "en"}:
            result["language"] = "ru"
        if result["graphics_profile"] not in {"beauty", "balanced", "low"}:
            result["graphics_profile"] = "balanced"
        if result["gpu_mode"] not in {"auto", "old-amd", "old-intel", "safe"}:
            result["gpu_mode"] = "auto"
        return result

    @staticmethod
    def _skin_payload(username):
        username = (username or "").strip()
        payload = {
            "username": username,
            "choice": L.get_skin_choice("skins"),
            "image": "",
        }
        try:
            image = L.render_player_head(username, 96)
            if image is not None:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                payload["image"] = (
                    "data:image/png;base64,"
                    + base64.b64encode(buffer.getvalue()).decode("ascii")
                )
        except Exception:  # noqa: BLE001
            pass
        return payload

    # ================= ГЛАВНАЯ =================
    def get_boot(self):
        # ВАЖНО: ничего сетевого здесь — этот вызов должен возвращаться мгновенно,
        # иначе окно висит на «—» и кажется, будто кнопки не работают. Новости и
        # статус сервера грузятся отдельными вызовами (get_news / get_server).
        s = L.load_settings()
        loader_key = L.CONFIG.get("MOD_LOADER", "")
        loader = L.LOADER_DISPLAY_NAMES.get(loader_key, (loader_key or "").capitalize())
        sys_ram, ram_max, rec = self._memory_profile()
        memory_auto = bool(s.get("memory_auto"))
        memory_mb = rec if memory_auto else int(
            s.get("memory_mb", L.CONFIG.get("MEMORY_MB", 4096)))
        server_ip = (L.CONFIG.get("PINNED_SERVER") or {}).get("ip", "")
        return {
            "server_ip": server_ip,
            "launcher_version": L.CONFIG.get("LAUNCHER_VERSION", ""),
            "modpack_version": str(L.CONFIG.get("MODPACK_VERSION", "")),
            "mc": L.CONFIG.get("MC_VERSION", ""),
            "loader": loader,
            "loader_version": L.CONFIG.get("LOADER_VERSION", ""),
            "mods_count": _mods_count(),
            "nick": s.get("username", ""),
            "memory_mb": memory_mb,
            "memory_auto": memory_auto,
            "low_end": bool(s.get("low_end_mode")),
            "no_sodium": bool(s.get("no_sodium")),
            "ram_min": 2048,
            "ram_max": ram_max,
            "ram_rec": rec,
            "sys_ram": sys_ram,
            "install_dir": str(L.INSTANCE_DIR),
            "ui_settings": self._ui_settings(),
            "client_mods": L.get_optional_mods_selection(),
            "skin_choice": L.get_skin_choice("skins"),
            "status": "Готово к запуску",
        }

    def load_news(self):
        # Сеть — в фоне, результат прилетает в страницу через pushNews. Так
        # окно не ждёт ответа сервера и остаётся отзывчивым.
        if not self._ui_settings().get("show_news", True):
            self._js("window.pushNews && window.pushNews([])")
            return

        def worker():
            try:
                news = L.fetch_server_news()
            except Exception:  # noqa: BLE001
                news = []
            self._js("window.pushNews && window.pushNews(%s)" % _q(news))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_server(self):
        # Пинг сервера тоже в фоне — иначе окно подвисало бы на время опроса.
        def worker():
            data = {"online": False}
            try:
                pinned = L.CONFIG.get("PINNED_SERVER") or {}
                host, port = L.parse_host_port(pinned.get("ip", ""))
                st = L.ping_server(host, port)
                data = {"online": bool(st.get("online")),
                        "players": st.get("players_online"),
                        "max": st.get("players_max"),
                        "ping": st.get("ping_ms")}
            except Exception:  # noqa: BLE001
                data = {"online": False}
            self._js("window.pushServer && window.pushServer(%s)" % _q(data))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_client_state(self):
        """Check whether the installed game files match the configured pack.

        Network work stays off the UI thread.  When the mirror is unavailable,
        launcher.py deliberately falls back to the local version, so an already
        installed client remains playable offline instead of showing a false
        update warning forever.
        """
        def local_snapshot():
            local_version = L.get_local_modpack_version()
            marker = L._read_install_marker()
            signature = L._install_signature()
            base_ready = bool(marker) and all(
                marker.get(key) == value for key, value in signature.items()
            )
            version_id = marker.get("version_id") if marker else None
            if base_ready and version_id:
                version_json = (
                    L.INSTANCE_DIR / "versions" / version_id
                    / (str(version_id) + ".json")
                )
                base_ready = version_json.exists()
            else:
                base_ready = False
            managed_ready = all(
                (L.INSTANCE_DIR / folder).is_dir() for folder in ("mods", "config")
            )
            return local_version, bool(
                base_ready and managed_ready and local_version != -1
            )

        local_version, local_ready = local_snapshot()
        initial = {
            "state": "ready" if local_ready else "checking",
            "needs_update": False,
            "can_launch": local_ready,
            "offline": False,
            "background_check": local_ready,
            "local_version": local_version,
            "detail": (
                "Клиент готов"
                if local_ready else "Проверяем установленный клиент…"
            ),
        }
        self._js(
            "window.pushClientState && window.pushClientState(%s)" % _q(initial)
        )

        def worker():
            payload = {
                "state": "warning",
                "needs_update": True,
                "can_launch": True,
                "offline": False,
            }
            try:
                local_version, local_ready = local_snapshot()
                version_status = L.get_modpack_version_status()
                remote_version = int(version_status["version"])
                online = version_status.get("online")
                pack_ready = local_version != -1 and local_version == remote_version
                ready = bool(local_ready and pack_ready)
                offline = online is False
                can_launch = bool(not offline or local_ready)
                if offline:
                    state = "offline" if local_ready else "error"
                    detail = (
                        "Офлайн · локальный клиент готов"
                        if local_ready
                        else "Нет сети — для первой установки нужен интернет"
                    )
                else:
                    state = "ready" if ready else "warning"
                    detail = (
                        "Клиент готов"
                        if ready
                        else ("Требуется установка" if local_version == -1
                              else "Требуется обновление")
                    )
                payload = {
                    "state": state,
                    "needs_update": bool(not offline and not ready),
                    "can_launch": can_launch,
                    "offline": offline,
                    "local_version": local_version,
                    "remote_version": remote_version,
                    "detail": detail,
                }
            except Exception as exc:  # noqa: BLE001
                # No network is not fatal.  A valid local installation can
                # still be launched; launch_game performs the authoritative
                # check again when the player presses Play.
                try:
                    _local_version, ready = local_snapshot()
                except Exception:  # noqa: BLE001
                    ready = False
                payload = {
                    "state": "offline" if ready else "error",
                    "needs_update": False,
                    "can_launch": ready,
                    "offline": True,
                    "detail": (
                        "Офлайн · локальный клиент готов"
                        if ready else "Нет сети — локальный клиент не установлен"
                    ),
                    "error": str(exc),
                }
            self._js(
                "window.pushClientState && window.pushClientState(%s)"
                % _q(payload)
            )

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True}

    def save_nick(self, nick):
        nick = _clean_nick(nick)
        if not nick:
            return {"ok": False, "error": "Invalid nickname"}
        try:
            L.update_settings(username=nick)
            return {"ok": True, "nick": nick}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ================= ЗАПУСК =================
    def _hide_launcher_for_game(self):
        try:
            if self._window is not None:
                self._window.hide()
                self._hidden_for_game = True
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _restore_launcher_after_game(self):
        if self._window is None:
            return
        try:
            self._window.restore()
        except Exception:  # noqa: BLE001
            pass
        try:
            # pywebview's WinForms show() also calls Activate(), returning
            # keyboard focus without relying on a backend-specific API.
            self._window.show()
            self._hidden_for_game = False
        except Exception:  # noqa: BLE001
            pass

    def play(self, nick):
        if self._launching or self._busy:
            return {"ok": False, "started": False,
                    "error": "Дождитесь окончания текущей операции"}
        active_session = L.get_active_game_session()
        if active_session:
            error = "Minecraft уже запущен (процесс %s)" % active_session.get(
                "pid", "?"
            )
            self._launch_telemetry("error", error, phase="preflight")
            return {"ok": False, "started": False, "error": error}
        raw_nick = str(nick or "").strip()
        nick = _clean_nick(raw_nick)
        if not nick or nick != raw_nick:
            error = "Ник — только латинские буквы, цифры и _"
            self._launch_telemetry("error", error, phase="preflight")
            return {"ok": False, "started": False, "error": error}
        try:
            s = L.load_settings()
            if s.get("memory_auto"):
                _sys_ram, _ram_max, mem = self._memory_profile()
            else:
                mem = int(s.get("memory_mb", L.CONFIG.get("MEMORY_MB", 4096)))
            low = bool(s.get("low_end_mode"))
            minimize_on_launch = _as_bool(s.get("minimize_on_launch", True))
            L.update_settings(username=nick)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            self._launch_telemetry("error", error, phase="preflight")
            return {"ok": False, "started": False, "error": error}

        self._launching = True
        telemetry = {"text": "Подготовка клиента", "phase": "preparing"}

        def phase_for(text):
            value = str(text or "").lower()
            for pattern, phase in (
                ("java", "java"),
                ("minecraft", "minecraft"),
                ("neoforge", "modloader"),
                ("forge", "modloader"),
                ("сборка модов", "modpack"),
                ("моды и дополнения", "addons"),
                ("настройки сборки", "configuration"),
                ("запуск игры", "launching"),
            ):
                if pattern in value:
                    return phase
            return telemetry["phase"]

        def status_cb(text):
            telemetry["text"] = str(text)
            telemetry["phase"] = phase_for(text)
            self._launch_telemetry("busy", text, phase=telemetry["phase"])

        def progress_cb(pct):
            try:
                self._launch_telemetry(
                    "busy", telemetry["text"], int(pct),
                    phase=telemetry["phase"],
                )
            except Exception:  # noqa: BLE001
                pass

        def worker():
            proc = None
            restore_window = False
            try:
                proc = L.launch_game(nick, mem, low, status_cb, progress_cb)
                self._launch_telemetry(
                    "busy", "Игра запущена", phase="running"
                )
                if proc is not None:
                    if minimize_on_launch:
                        restore_window = self._hide_launcher_for_game()
                    try:
                        proc.wait()
                    finally:
                        L.record_game_finished(proc)
                rc = getattr(proc, "returncode", 0) or 0
                if rc:
                    self._launch_telemetry(
                        "error", "Игра закрылась с ошибкой", phase="exited"
                    )
                else:
                    self._launch_telemetry(
                        "idle", "Готово к запуску", phase="exited"
                    )
            except Exception as exc:  # noqa: BLE001
                restore_window = True
                self._launch_telemetry("error", str(exc), phase="failed")
                L.runtime_log(
                    "web_launch_failed: %s", exc, level=40,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            finally:
                if restore_window:
                    self._restore_launcher_after_game()
                self._launching = False

        self._launch_telemetry("busy", "Подготовка клиента", 0, phase="queued")
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True}

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

    def get_client_mods(self):
        """Return the real optional/client mods configured by launcher.py."""
        try:
            selection = L.get_optional_mods_selection()
            configured = list(L.CONFIG.get("OPTIONAL_MODS", []))
            icons = self._pack_icons([
                str(mod.get("slug") or "") for mod in configured
            ])
            mods = []
            for mod in configured:
                mod_id = str(mod.get("id") or "")
                if not mod_id:
                    continue
                mods.append({
                    "id": mod_id,
                    "name": mod.get("name", mod_id),
                    "description": mod.get("description", ""),
                    "category": mod.get("category", ""),
                    "slug": mod.get("slug", ""),
                    "icon": icons.get(str(mod.get("slug") or ""), ""),
                    "enabled": bool(selection.get(mod_id, mod.get("default", True))),
                })
            return {"ok": True, "mods": mods, "selection": selection}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "mods": [], "selection": {}}

    def set_client_mod(self, mod_id, enabled):
        mod_id = str(mod_id or "").strip()
        allowed = {
            str(mod.get("id"))
            for mod in L.CONFIG.get("OPTIONAL_MODS", [])
            if mod.get("id")
        }
        if mod_id not in allowed:
            return {"ok": False, "error": "Unknown client mod", "id": mod_id}
        try:
            selection = L.get_optional_mods_selection()
            selection[mod_id] = _as_bool(enabled)
            L.save_optional_mods_selection(selection)
            return {
                "ok": True,
                "id": mod_id,
                "enabled": selection[mod_id],
                "selection": selection,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "id": mod_id}

    def set_client_mods(self, values):
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except Exception:  # noqa: BLE001
                values = None
        if not isinstance(values, dict):
            return {"ok": False, "error": "Client mod state must be an object"}
        allowed = {
            str(mod.get("id"))
            for mod in L.CONFIG.get("OPTIONAL_MODS", [])
            if mod.get("id")
        }
        try:
            selection = L.get_optional_mods_selection()
            for mod_id, enabled in values.items():
                mod_id = str(mod_id)
                if mod_id in allowed:
                    selection[mod_id] = _as_bool(enabled)
            L.save_optional_mods_selection(selection)
            return {"ok": True, "selection": selection}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # Backwards-compatible optional-mod names used by the Tkinter UI/model.
    def get_optional_mods(self):
        return self.get_client_mods()

    def set_optional_mod(self, mod_id, enabled):
        return self.set_client_mod(mod_id, enabled)

    def toggle_client_mod(self, mod_id, enabled):
        """Compatibility name used by the approved center-control page."""
        return self.set_client_mod(mod_id, enabled)

    # ================= НАСТРОЙКИ =================
    def set_memory(self, mb):
        try:
            value = int(mb)
            _sys_ram, ram_max, recommended = self._memory_profile()
            automatic = value <= 0
            value = recommended if automatic else max(2048, min(value, ram_max))
            L.update_settings(memory_mb=value, memory_auto=automatic)
            return {"memory_mb": value, "memory_auto": automatic}
        except Exception:  # noqa: BLE001
            return None

    def set_low_end(self, flag):
        try:
            L.update_settings(low_end_mode=_as_bool(flag))
        except Exception:  # noqa: BLE001
            pass

    def set_no_sodium(self, flag):
        try:
            L.update_settings(no_sodium=_as_bool(flag))
        except Exception:  # noqa: BLE001
            pass

    def get_ui_settings(self):
        """Settings owned by the HTML shell, persisted in launcher settings."""
        return self._ui_settings()

    def save_ui_settings(self, values):
        if not isinstance(values, dict):
            return {"ok": False, "error": "Settings must be an object"}
        current = self._ui_settings()
        update = {}
        for key in ("auto_updates", "minimize_on_launch", "show_news"):
            if key in values:
                update[key] = _as_bool(values[key])
        language = values.get("language")
        if language is not None:
            if language not in {"ru", "uk", "en"}:
                return {"ok": False, "error": "Unsupported language"}
            update["language"] = language
        graphics_profile = values.get("graphics_profile")
        if graphics_profile is not None:
            if graphics_profile not in {"beauty", "balanced", "low"}:
                return {"ok": False, "error": "Unsupported graphics profile"}
            update["graphics_profile"] = graphics_profile
        gpu_mode = values.get("gpu_mode")
        if gpu_mode is not None:
            if gpu_mode not in {"auto", "old-amd", "old-intel", "safe"}:
                return {"ok": False, "error": "Unsupported GPU mode"}
            update["gpu_mode"] = gpu_mode
        try:
            if update:
                L.update_settings(**update)
            current.update(update)
            return {"ok": True, "settings": current}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # Kept as a generic alias for the current prototype and older UI builds.
    def save_settings(self, values):
        return self.save_ui_settings(values)

    def set_language(self, language):
        return self.save_ui_settings({"language": language})

    def get_install_dir(self):
        return {"ok": True, "path": str(L.INSTANCE_DIR)}

    def choose_install_dir(self):
        """Choose a destination; moving is a separate, explicit operation."""
        if self._busy or self._launching:
            return {"ok": False, "error": "Close the game and wait for the current operation"}
        try:
            chosen = self._choose_path(folder=True)
            if not chosen:
                return {"ok": False, "cancelled": True, "path": str(L.INSTANCE_DIR)}
            target = Path(os.path.expandvars(chosen)).expanduser().resolve()
            current = Path(L.INSTANCE_DIR).resolve()
            if target.is_file():
                return {"ok": False, "error": "A folder is required"}
            # Match the proven Tkinter flow: choosing a non-empty parent puts
            # the installation in its own Industrial Horizon subdirectory.
            if target != current and target.exists() and any(target.iterdir()):
                target = target / (L.CONFIG.get("PACK_NAME") or "Industrial Horizon")
            return {
                "ok": True,
                "path": str(target),
                "current": str(current),
                "changed": target != current,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "path": str(L.INSTANCE_DIR)}

    def move_installation(self, path):
        """Move through launcher's validated implementation, never in JS."""
        if self._busy or self._launching:
            return {"ok": False, "error": "Close the game and wait for the current operation"}
        try:
            raw = os.path.expandvars(str(path or "").strip())
            if not raw:
                raise ValueError("Installation path is empty")
            target = Path(raw).expanduser()
            if not target.is_absolute():
                raise ValueError("Installation path must be absolute")
            target = target.resolve()
            if target.is_file():
                raise ValueError("A folder is required")
            current = Path(L.INSTANCE_DIR).resolve()
            if target == current:
                return {"ok": True, "started": False, "path": str(current)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "path": str(L.INSTANCE_DIR)}

        self._busy = True

        def notify(state, text):
            payload = {"state": state, "text": str(text), "path": str(target)}
            self._js(
                "window.onInstallPathState && window.onInstallPathState(%s)"
                % _q(payload)
            )

        def worker():
            try:
                notify("moving", "Moving the game files…")
                L.move_installation(
                    target,
                    status_cb=lambda text: notify("moving", text),
                )
                notify("ready", "Installation moved")
                self._toast("Папка игры изменена: %s" % L.INSTANCE_DIR, "ok")
            except Exception as exc:  # noqa: BLE001
                notify("error", exc)
                self._toast("Не удалось перенести игру: %s" % exc, "err")
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True, "path": str(target)}

    # Name used by some intermediate prototype builds.
    def set_install_dir(self, path):
        return self.move_installation(path)

    def repair(self):
        if self._busy or self._launching:
            error = "Дождитесь окончания текущей операции"
            self._toast(error, "err")
            return {"ok": False, "started": False, "error": error}
        active_session = L.get_active_game_session()
        if active_session:
            error = "Закройте Minecraft перед восстановлением клиента"
            self._repair_state("error", error)
            self._toast(error, "err")
            return {"ok": False, "started": False, "error": error}
        self._busy = True
        state = {"text": "Проверка системных файлов"}

        def status_cb(text):
            state["text"] = str(text)
            self._repair_state("progress", text)

        def progress_cb(pct):
            try:
                self._repair_state("progress", state["text"], int(pct))
            except Exception:  # noqa: BLE001
                pass

        def worker():
            try:
                L.repair_installation(status_cb, progress_cb)
                detail = "Системные файлы сброшены — установка продолжится при запуске"
                self._repair_state("complete", detail, 100)
                self._toast("Проверка подготовлена. Нажмите «Играть» для переустановки.", "ok")
            except Exception as exc:  # noqa: BLE001
                self._repair_state("error", str(exc))
                self._toast("Не удалось переустановить: %s" % exc, "err")
            finally:
                self._busy = False

        self._repair_state("progress", state["text"], 0)
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True}

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
                    launch_log = L.INSTANCE_DIR / "latest_launch.log"
                    if launch_log.exists():
                        zf.write(launch_log, "latest_launch.log")
                        found += 1
                    crash_dir = L.INSTANCE_DIR / "crash-reports"
                    if crash_dir.exists():
                        crashes = sorted(crash_dir.glob("crash-*.txt"),
                                         key=lambda p: p.stat().st_mtime)
                        for p in crashes[-2:]:
                            zf.write(p, "crash-reports/" + p.name)
                            found += 1
                    for path in L.get_runtime_log_files():
                        zf.write(path, "launcher/" + path.name)
                        found += 1
                    game_history = L.RUNTIME_LOG_DIR / "game"
                    if game_history.is_dir():
                        history = sorted(
                            game_history.glob("launch_*.log"),
                            key=lambda p: p.stat().st_mtime,
                        )
                        for path in history[-3:]:
                            zf.write(path, "launcher/game/" + path.name)
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
    # Stable, descriptive names used by the center-control prototype. Keep the
    # original methods for compatibility with already shipped pages.
    def repair_files(self):
        return self.repair()

    def export_logs(self):
        return self.collect_logs()

    def open_logs_folder(self):
        try:
            target = L.INSTANCE_DIR / "logs"
            L.open_folder(target)
            return {"ok": True, "path": str(target)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

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
        slug = str(slug or "")
        cfg = next((c for c in L.CONFIG.get("RECOMMENDED_SHADER_PACKS", [])
                    if c.get("slug") == slug), None)
        if not cfg:
            error = "Неизвестный шейдер: %s" % slug
            self._catalog_install_state("shader", slug, "error", error)
            return {"ok": False, "started": False, "error": error}
        job = ("shader", slug)
        if job in self._catalog_jobs:
            return {"ok": False, "started": False,
                    "error": "Установка уже выполняется"}
        self._catalog_jobs.add(job)

        def status(text):
            self._catalog_install_state("shader", slug, "installing", text)

        def worker():
            try:
                L.install_recommended_shader_pack(cfg, status)
                for p in L.list_shader_packs():
                    if cfg.get("name", "").split(" —")[0].lower() in p.get("name", "").lower():
                        L.set_shader_enabled(p, True)
                        break
                detail = "Шейдер «%s» установлен и включён." % cfg.get("name")
                self._catalog_install_state("shader", slug, "done", detail, 100)
                self._toast(detail, "ok")
                self._js("window.refreshShaders && window.refreshShaders()")
            except Exception as exc:  # noqa: BLE001
                detail = "Не удалось установить шейдер: %s" % exc
                self._catalog_install_state("shader", slug, "error", detail)
                self._toast(detail, "err")
            finally:
                self._catalog_jobs.discard(job)

        self._catalog_install_state("shader", slug, "installing", "Установка шейдера")
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True, "kind": "shader", "slug": slug}

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
        slug = str(slug or "")
        cfg = next((c for c in L.CONFIG.get("RECOMMENDED_RESOURCE_PACKS", [])
                    if c.get("slug") == slug), None)
        if not cfg:
            error = "Неизвестный ресурс-пак: %s" % slug
            self._catalog_install_state("resource", slug, "error", error)
            return {"ok": False, "started": False, "error": error}
        job = ("resource", slug)
        if job in self._catalog_jobs:
            return {"ok": False, "started": False,
                    "error": "Установка уже выполняется"}
        self._catalog_jobs.add(job)

        def status(text):
            self._catalog_install_state("resource", slug, "installing", text)

        def worker():
            try:
                L.install_recommended_resource_pack(cfg, status)
                detail = "Ресурс-пак «%s» установлен." % cfg.get("name")
                self._catalog_install_state("resource", slug, "done", detail, 100)
                self._toast(detail, "ok")
                self._js("window.refreshResources && window.refreshResources()")
            except Exception as exc:  # noqa: BLE001
                detail = "Не удалось установить пак: %s" % exc
                self._catalog_install_state("resource", slug, "error", detail)
                self._toast(detail, "err")
            finally:
                self._catalog_jobs.discard(job)

        self._catalog_install_state(
            "resource", slug, "installing", "Установка ресурс-пака"
        )
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True, "kind": "resource", "slug": slug}

    def get_skin_state(self, username=None):
        username = _clean_nick(username or L.load_settings().get("username", ""))
        return {"ok": True, **self._skin_payload(username)}

    def choose_skin_file(self, username=None):
        username = _clean_nick(username or L.load_settings().get("username", ""))
        if not username:
            return {"ok": False, "error": "Enter a nickname first"}
        try:
            chosen = self._choose_path(folder=False, file_types=("PNG (*.png)",))
            if not chosen:
                return {"ok": False, "cancelled": True}
            L.install_own_skin_file("skins", chosen, username)
            payload = self._skin_payload(username)
            self._js("window.onSkinChanged && window.onSkinChanged(%s)" % _q(payload))
            return {"ok": True, **payload}
        except Exception as exc:  # noqa: BLE001
            self._toast("Не удалось применить скин: %s" % exc, "err")
            return {"ok": False, "error": str(exc)}

    def reset_skin(self, username=None):
        username = _clean_nick(username or L.load_settings().get("username", ""))
        if not username:
            return {"ok": False, "error": "Enter a nickname first"}
        try:
            L.clear_pack_skin("skins", username)
            payload = self._skin_payload(username)
            self._js("window.onSkinChanged && window.onSkinChanged(%s)" % _q(payload))
            return {"ok": True, **payload}
        except Exception as exc:  # noqa: BLE001
            self._toast("Не удалось сбросить скин: %s" % exc, "err")
            return {"ok": False, "error": str(exc)}

    def open_skins_folder(self):
        try:
            target = L.get_localskin_dir() / "skins"
            L.open_folder(target)
            return {"ok": True, "path": str(target)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ================= СЛУЖЕБНОЕ / ССЫЛКИ =================
    def open_folder(self):
        try:
            L.open_folder(L.INSTANCE_DIR)
            return {"ok": True, "path": str(L.INSTANCE_DIR)}
        except Exception:  # noqa: BLE001
            try:
                os.startfile(str(L.INSTANCE_DIR))  # noqa: S606
                return {"ok": True, "path": str(L.INSTANCE_DIR)}
            except Exception:  # noqa: BLE001
                return {"ok": False, "path": str(L.INSTANCE_DIR)}

    def open_game_folder(self):
        return self.open_folder()

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
            if not self._ui_settings().get("auto_updates", True):
                return None
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

                # Executable updates are accepted only from HTTPS and only
                # with the adjacent SHA-256 sidecar published by CI.
                if urllib.parse.urlsplit(str(exe_url)).scheme.lower() != "https":
                    raise RuntimeError("небезопасный источник обновления")
                expected_sha256 = (info.get("sha256") if "sha256" in info
                                   else L.fetch_update_sha256(exe_url))
                if not expected_sha256:
                    raise RuntimeError("контрольная сумма обновления недоступна")
                L.download_file(exe_url, new_exe, prog)
                if not L.verify_update_installer(new_exe, expected_sha256):
                    new_exe.unlink(missing_ok=True)
                    raise RuntimeError("контрольная сумма обновления не совпала")
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
            self._window.minimize()
        except Exception:  # noqa: BLE001
            pass

    def maximize(self):
        try:
            self._window.toggle_fullscreen()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        try:
            _release_single_instance_lock()
            self._window.destroy()
        except Exception:  # noqa: BLE001
            pass


def main():
    if "--selftest" in sys.argv:
        return
    L.install_runtime_exception_hooks()
    if "--tkinter-ui" in sys.argv:
        L.main()
        return
    if not L.acquire_single_instance_lock():
        return

    # Игра может стоять не на диске C — как и в старом лаунчере, подхватываем
    # выбранную папку установки до обращения к файлам.
    try:
        L.set_install_dir(L.get_saved_install_dir())
    except Exception:  # noqa: BLE001
        pass

    try:
        import webview  # pywebview — может отсутствовать/не завестись без WebView2
        api = Api()
        test_screen = "--test-screen" in sys.argv or os.environ.get("IH_TEST_SCREEN") == "1"
        force_center_control = (
            "--center-control" in sys.argv
            or os.environ.get("IH_CENTER_CONTROL") == "1"
        )
        legacy_ui = (
            "--legacy-ui" in sys.argv
            or "--legacy-index" in sys.argv
            or os.environ.get("IH_LEGACY_UI") == "1"
            or os.environ.get("IH_LEGACY_INDEX") == "1"
        )

        # The approved center-control screen is the production default. The
        # previous index remains explicitly accessible for support/rollback,
        # while --test-screen keeps its established development behaviour.
        if force_center_control:
            center_control = True
            ui_file = "ui/center-control-layouts.html"
        elif test_screen:
            center_control = False
            ui_file = "ui/test-screen.html"
        elif legacy_ui:
            center_control = False
            ui_file = "ui/index.html"
        else:
            center_control = True
            ui_file = "ui/center-control-layouts.html"
        window_title = L.CONFIG.get("WINDOW_TITLE") or "Industrial Horizon"
        if test_screen and not force_center_control:
            window_title += " — тестовый интерфейс"
        # Обычное системное окно (не frameless): его можно свободно растягивать
        # за края, есть родные «свернуть/развернуть/закрыть», и ничего не
        # перехватывает клики. min_size не даёт сжать до нечитаемого.
        startup_screen = _primary_webview_screen(webview)
        if center_control:
            initial_width, initial_height = _center_control_window_size(
                startup_screen
            )
        else:
            initial_width = 1080 if test_screen else 1160
            initial_height = 680 if test_screen else 740
        # The compact production layout is designed for an approximately
        # 900px client area.  A 960px native minimum leaves room for window
        # borders while preventing the bottom console from collapsing into an
        # unusable ultra-narrow strip.  600px height still fits comfortably on
        # common 1366x768 displays (including the taskbar and window chrome).
        minimum_size = CENTER_MIN_SIZE if center_control else (900, 600)
        window = webview.create_window(
            window_title,
            url=_res(ui_file),
            js_api=api,
            width=initial_width,
            height=initial_height,
            x=None,
            y=None,
            screen=startup_screen,
            min_size=minimum_size,
            resizable=True,
            fullscreen=False,
            minimized=False,
            maximized=False,
            background_color="#0a0e14",
        )
        api._window = window
        webview.start(
            lambda: _start_web_runtime(
                api, window, webview, initial_width, initial_height,
                screen=startup_screen,
            )
        )
        _release_single_instance_lock()
    except Exception as exc:  # noqa: BLE001
        # Новый интерфейс не поднялся (чаще всего — на ПК нет среды WebView2).
        # Падать нельзя: откатываемся на проверенный старый tkinter-лаунчер,
        # чтобы у человека в любом случае работала кнопка «Играть».
        try:
            import traceback
            traceback.print_exc()
        except Exception:  # noqa: BLE001
            pass
        L.runtime_log(
            "webview_start_failed; falling back to tkinter: %s", exc,
            level=40,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        # The web window already owns the single-instance socket.  Release it
        # before delegating to launcher.main(), which acquires the same lock for
        # the proven Tkinter fallback.
        _release_single_instance_lock()
        L.main()


if __name__ == "__main__":
    main()
