import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import launcher
import webui


class LauncherReliabilityTests(unittest.TestCase):
    @unittest.skipUnless(launcher._PIL_OK, "Pillow is required")
    def test_player_head_falls_back_to_bundled_nickname_skin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled = root / "skins" / "PlayerOne.png"
            bundled.parent.mkdir(parents=True)
            launcher.Image.new("RGBA", (64, 64), (40, 90, 140, 255)).save(bundled)
            local_root = root / "LocalSkin"
            with (
                mock.patch.object(launcher, "get_localskin_dir", return_value=local_root),
                mock.patch.object(
                    launcher, "resource_path", side_effect=lambda name: root / name
                ),
            ):
                head = launcher.render_player_head("PlayerOne", 48)
        self.assertIsNotNone(head)
        self.assertEqual(head.size, (48, 48))

    def test_source_configpack_override_is_local_and_never_affects_frozen_build(self):
        keys = (
            "CONFIGPACK_URL",
            "CONFIGPACK_VERSION_URL",
            "CONFIGPACK_MIRROR_URL",
            "CONFIGPACK_VERSION",
        )
        original = {key: launcher.CONFIG.get(key) for key in keys}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "quest_work" / "create_rework"
            candidate.mkdir(parents=True)
            (candidate / "configpack_v47_candidate.zip").write_bytes(b"candidate")
            (candidate / "configpack_version_v47_candidate.txt").write_text(
                "47\n", encoding="utf-8"
            )
            try:
                self.assertTrue(launcher._apply_local_configpack_override(root))
                self.assertTrue(launcher.CONFIG["CONFIGPACK_URL"].startswith("file:"))
                self.assertTrue(
                    launcher.CONFIG["CONFIGPACK_VERSION_URL"].startswith("file:")
                )
                self.assertEqual(launcher.CONFIG["CONFIGPACK_MIRROR_URL"], "")
                self.assertEqual(launcher.get_remote_configpack_version(), 47)
                with mock.patch.object(launcher.sys, "frozen", True, create=True):
                    self.assertFalse(
                        launcher._apply_local_configpack_override(
                            marker_path=root / "missing-marker.json"
                        )
                    )
                    marker = root / "dev_configpack_v47.json"
                    marker.write_text(json.dumps({
                        "enabled": True,
                        "version": 47,
                        "archive": str(candidate / "configpack_v47_candidate.zip"),
                        "version_file": str(
                            candidate / "configpack_version_v47_candidate.txt"
                        ),
                    }), encoding="utf-8")
                    self.assertTrue(
                        launcher._apply_local_configpack_override(marker_path=marker)
                    )
            finally:
                launcher.CONFIG.update(original)

    def test_settings_write_is_atomic_and_corrupt_primary_uses_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            with mock.patch.object(launcher, "SETTINGS_FILE", settings):
                launcher.save_settings({"username": "First"})
                launcher.save_settings({"username": "Second"})
                self.assertEqual(
                    json.loads(settings.read_text(encoding="utf-8")),
                    {"username": "Second"},
                )
                settings.write_text("{broken", encoding="utf-8")
                self.assertEqual(launcher.load_settings(), {"username": "First"})
                self.assertFalse(list(settings.parent.glob("*.tmp")))

    def test_managed_folder_commit_replaces_live_tree_only_after_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            (instance / "mods").mkdir(parents=True)
            (instance / "mods" / "old.jar").write_bytes(b"old")
            with mock.patch.object(launcher, "INSTANCE_DIR", instance):
                transaction, stage = launcher._begin_modpack_transaction()
                (stage / "mods").mkdir()
                (stage / "mods" / "new.jar").write_bytes(b"new")
                launcher._commit_managed_folders(transaction, ("mods",))

            self.assertFalse((instance / "mods" / "old.jar").exists())
            self.assertEqual((instance / "mods" / "new.jar").read_bytes(), b"new")
            self.assertFalse(
                (instance / launcher.MODPACK_TRANSACTION_DIR_NAME).exists()
            )

    def test_interrupted_commit_restores_previous_mods(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            transaction = instance / launcher.MODPACK_TRANSACTION_DIR_NAME
            backup = transaction / "backup" / "mods"
            stage = transaction / "stage"
            backup.mkdir(parents=True)
            stage.mkdir(parents=True)
            (backup / "old.jar").write_bytes(b"old")
            (instance / "mods").mkdir(parents=True)
            (instance / "mods" / "partial.jar").write_bytes(b"partial")
            (transaction / "journal.json").write_text(
                json.dumps({
                    "phase": "committing",
                    "roots": ["mods"],
                    "existed": ["mods"],
                }),
                encoding="utf-8",
            )

            with mock.patch.object(launcher, "INSTANCE_DIR", instance):
                self.assertTrue(launcher.recover_interrupted_modpack_update())

            self.assertEqual((instance / "mods" / "old.jar").read_bytes(), b"old")
            self.assertFalse((instance / "mods" / "partial.jar").exists())
            self.assertFalse(transaction.exists())

    def test_integrity_check_detects_missing_and_corrupt_jars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            entries = []
            for index in range(20):
                data = ("jar-%d" % index).encode("ascii")
                name = "core-%02d.jar" % index
                if index != 18:
                    (mods / name).write_bytes(b"wrong" if index == 19 else data)
                entries.append({
                    "path": "mods/" + name,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                })
            manifest = {"version": "1", "modsOnly": True, "files": entries}
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "APP_DATA_DIR", root / "app"),
                mock.patch.object(launcher, "load_settings", return_value={}),
                mock.patch.object(
                    launcher, "get_optional_mods_selection", return_value={}
                ),
                mock.patch.dict(
                    launcher.CONFIG,
                    {"REMOVED_MODS": [], "OPTIONAL_MODS": []},
                ),
            ):
                result = launcher.verify_modpack_integrity(manifest=manifest)

            self.assertFalse(result["ok"])
            self.assertEqual(result["missing"], ["core-18.jar"])
            self.assertEqual(result["corrupt"], ["core-19.jar"])

    def test_game_session_blocks_live_pid_and_clears_stale_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "game_session.json"
            session.write_text(json.dumps({"pid": 4321}), encoding="utf-8")
            with (
                mock.patch.object(launcher, "GAME_SESSION_FILE", session),
                mock.patch.object(launcher, "_process_is_running", return_value=True),
            ):
                self.assertEqual(launcher.get_active_game_session()["pid"], 4321)
            with (
                mock.patch.object(launcher, "GAME_SESSION_FILE", session),
                mock.patch.object(launcher, "_process_is_running", return_value=False),
            ):
                self.assertIsNone(launcher.get_active_game_session())
            self.assertFalse(session.exists())

    def test_repair_preserves_user_resource_and_shader_packs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            for name in launcher.REPAIRABLE_FOLDERS + ["resourcepacks", "shaderpacks"]:
                folder = instance / name
                folder.mkdir(parents=True)
                (folder / "keep.txt").write_text(name, encoding="utf-8")
            markers = {
                "MODPACK_VERSION_FILE": instance / ".modpack_version",
                "INSTALL_MARKER_FILE": instance / ".install_complete.json",
                "CONFIGPACK_MARKER_FILE": instance / ".configpack.json",
                "MODPACK_MANIFEST_CACHE_FILE": root / "manifest.json",
                "OPTIONAL_CACHE_DIR": root / "optional",
                "APP_DATA_DIR": root / "app",
            }
            with mock.patch.multiple(launcher, INSTANCE_DIR=instance, **markers):
                launcher.repair_installation()

            for name in launcher.REPAIRABLE_FOLDERS:
                self.assertFalse((instance / name).exists(), name)
            self.assertTrue((instance / "resourcepacks" / "keep.txt").exists())
            self.assertTrue((instance / "shaderpacks" / "keep.txt").exists())

    def test_zip_validation_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../outside.txt", "bad")
            with zipfile.ZipFile(archive) as zf:
                with self.assertRaises(ValueError):
                    launcher._safe_zip_targets(zf, Path(tmp) / "stage")

    def test_full_install_failure_leaves_live_mods_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            live_mod = instance / "mods" / "working.jar"
            live_mod.parent.mkdir(parents=True)
            live_mod.write_bytes(b"known-good")
            app_data = root / "app"

            def bad_download(destination, _progress, _status):
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(destination, "w") as zf:
                    zf.writestr("mods/new.jar", b"new")
                    zf.writestr("../escape.txt", b"bad")

            globals_for_instance = {
                "INSTANCE_DIR": instance,
                "APP_DATA_DIR": app_data,
                "MODPACK_VERSION_FILE": instance / ".modpack_version",
                "CONFIGPACK_MARKER_FILE": instance / ".configpack.json",
                "INSTALL_MARKER_FILE": instance / ".install_complete.json",
            }
            with (
                mock.patch.multiple(launcher, **globals_for_instance),
                mock.patch.object(
                    launcher, "install_modpack_delta", return_value=False
                ),
                mock.patch.object(
                    launcher, "download_modpack_archive", side_effect=bad_download
                ),
            ):
                with self.assertRaises(ValueError):
                    launcher.install_modpack(lambda _text: None, lambda _pct: None)

            self.assertEqual(live_mod.read_bytes(), b"known-good")
            self.assertFalse(
                (instance / launcher.MODPACK_TRANSACTION_DIR_NAME).exists()
            )
            self.assertFalse((root / "escape.txt").exists())


