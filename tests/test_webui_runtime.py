import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import webui


class WebUiRuntimeTests(unittest.TestCase):
    @staticmethod
    def _window():
        return types.SimpleNamespace(
            restore=lambda: None,
            show=lambda: None,
            destroy=lambda: None,
        )

    def test_webview_failure_releases_lock_before_tkinter_fallback(self):
        events = []

        def fail_window(*_args, **_kwargs):
            raise RuntimeError("simulated WebView failure")

        fake_webview = types.SimpleNamespace(
            create_window=fail_window,
            start=mock.Mock(),
        )
        with (
            mock.patch.object(sys, "argv", ["webui.py"]),
            mock.patch.dict(sys.modules, {"webview": fake_webview}),
            mock.patch.object(
                webui.L, "acquire_single_instance_lock", return_value=True
            ) as acquire,
            mock.patch.object(webui.L, "set_install_dir"),
            mock.patch.object(
                webui.L, "get_saved_install_dir", return_value=Path("C:/IH")
            ),
            mock.patch.object(
                webui, "_release_single_instance_lock",
                side_effect=lambda: events.append("release"),
            ) as release,
            mock.patch.object(
                webui.L, "main", side_effect=lambda: events.append("tkinter")
            ) as tkinter_main,
            mock.patch("traceback.print_exc"),
        ):
            webui.main()

        acquire.assert_called_once_with()
        release.assert_called_once_with()
        tkinter_main.assert_called_once_with()
        fake_webview.start.assert_not_called()
        self.assertEqual(events, ["release", "tkinter"])

    def test_normal_webview_shutdown_releases_single_instance_lock(self):
        events = []
        created = {}
        working_area = types.SimpleNamespace(
            X=0, Y=0, Width=2560, Height=1392
        )
        screen = types.SimpleNamespace(
            x=0, y=0, width=2560, height=1440, frame=working_area
        )

        def create_window(title, **kwargs):
            created.update(title=title, **kwargs)
            return self._window()

        def start(callback=None):
            events.append("webview")
            if callback:
                callback()

        fake_webview = types.SimpleNamespace(
            create_window=create_window,
            start=start,
            screens=[screen],
        )
        with (
            mock.patch.object(sys, "argv", ["webui.py"]),
            mock.patch.dict(sys.modules, {"webview": fake_webview}),
            mock.patch.object(
                webui.L, "acquire_single_instance_lock", return_value=True
            ),
            mock.patch.object(webui.L, "set_install_dir"),
            mock.patch.object(
                webui.L, "get_saved_install_dir", return_value=Path("C:/IH")
            ),
            mock.patch.object(
                webui.Api, "_serve_single_instance",
                side_effect=lambda: events.append("serve"),
            ) as serve,
            mock.patch.object(
                webui, "_release_single_instance_lock",
                side_effect=lambda: events.append("release"),
            ) as release,
        ):
            webui.main()

        serve.assert_called_once_with()
        release.assert_called_once_with()
        self.assertEqual(events, ["webview", "serve", "release"])
        self.assertTrue(
            str(created["url"]).replace("\\", "/").endswith(
                "ui/center-control-layouts.html"
            )
        )
        self.assertEqual((created["width"], created["height"]), (1320, 820))
        self.assertEqual(created["min_size"], (960, 600))
        self.assertIs(created["screen"], screen)
        self.assertIsNone(created["x"])
        self.assertIsNone(created["y"])
        self.assertTrue(created["resizable"])
        self.assertFalse(created["fullscreen"])
        self.assertFalse(created["minimized"])
        self.assertFalse(created["maximized"])

    def test_shown_window_is_restored_resized_and_centered_before_listener(self):
        events = []
        shown = mock.Mock()
        shown.wait.return_value = True
        window = types.SimpleNamespace(
            events=types.SimpleNamespace(shown=shown),
            restore=lambda: events.append(("restore",)),
            resize=lambda width, height: events.append(("resize", width, height)),
            move=lambda x, y: events.append(("move", x, y)),
        )
        screen = types.SimpleNamespace(x=100, y=50, width=1920, height=1080)
        fake_webview = types.SimpleNamespace(screens=[screen])
        api = mock.Mock()
        api._serve_single_instance.side_effect = lambda: events.append(("serve",))

        webui._start_web_runtime(api, window, fake_webview, 1320, 820)

        shown.wait.assert_called_once_with(10)
        api._serve_single_instance.assert_called_once_with()
        self.assertEqual(
            events,
            [
                ("restore",),
                ("resize", 1320, 820),
                ("move", 400, 180),
                ("serve",),
            ],
        )

    def test_preferred_size_uses_large_and_1366_work_areas(self):
        large = types.SimpleNamespace(
            x=0,
            y=0,
            width=2560,
            height=1440,
            frame=types.SimpleNamespace(X=0, Y=0, Width=2560, Height=1392),
        )
        compact = types.SimpleNamespace(
            x=0,
            y=0,
            width=1366,
            height=768,
            frame=types.SimpleNamespace(X=0, Y=0, Width=1366, Height=728),
        )

        self.assertEqual(webui._center_control_window_size(large), (1320, 820))
        compact_size = webui._center_control_window_size(compact)
        self.assertEqual(compact_size, (1068, 664))
        self.assertGreaterEqual(compact_size[0], webui.CENTER_MIN_SIZE[0])
        self.assertGreaterEqual(compact_size[1], webui.CENTER_MIN_SIZE[1])
        self.assertLessEqual(
            compact_size[0], compact.frame.Width - 2 * webui.CENTER_SCREEN_MARGIN[0]
        )
        self.assertLessEqual(
            compact_size[1], compact.frame.Height - 2 * webui.CENTER_SCREEN_MARGIN[1]
        )

    @staticmethod
    def _immediate_thread(target, daemon=True):
        del daemon
        return types.SimpleNamespace(start=target)

    def test_play_hides_only_for_running_process_and_restores_after_wait(self):
        events = []
        process = types.SimpleNamespace(
            returncode=0,
            wait=lambda: events.append("wait"),
        )
        window = types.SimpleNamespace(
            hide=lambda: events.append("hide"),
            restore=lambda: events.append("restore"),
            show=lambda: events.append("show"),
        )
        api = webui.Api()
        api._window = window

        def launch(*_args, **_kwargs):
            events.append("launch")
            return process

        with (
            mock.patch.object(
                webui.L, "load_settings",
                return_value={"memory_auto": False, "memory_mb": 4096},
            ),
            mock.patch.object(webui.L, "update_settings"),
            mock.patch.object(webui.L, "launch_game", side_effect=launch),
            mock.patch.object(
                webui.threading, "Thread", side_effect=self._immediate_thread
            ),
        ):
            api.play("Player")

        self.assertEqual(events, ["launch", "hide", "wait", "restore", "show"])
        self.assertFalse(api._launching)

    def test_play_does_not_hide_when_launcher_returns_no_process(self):
        window = mock.Mock()
        api = webui.Api()
        api._window = window
        with (
            mock.patch.object(
                webui.L, "load_settings",
                return_value={"memory_auto": False, "memory_mb": 4096},
            ),
            mock.patch.object(webui.L, "update_settings"),
            mock.patch.object(webui.L, "launch_game", return_value=None),
            mock.patch.object(
                webui.threading, "Thread", side_effect=self._immediate_thread
            ),
        ):
            api.play("Player")

        window.hide.assert_not_called()
        window.restore.assert_not_called()
        window.show.assert_not_called()

    def test_play_error_restores_and_focuses_visible_launcher(self):
        events = []
        window = types.SimpleNamespace(
            hide=lambda: events.append("hide"),
            restore=lambda: events.append("restore"),
            show=lambda: events.append("show"),
        )
        api = webui.Api()
        api._window = window
        with (
            mock.patch.object(
                webui.L, "load_settings",
                return_value={"memory_auto": False, "memory_mb": 4096},
            ),
            mock.patch.object(webui.L, "update_settings"),
            mock.patch.object(
                webui.L, "launch_game", side_effect=RuntimeError("boom")
            ),
            mock.patch.object(
                webui.threading, "Thread", side_effect=self._immediate_thread
            ),
        ):
            api.play("Player")

        self.assertEqual(events, ["restore", "show"])
        self.assertFalse(api._launching)

    def test_release_single_instance_lock_closes_server_and_clears_owner(self):
        server = mock.Mock()
        with (
            mock.patch.object(
                webui.L, "get_single_instance_server", return_value=server
            ),
            mock.patch.object(webui.L, "_single_instance_server", server),
        ):
            webui._release_single_instance_lock()
            self.assertIsNone(webui.L._single_instance_server)

        server.close.assert_called_once_with()

    def test_system_action_aliases_delegate_to_existing_implementations(self):
        api = webui.Api()
        marker = object()
        with (
            mock.patch.object(api, "open_folder", return_value=marker) as open_folder,
            mock.patch.object(api, "repair", return_value=marker) as repair,
            mock.patch.object(api, "collect_logs", return_value=marker) as logs,
            mock.patch.object(
                api, "get_client_mods", return_value=marker
            ) as get_client_mods,
            mock.patch.object(
                api, "set_client_mod", return_value=marker
            ) as set_client_mod,
        ):
            self.assertIs(api.open_game_folder(), marker)
            self.assertIs(api.repair_files(), marker)
            self.assertIs(api.export_logs(), marker)
            self.assertIs(api.get_optional_mods(), marker)
            self.assertIs(api.set_optional_mod("emi", False), marker)

        open_folder.assert_called_once_with()
        repair.assert_called_once_with()
        logs.assert_called_once_with()
        get_client_mods.assert_called_once_with()
        set_client_mod.assert_called_once_with("emi", False)

    def test_folder_endpoints_resolve_launcher_owned_paths(self):
        api = webui.Api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            local_skin = root / "LocalSkin"
            with (
                mock.patch.object(webui.L, "INSTANCE_DIR", instance),
                mock.patch.object(
                    webui.L, "get_localskin_dir", return_value=local_skin
                ),
                mock.patch.object(webui.L, "open_folder") as open_folder,
            ):
                install = api.get_install_dir()
                game = api.open_game_folder()
                logs = api.open_logs_folder()
                skins = api.open_skins_folder()

        self.assertEqual(install, {"ok": True, "path": str(instance)})
        self.assertEqual(game, {"ok": True, "path": str(instance)})
        self.assertEqual(logs, {"ok": True, "path": str(instance / "logs")})
        self.assertEqual(
            skins, {"ok": True, "path": str(local_skin / "skins")}
        )
        self.assertEqual(
            open_folder.call_args_list,
            [
                mock.call(instance),
                mock.call(instance / "logs"),
                mock.call(local_skin / "skins"),
            ],
        )

    def test_install_path_picker_is_non_destructive_and_alias_moves_explicitly(self):
        api = webui.Api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"
            selected = root / "selected"
            current.mkdir()
            selected.mkdir()
            with (
                mock.patch.object(webui.L, "INSTANCE_DIR", current),
                mock.patch.object(
                    api, "_choose_path", return_value=str(selected)
                ),
                mock.patch.object(webui.L, "move_installation") as move,
            ):
                chosen = api.choose_install_dir()
                unchanged = api.move_installation(str(current))

        self.assertTrue(chosen["ok"])
        self.assertTrue(chosen["changed"])
        self.assertEqual(Path(chosen["path"]), selected.resolve())
        self.assertEqual(
            unchanged,
            {"ok": True, "started": False, "path": str(current.resolve())},
        )
        move.assert_not_called()

        marker = {"ok": True, "started": True}
        with mock.patch.object(
            api, "move_installation", return_value=marker
        ) as move_explicitly:
            self.assertIs(api.set_install_dir("D:/Industrial Horizon"), marker)
        move_explicitly.assert_called_once_with("D:/Industrial Horizon")


if __name__ == "__main__":
    unittest.main()
