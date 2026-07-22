import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import webui


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.api = webui.Api()

    def test_clean_nickname_keeps_minecraft_underscore(self):
        self.assertEqual(webui._clean_nick(" Ab_c-12 "), "Ab_c12")

    def test_boot_and_memory_use_real_settings_contract(self):
        saved = {}
        settings = {
            "username": "Test_User",
            "memory_auto": True,
            "memory_mb": 4096,
            "low_end_mode": False,
            "no_sodium": False,
            "language": "uk",
            "graphics_profile": "balanced",
            "gpu_mode": "auto",
        }
        with (
            mock.patch.object(webui.L, "load_settings", return_value=settings),
            mock.patch.object(webui.L, "update_settings",
                              side_effect=lambda **values: saved.update(values)),
            mock.patch.object(webui.L, "get_system_ram_mb", return_value=16384),
            mock.patch.object(webui.L, "get_optional_mods_selection",
                              return_value={"emi": True}),
            mock.patch.object(webui.L, "get_skin_choice", return_value=""),
        ):
            boot = self.api.get_boot()
            memory = self.api.set_memory(0)

        self.assertEqual(boot["nick"], "Test_User")
        self.assertTrue(boot["memory_auto"])
        self.assertEqual(boot["ui_settings"]["language"], "uk")
        self.assertTrue(memory["memory_auto"])
        self.assertTrue(saved["memory_auto"])

    def test_client_mods_are_whitelisted_and_persisted(self):
        configured = [{
            "id": "emi",
            "name": "EMI",
            "slug": "emi",
            "category": "Интерфейс",
            "description": "Recipes",
            "default": True,
        }]
        saved = {}
        with (
            mock.patch.dict(webui.L.CONFIG, {"OPTIONAL_MODS": configured}),
            mock.patch.object(webui.L, "get_optional_mods_selection",
                              return_value={"emi": True}),
            mock.patch.object(webui.L, "save_optional_mods_selection",
                              side_effect=lambda value: saved.update(value)),
            mock.patch.object(self.api, "_pack_icons",
                              return_value={"emi": "https://example.invalid/icon.webp"}),
        ):
            result = self.api.get_client_mods()
            changed = self.api.set_client_mod("emi", False)
            rejected = self.api.set_client_mod("unknown", True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["mods"][0]["id"], "emi")
        self.assertTrue(result["mods"][0]["icon"])
        self.assertTrue(changed["ok"])
        self.assertFalse(saved["emi"])
        self.assertFalse(rejected["ok"])

    def test_client_state_pushes_ready_for_current_install(self):
        finished = threading.Event()
        messages = []

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            (instance / "config").mkdir()
            version_json = (
                instance / "versions" / "test-neoforge" / "test-neoforge.json"
            )
            version_json.parent.mkdir(parents=True)
            version_json.write_text("{}", encoding="utf-8")

            def capture(code):
                messages.append(code)
                if "ClientState" in code and "ready" in code:
                    finished.set()

            marker = {**webui.L._install_signature(), "version_id": "test-neoforge"}
            self.api._js = capture
            with (
                mock.patch.object(webui.L, "INSTANCE_DIR", instance),
                mock.patch.object(webui.L, "get_local_modpack_version", return_value=4),
                mock.patch.object(
                    webui.L, "get_modpack_version_status",
                    return_value={"version": 4, "online": True},
                ),
                mock.patch.object(webui.L, "_read_install_marker", return_value=marker),
            ):
                self.api.refresh_client_state()
                self.assertTrue(finished.wait(2), messages)

        payload = "\n".join(messages)
        self.assertIn('"state": "ready"', payload)
        self.assertIn('"needs_update": false', payload)

    def test_empty_install_path_is_rejected_without_moving(self):
        with mock.patch.object(webui.L, "move_installation") as move:
            result = self.api.move_installation("")
        self.assertFalse(result["ok"])
        move.assert_not_called()


class MainTests(unittest.TestCase):
    @staticmethod
    def _fake_webview(created):
        window = types.SimpleNamespace(
            restore=lambda: None,
            show=lambda: None,
            destroy=lambda: None,
        )

        def create_window(title, **kwargs):
            created.update(title=title, **kwargs)
            return window

        return types.SimpleNamespace(
            create_window=create_window,
            start=lambda callback=None: callback() if callback else None,
        )

    def test_center_control_is_default_entry(self):
        created = {}
        fake_webview = self._fake_webview(created)
        with (
            mock.patch.object(sys, "argv", ["webui.py"]),
            mock.patch.dict(sys.modules, {"webview": fake_webview}),
            mock.patch.object(webui.L, "acquire_single_instance_lock", return_value=True),
            mock.patch.object(webui.L, "set_install_dir"),
            mock.patch.object(webui.L, "get_saved_install_dir", return_value=Path("C:/IH")),
            mock.patch.object(webui.Api, "_serve_single_instance"),
            mock.patch.object(webui, "_release_single_instance_lock"),
        ):
            webui.main()
        selected = str(created["url"]).replace("\\", "/")
        self.assertTrue(selected.endswith("ui/center-control-layouts.html"))

    def test_selftest_exits_before_window_initialization(self):
        with (
            mock.patch.object(sys, "argv", ["webui.py", "--selftest"]),
            mock.patch.object(webui.L, "acquire_single_instance_lock") as acquire,
        ):
            webui.main()
        acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