class WebUiReliabilityTests(unittest.TestCase):
    @staticmethod
    def _immediate_thread(target, daemon=True):
        del daemon
        return mock.Mock(start=target)

    def test_minimize_setting_keeps_launcher_visible_while_game_runs(self):
        api = webui.Api()
        window = mock.Mock()
        api._window = window
        process = mock.Mock(returncode=0)
        with (
            mock.patch.object(webui.L, "get_active_game_session", return_value=None),
            mock.patch.object(
                webui.L, "load_settings",
                return_value={
                    "memory_auto": False,
                    "memory_mb": 4096,
                    "minimize_on_launch": False,
                },
            ),
            mock.patch.object(webui.L, "update_settings"),
            mock.patch.object(webui.L, "launch_game", return_value=process),
            mock.patch.object(webui.L, "record_game_finished") as finished,
            mock.patch.object(
                webui.threading, "Thread", side_effect=self._immediate_thread
            ),
        ):
            result = api.play("Player")

        self.assertEqual(result, {"ok": True, "started": True})
        process.wait.assert_called_once_with()
        finished.assert_called_once_with(process)
        window.hide.assert_not_called()
        window.restore.assert_not_called()
        window.show.assert_not_called()

    def test_disabled_auto_updates_do_not_contact_release_sources(self):
        api = webui.Api()
        with (
            mock.patch.object(
                api, "_ui_settings", return_value={"auto_updates": False}
            ),
            mock.patch.object(webui.L, "check_for_launcher_update") as check,
        ):
            self.assertIsNone(api.check_update())
        check.assert_not_called()

    def test_offline_client_state_explicitly_allows_local_launch(self):
        api = webui.Api()
        messages = []
        api._js = messages.append
        marker = {**launcher._install_signature(), "version_id": "local-version"}
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            (instance / "config").mkdir()
            version_json = (
                instance / "versions" / "local-version" / "local-version.json"
            )
            version_json.parent.mkdir(parents=True)
            version_json.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(webui.L, "INSTANCE_DIR", instance),
                mock.patch.object(
                    webui.L, "get_local_modpack_version", return_value=12
                ),
                mock.patch.object(
                    webui.L, "get_modpack_version_status",
                    return_value={"version": 12, "online": False},
                ),
                mock.patch.object(webui.L, "_read_install_marker", return_value=marker),
                mock.patch.object(
                    webui.threading, "Thread", side_effect=self._immediate_thread
                ),
            ):
                result = api.refresh_client_state()

        payload = "\n".join(messages)
        self.assertEqual(result, {"ok": True, "started": True})
        self.assertIn('"state": "offline"', payload)
        self.assertIn('"can_launch": true', payload)
        self.assertIn('"offline": true', payload)

    def test_repair_returns_started_and_emits_explicit_completion(self):
        api = webui.Api()
        messages = []
        api._js = messages.append
        with (
            mock.patch.object(webui.L, "repair_installation"),
            mock.patch.object(
                webui.threading, "Thread", side_effect=self._immediate_thread
            ),
        ):
            result = api.repair()

        payload = "\n".join(messages)
        self.assertEqual(result, {"ok": True, "started": True})
        self.assertIn("onRepairState", payload)
        self.assertIn('"state": "complete"', payload)

    def test_repair_refuses_to_modify_files_while_minecraft_is_running(self):
        api = webui.Api()
        messages = []
        api._js = messages.append
        with (
            mock.patch.object(webui.L, "get_active_game_session", return_value={"pid": 99}),
            mock.patch.object(webui.L, "repair_installation") as repair,
        ):
            result = api.repair()
        self.assertFalse(result["ok"])
        self.assertIn("Закройте Minecraft", result["error"])
        repair.assert_not_called()
        self.assertIn('"state": "error"', "\n".join(messages))

    def test_repair_confirmation_is_rendered_inside_launcher(self):
        html = (
            Path(__file__).parents[1] / "ui" / "center-control-layouts.html"
        ).read_text(encoding="utf-8")
        self.assertIn('data-dialog-view="repair-confirm"', html)
        self.assertIn("data-confirm-repair", html)
        self.assertNotIn("window.confirm(", html)

    def test_catalog_install_returns_started_and_emits_terminal_state(self):
        api = webui.Api()
        messages = []
        api._js = messages.append
        configured = [{"slug": "test-shader", "name": "Test Shader"}]

        def install(_cfg, status):
            status("Downloading")

        with (
            mock.patch.dict(
                webui.L.CONFIG, {"RECOMMENDED_SHADER_PACKS": configured}
            ),
            mock.patch.object(
                webui.L, "install_recommended_shader_pack", side_effect=install
            ),
            mock.patch.object(webui.L, "list_shader_packs", return_value=[]),
            mock.patch.object(
                webui.threading, "Thread", side_effect=self._immediate_thread
            ),
        ):
            result = api.install_shader("test-shader")

        payload = "\n".join(messages)
        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        self.assertIn('"state": "installing"', payload)
        self.assertIn('"state": "done"', payload)


if __name__ == "__main__":
    unittest.main()
