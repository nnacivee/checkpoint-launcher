import copy
import importlib.util
import json
import threading
import unittest
from pathlib import Path
from unittest import mock

import webui


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "launcher_16633_bedrock", ROOT / "launcher.py"
)
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


class BedrockModeTests(unittest.TestCase):
    PACK_FILES = [
        "IH-Bedrock-Parity-1.0.zip",
        "IH-Bedrock-Water-1.1.zip",
        "IH-GUI-Overhaul-Bedrock-Dark-2.1.zip",
        "IH-Bedrock-Style-Cursors-1.0.0.zip",
    ]

    def _settings_store(self, initial):
        stored = copy.deepcopy(initial)

        def load():
            return copy.deepcopy(stored)

        def save(value):
            stored.clear()
            stored.update(copy.deepcopy(value))

        def update(**values):
            stored.update(copy.deepcopy(values))

        return stored, load, save, update

    def _install_side_effect(self, stored):
        slugs = launcher.CONFIG["BEDROCK_MODE"]["resource_packs"]

        def install(_status_cb=None, _progress_cb=None):
            remembered = dict(stored.get("recommended_packs", {}))
            remembered.update(dict(zip(slugs, self.PACK_FILES)))
            stored["recommended_packs"] = remembered
            return list(self.PACK_FILES)

        return install

    def test_catalogue_is_exact_pinned_and_mode_is_complete(self):
        mode = launcher.CONFIG["BEDROCK_MODE"]
        mods = {
            entry["id"]: entry
            for entry in launcher.CONFIG["OPTIONAL_MODS"]
        }
        packs = {
            entry["slug"]: entry
            for entry in launcher.CONFIG["RECOMMENDED_RESOURCE_PACKS"]
        }

        self.assertEqual(
            mode["resource_packs"],
            [
                "bedrock-parity",
                "bedrock-waters",
                "gui-overhaul-bedrock-dark",
                "bedrock-style-cursors",
            ],
        )
        self.assertIn("default-dark-mode", mode["disable_resource_packs"])
        for mod_id in (
            "bedrock_hotbar",
            "third_person_death",
            "smooth_gui",
            "cursors_extended",
            "chat_animation",
            "chunks_fade_in",
            "not_enough_animations",
            "model_gap_fix",
            "smooth_swapping",
        ):
            self.assertIn(mod_id, mode["optional_mods"])
            self.assertFalse(mods[mod_id]["default"])
            self.assertTrue(mods[mod_id]["url"].startswith(
                "https://cdn.modrinth.com/"
            ))
            self.assertTrue(mods[mod_id]["hashes"]["sha1"])
            self.assertTrue(mods[mod_id]["hashes"]["sha512"])
        for slug in mode["resource_packs"]:
            self.assertTrue(packs[slug]["url"].startswith(
                "https://cdn.modrinth.com/"
            ))
            self.assertGreater(packs[slug]["size"], 0)
            self.assertTrue(packs[slug]["hashes"]["sha1"])
            self.assertTrue(packs[slug]["hashes"]["sha512"])

    def test_active_mode_forces_every_managed_mod_on(self):
        settings = {
            "bedrock_mode": {"enabled": True},
            "optional_mods": {},
        }
        with mock.patch.object(
            launcher, "load_settings", return_value=settings
        ):
            selected = launcher.get_optional_mods_selection()

        for mod_id in launcher.CONFIG["BEDROCK_MODE"]["optional_mods"]:
            self.assertTrue(selected[mod_id])

    def test_enable_and_disable_restore_mods_and_resource_pack_order(self):
        mode_ids = launcher.CONFIG["BEDROCK_MODE"]["optional_mods"]
        initial_selection = {mod_id: False for mod_id in mode_ids}
        initial_selection["chat_animation"] = True
        original_packs = [
            "vanilla",
            "file/Custom.zip",
            "file/DefaultDark.zip",
            "file/IH-Bedrock-Parity-1.0.zip",
        ]
        initial = {
            "optional_mods": initial_selection,
            "recommended_packs": {
                "default-dark-mode": "DefaultDark.zip",
            },
            "memory_mb": 4096,
        }
        stored, load, save, update = self._settings_store(initial)
        pack_state = list(original_packs)
        applied = []

        def get_packs():
            return list(pack_state)

        def set_packs(values):
            pack_state[:] = list(values)

        def apply_mods(selection, *_args, **_kwargs):
            applied.append(copy.deepcopy(selection))
            stored["memory_mb"] = 8192

        with (
            mock.patch.object(launcher, "load_settings", side_effect=load),
            mock.patch.object(launcher, "save_settings", side_effect=save),
            mock.patch.object(
                launcher, "update_settings", side_effect=update
            ),
            mock.patch.object(
                launcher,
                "_install_bedrock_mode_packs",
                side_effect=self._install_side_effect(stored),
            ),
            mock.patch.object(
                launcher, "get_enabled_resource_packs",
                side_effect=get_packs,
            ),
            mock.patch.object(
                launcher, "set_enabled_resource_packs",
                side_effect=set_packs,
            ),
            mock.patch.object(
                launcher, "_apply_bedrock_mode_mod_files",
                side_effect=apply_mods,
            ),
            mock.patch.object(
                launcher, "get_active_game_session", return_value=None
            ),
        ):
            enabled = launcher.set_bedrock_mode(True)
            self.assertTrue(enabled["enabled"])
            self.assertNotIn("file/DefaultDark.zip", pack_state)
            for filename in self.PACK_FILES:
                self.assertIn("file/" + filename, pack_state)
            self.assertEqual(
                pack_state.count("file/IH-Bedrock-Parity-1.0.zip"), 1
            )
            for mod_id in mode_ids:
                self.assertTrue(stored["optional_mods"][mod_id])
            self.assertEqual(
                set(stored["recommended_packs"]),
                {
                    "default-dark-mode",
                    *launcher.CONFIG["BEDROCK_MODE"]["resource_packs"],
                },
            )
            self.assertEqual(stored["memory_mb"], 8192)

            # Changes unrelated to the preset remain the player's changes.
            stored["optional_mods"]["more_culling"] = True
            pack_state.append("file/Added-While-Bedrock.zip")
            disabled = launcher.set_bedrock_mode(False)

        self.assertFalse(disabled["enabled"])
        self.assertEqual(
            pack_state,
            original_packs + ["file/Added-While-Bedrock.zip"],
        )
        self.assertTrue(stored["optional_mods"]["chat_animation"])
        self.assertTrue(stored["optional_mods"]["more_culling"])
        for mod_id in mode_ids:
            if mod_id != "chat_animation":
                self.assertFalse(stored["optional_mods"][mod_id])
        self.assertGreaterEqual(len(applied), 2)

    def test_failed_apply_rolls_back_visible_state(self):
        initial = {
            "optional_mods": {"chat_animation": False},
            "recommended_packs": {
                "default-dark-mode": "DefaultDark.zip",
            },
        }
        original = copy.deepcopy(initial)
        stored, load, save, update = self._settings_store(initial)
        pack_state = ["vanilla", "file/DefaultDark.zip"]
        calls = {"count": 0}

        def apply_mods(_selection, *_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                stored["recommended_packs"]["shader-installed-concurrently"] = (
                    "Shader.zip"
                )
                raise RuntimeError("broken optional mod")

        with (
            mock.patch.object(launcher, "load_settings", side_effect=load),
            mock.patch.object(launcher, "save_settings", side_effect=save),
            mock.patch.object(
                launcher, "update_settings", side_effect=update
            ),
            mock.patch.object(
                launcher,
                "_install_bedrock_mode_packs",
                side_effect=self._install_side_effect(stored),
            ),
            mock.patch.object(
                launcher, "get_enabled_resource_packs",
                side_effect=lambda: list(pack_state),
            ),
            mock.patch.object(
                launcher, "set_enabled_resource_packs",
                side_effect=lambda values: pack_state.__setitem__(
                    slice(None), list(values)
                ),
            ),
            mock.patch.object(
                launcher, "_apply_bedrock_mode_mod_files",
                side_effect=apply_mods,
            ),
            mock.patch.object(
                launcher, "get_active_game_session", return_value=None
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "broken optional mod"
            ):
                launcher.set_bedrock_mode(True)

        self.assertEqual(stored["optional_mods"], original["optional_mods"])
        self.assertEqual(
            stored["recommended_packs"]["default-dark-mode"],
            original["recommended_packs"]["default-dark-mode"],
        )
        self.assertEqual(
            stored["recommended_packs"]["shader-installed-concurrently"],
            "Shader.zip",
        )
        self.assertFalse(stored.get("bedrock_mode", {}).get("enabled"))
        self.assertEqual(pack_state, [
            "vanilla", "file/DefaultDark.zip"
        ])
        self.assertEqual(calls["count"], 2)

    def test_active_repair_keeps_launching_when_cosmetics_are_offline(self):
        old_entry = "file/Old-Bedrock-UI.zip"
        settings = {
            "bedrock_mode": {
                "enabled": True,
                "managed_resource_packs": [old_entry],
                "disabled_resource_packs": [],
                "restore": {
                    "mods": {},
                    "resource_packs": ["vanilla"],
                },
            },
            "recommended_packs": {},
        }
        stored, load, save, update = self._settings_store(settings)
        pack_state = ["vanilla", old_entry]
        with (
            mock.patch.object(launcher, "load_settings", side_effect=load),
            mock.patch.object(launcher, "save_settings", side_effect=save),
            mock.patch.object(
                launcher, "update_settings", side_effect=update
            ),
            mock.patch.object(
                launcher,
                "_install_bedrock_mode_packs",
                side_effect=OSError("offline"),
            ),
            mock.patch.object(
                launcher,
                "_existing_bedrock_mode_pack_entries",
                return_value=[old_entry],
            ),
            mock.patch.object(
                launcher, "get_enabled_resource_packs",
                side_effect=lambda: list(pack_state),
            ),
            mock.patch.object(
                launcher, "set_enabled_resource_packs",
                side_effect=lambda values: pack_state.__setitem__(
                    slice(None), list(values)
                ),
            ),
        ):
            result = launcher.ensure_bedrock_mode_applied()

        self.assertTrue(result["enabled"])
        self.assertEqual(pack_state, ["vanilla", old_entry])

    def test_active_repair_replaces_stale_managed_pack_filename(self):
        old_entry = "file/Old-Bedrock-UI.zip"
        new_entry = "file/" + self.PACK_FILES[0]
        settings = {
            "bedrock_mode": {
                "enabled": True,
                "managed_resource_packs": [old_entry],
                "disabled_resource_packs": [],
                "restore": {
                    "mods": {},
                    "resource_packs": ["vanilla"],
                },
            },
            "recommended_packs": {},
        }
        stored, load, save, update = self._settings_store(settings)
        pack_state = ["vanilla", old_entry]
        with (
            mock.patch.object(launcher, "load_settings", side_effect=load),
            mock.patch.object(launcher, "save_settings", side_effect=save),
            mock.patch.object(
                launcher, "update_settings", side_effect=update
            ),
            mock.patch.object(
                launcher,
                "_install_bedrock_mode_packs",
                return_value=[self.PACK_FILES[0]],
            ),
            mock.patch.object(
                launcher, "get_enabled_resource_packs",
                side_effect=lambda: list(pack_state),
            ),
            mock.patch.object(
                launcher, "set_enabled_resource_packs",
                side_effect=lambda values: pack_state.__setitem__(
                    slice(None), list(values)
                ),
            ),
        ):
            launcher.ensure_bedrock_mode_applied()

        self.assertNotIn(old_entry, pack_state)
        self.assertIn(new_entry, pack_state)

    def test_mode_cannot_change_while_minecraft_is_running(self):
        with mock.patch.object(
            launcher,
            "get_active_game_session",
            return_value={"pid": 1234},
        ):
            with self.assertRaises(launcher.GameAlreadyRunning):
                launcher.set_bedrock_mode(True)

    def test_ui_and_web_api_expose_the_mode(self):
        html = (ROOT / "ui" / "center-control-layouts.html").read_text(
            encoding="utf-8"
        )
        webui = (ROOT / "webui.py").read_text(encoding="utf-8")
        self.assertIn("data-bedrock-mode", html)
        self.assertIn("requestBedrockMode", html)
        self.assertIn("window.onBedrockModeState", html)
        self.assertIn("def get_bedrock_mode(", webui)
        self.assertIn("def set_bedrock_mode(", webui)

    def test_web_api_reports_backend_state_after_async_failure(self):
        api = webui.Api()
        emitted = []
        finished = threading.Event()

        def toast(*_args, **_kwargs):
            finished.set()

        with (
            mock.patch.object(
                webui.L, "get_active_game_session", return_value=None
            ),
            mock.patch.object(
                webui.L,
                "set_bedrock_mode",
                side_effect=RuntimeError("download failed"),
            ),
            mock.patch.object(
                webui.L,
                "get_bedrock_mode_state",
                return_value={"enabled": False},
            ),
            mock.patch.object(api, "_js", side_effect=emitted.append),
            mock.patch.object(api, "_toast", side_effect=toast),
        ):
            started = api.set_bedrock_mode(True)
            self.assertTrue(started["started"])
            self.assertTrue(finished.wait(2))

        payloads = []
        for script in emitted:
            marker = "window.onBedrockModeState("
            if marker not in script:
                continue
            raw = script.split(marker, 1)[1].rsplit(")", 1)[0]
            payloads.append(json.loads(raw))
        error = next(
            payload for payload in payloads
            if payload.get("state") == "error"
        )
        self.assertFalse(error["enabled"])

    def test_web_api_returns_forced_managed_mod_state(self):
        api = webui.Api()
        configured = [{
            "id": "bedrock_hotbar",
            "name": "Bedrock Hotbar",
            "default": False,
        }]
        with (
            mock.patch.dict(
                webui.L.CONFIG, {"OPTIONAL_MODS": configured}
            ),
            mock.patch.object(
                webui.L,
                "get_optional_mods_selection",
                return_value={"bedrock_hotbar": True},
            ),
            mock.patch.object(
                webui.L,
                "save_optional_mods_selection",
                return_value={"bedrock_hotbar": True},
            ),
        ):
            result = api.set_client_mod("bedrock_hotbar", False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["enabled"])
        self.assertTrue(result["selection"]["bedrock_hotbar"])


if __name__ == "__main__":
    unittest.main()
