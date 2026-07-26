import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import launcher


class RepairIntegrity16619Tests(unittest.TestCase):
    @staticmethod
    def _mod_manifest(version=13, count=20):
        files = []
        payloads = {}
        for index in range(count):
            name = "core-%02d.jar" % index
            payload = ("payload-%02d" % index).encode("ascii")
            payloads[name] = payload
            files.append({
                "path": "mods/" + name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        return {
            "version": version,
            "modsOnly": True,
            "files": files,
        }, payloads

    def test_missing_manifest_is_refetched_and_same_version_files_are_checked(self):
        manifest, payloads = self._mod_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            for name, payload in payloads.items():
                (mods / name).write_bytes(payload)
            (mods / "core-19.jar").write_bytes(b"damage-19")
            version_file = instance / ".modpack_version"
            version_file.write_text("13", encoding="utf-8")
            cache_file = root / "app" / "modpack_manifest.json"

            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    APP_DATA_DIR=root / "app",
                    MODPACK_VERSION_FILE=version_file,
                    MODPACK_MANIFEST_CACHE_FILE=cache_file,
                ),
                mock.patch.object(
                    launcher, "_fetch_modpack_manifest", return_value=manifest
                ) as fetch,
                mock.patch.object(launcher, "_sha_index_load", return_value={}),
                mock.patch.object(launcher, "_sha_index_save"),
                mock.patch.object(launcher, "load_settings", return_value={}),
                mock.patch.object(
                    launcher, "get_optional_mods_selection", return_value={}
                ),
                mock.patch.dict(
                    launcher.CONFIG,
                    {"REMOVED_MODS": [], "OPTIONAL_MODS": []},
                ),
            ):
                result = launcher.verify_modpack_integrity()

            fetch.assert_called_once_with()
            self.assertTrue(result["available"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["corrupt"], ["core-19.jar"])
            self.assertTrue(cache_file.is_file())
            self.assertEqual(
                json.loads(cache_file.read_text(encoding="utf-8"))["version"],
                13,
            )

    def test_unavailable_manifest_is_not_reported_as_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            version_file = instance / ".modpack_version"
            version_file.parent.mkdir(parents=True)
            version_file.write_text("13", encoding="utf-8")
            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    MODPACK_VERSION_FILE=version_file,
                    MODPACK_MANIFEST_CACHE_FILE=root / "missing.json",
                ),
                mock.patch.object(
                    launcher, "_fetch_modpack_manifest", return_value=None
                ),
            ):
                result = launcher.verify_modpack_integrity()

        self.assertFalse(result["available"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "manifest_unavailable")

    def test_repair_client_keeps_existing_system_and_player_files(self):
        manifest, _payloads = self._mod_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            for name in launcher.REPAIRABLE_FOLDERS + [
                "saves", "resourcepacks", "shaderpacks"
            ]:
                folder = instance / name
                folder.mkdir(parents=True)
                (folder / "keep.txt").write_text(name, encoding="utf-8")
            (instance / "options.txt").write_text(
                "lang:ru_ru\n", encoding="utf-8"
            )
            install_marker = instance / ".install_complete.json"
            install_marker.write_text("{}", encoding="utf-8")

            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    APP_DATA_DIR=root / "app",
                    INSTALL_MARKER_FILE=install_marker,
                ),
                mock.patch.object(launcher, "_check_installation_preconditions"),
                mock.patch.object(
                    launcher, "recover_interrupted_modpack_update"
                ),
                mock.patch.object(
                    launcher, "recover_interrupted_configpack_update"
                ),
                mock.patch.object(
                    launcher, "install_minecraft_and_modloader",
                    return_value="neoforge-test",
                ) as install_game,
                mock.patch.object(
                    launcher, "get_local_modpack_version", return_value=13
                ),
                mock.patch.object(
                    launcher, "get_remote_modpack_version", return_value=13
                ),
                mock.patch.object(
                    launcher,
                    "verify_modpack_integrity",
                    return_value={
                        "available": True,
                        "ok": True,
                        "missing": [],
                        "corrupt": [],
                    },
                ),
                mock.patch.object(launcher, "install_modpack_delta") as delta,
                mock.patch.object(launcher, "install_modpack") as full,
                mock.patch.object(
                    launcher, "install_extra_client_mods", return_value=[]
                ) as extras,
                mock.patch.object(launcher, "remove_blocked_mods"),
                mock.patch.object(launcher, "install_configpack") as config,
                mock.patch.object(
                    launcher, "install_minimal_ui_defaults_script"
                ) as minimal_ui,
                mock.patch.object(
                    launcher, "get_active_game_session", return_value=None
                ),
            ):
                result = launcher.repair_client()

            install_game.assert_called_once()
            delta.assert_not_called()
            full.assert_not_called()
            extras.assert_called_once()
            config.assert_called_once()
            minimal_ui.assert_called_once()
            self.assertTrue(config.call_args.kwargs["force_verify"])
            self.assertTrue(
                install_game.call_args.kwargs["force"]
            )
            self.assertTrue(result["minecraft_checked"])
            self.assertTrue(result["modpack_checked"])
            self.assertTrue(result["extras_checked"])
            for name in launcher.REPAIRABLE_FOLDERS + [
                "saves", "resourcepacks", "shaderpacks"
            ]:
                self.assertTrue((instance / name / "keep.txt").is_file(), name)
            self.assertEqual(
                (instance / "options.txt").read_text(encoding="utf-8"),
                "lang:ru_ru\n",
            )

    def test_backend_repair_refuses_before_any_file_mutation(self):
        with (
            mock.patch.object(
                launcher, "get_active_game_session", return_value={"pid": 42}
            ),
            mock.patch.object(
                launcher, "recover_interrupted_modpack_update"
            ) as recover_modpack,
            mock.patch.object(
                launcher, "recover_interrupted_configpack_update"
            ) as recover_configpack,
            mock.patch.object(
                launcher, "create_player_settings_snapshot"
            ) as snapshot,
            mock.patch.object(
                launcher, "prepare_or_repair_client"
            ) as prepare,
        ):
            with self.assertRaises(launcher.GameAlreadyRunning):
                launcher.repair_client()
        recover_modpack.assert_not_called()
        recover_configpack.assert_not_called()
        snapshot.assert_not_called()
        prepare.assert_not_called()

    def test_failed_repair_restores_snapshot_and_keeps_map_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            options = instance / "options.txt"
            ui = instance / "config" / "betterf3.toml"
            map_data = instance / "journeymap" / "data" / "map.bin"
            ftb_data = instance / "local" / "ftbchunks" / "data" / "map.dat"
            for path, payload in (
                (options, b"guiScale:2\n"),
                (ui, b"player-ui"),
                (map_data, b"journeymap"),
                (ftb_data, b"ftb-map"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

            def fail_after_mutation(*_args, **_kwargs):
                options.write_bytes(b"damaged")
                ui.write_bytes(b"damaged")
                raise RuntimeError("simulated repair failure")

            with (
                mock.patch.multiple(
                    launcher, INSTANCE_DIR=instance, APP_DATA_DIR=app
                ),
                mock.patch.object(
                    launcher, "get_active_game_session", return_value=None
                ),
                mock.patch.object(
                    launcher, "recover_interrupted_modpack_update"
                ),
                mock.patch.object(
                    launcher, "recover_interrupted_configpack_update"
                ),
                mock.patch.object(
                    launcher,
                    "prepare_or_repair_client",
                    side_effect=fail_after_mutation,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "simulated repair failure"
                ):
                    launcher.repair_client()

            self.assertEqual(options.read_bytes(), b"guiScale:2\n")
            self.assertEqual(ui.read_bytes(), b"player-ui")
            self.assertEqual(map_data.read_bytes(), b"journeymap")
            self.assertEqual(ftb_data.read_bytes(), b"ftb-map")
            snapshots = [
                path for path in (app / "player_settings_snapshots").iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
            self.assertEqual(len(snapshots), 1)
            manifest = json.loads(
                (snapshots[0] / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["entries"]["options.txt"]["state"], "file"
            )
            self.assertEqual(
                manifest["entries"]["config/betterf3.toml"]["state"], "file"
            )

    def test_snapshot_exactly_restores_empty_and_absent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            empty = instance / "options.txt"
            created_later = instance / "servers.dat"
            empty.parent.mkdir(parents=True)
            empty.write_bytes(b"")
            with mock.patch.multiple(
                launcher, INSTANCE_DIR=instance, APP_DATA_DIR=app
            ):
                snapshot = launcher.create_player_settings_snapshot("test")
                empty.write_bytes(b"damaged")
                created_later.write_bytes(b"new")
                launcher.restore_player_settings_snapshot(snapshot)
            self.assertTrue(empty.is_file())
            self.assertEqual(empty.read_bytes(), b"")
            self.assertFalse(created_later.exists())

    def test_snapshot_restores_file_over_wrong_type_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            options = instance / "options.txt"
            options.parent.mkdir(parents=True)
            options.write_bytes(b"known-good")
            with mock.patch.multiple(
                launcher, INSTANCE_DIR=instance, APP_DATA_DIR=app
            ):
                snapshot = launcher.create_player_settings_snapshot("test")
                options.unlink()
                options.mkdir()
                (options / "junk").write_bytes(b"junk")
                launcher.restore_player_settings_snapshot(snapshot)
            self.assertTrue(options.is_file())
            self.assertEqual(options.read_bytes(), b"known-good")

    def test_snapshot_aborts_on_reparse_protected_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            options = instance / "options.txt"
            options.parent.mkdir(parents=True)
            options.write_bytes(b"settings")
            original = launcher._path_is_reparse_point

            def mark_options_as_reparse(path):
                return Path(path) == options or original(path)

            with (
                mock.patch.multiple(
                    launcher, INSTANCE_DIR=instance, APP_DATA_DIR=app
                ),
                mock.patch.object(
                    launcher,
                    "_path_is_reparse_point",
                    side_effect=mark_options_as_reparse,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "резервную копию"
                ):
                    launcher.create_player_settings_snapshot("test")

    def test_corrupt_snapshot_is_rejected_before_any_live_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            options = instance / "options.txt"
            ui = instance / "config" / "betterf3.toml"
            options.parent.mkdir(parents=True)
            ui.parent.mkdir(parents=True)
            options.write_bytes(b"known-options")
            ui.write_bytes(b"known-ui")
            with mock.patch.multiple(
                launcher, INSTANCE_DIR=instance, APP_DATA_DIR=app
            ):
                snapshot = launcher.create_player_settings_snapshot("test")
                options.write_bytes(b"live-options")
                ui.write_bytes(b"live-ui")
                (snapshot / "files" / "config" / "betterf3.toml").write_bytes(
                    b"corrupt"
                )
                with self.assertRaises(ValueError):
                    launcher.restore_player_settings_snapshot(snapshot)
            self.assertEqual(options.read_bytes(), b"live-options")
            self.assertEqual(ui.read_bytes(), b"live-ui")

    def test_snapshot_retention_keeps_current_five_and_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            options = instance / "options.txt"
            options.parent.mkdir(parents=True)
            options.write_bytes(b"settings")
            unrelated = app / "player_settings_snapshots" / "do-not-delete"
            unrelated.mkdir(parents=True)
            (unrelated / "note.txt").write_text("user", encoding="utf-8")
            snapshots = []
            with mock.patch.multiple(
                launcher, INSTANCE_DIR=instance, APP_DATA_DIR=app
            ):
                for _index in range(7):
                    snapshots.append(
                        launcher.create_player_settings_snapshot("test")
                    )
            managed = [
                path
                for path in (app / "player_settings_snapshots").iterdir()
                if launcher._managed_snapshot_sort_key(path) is not None
            ]
            self.assertEqual(len(managed), 5)
            self.assertTrue(snapshots[-1].is_dir())
            self.assertTrue(unrelated.is_dir())

    def test_repair_recovers_interrupted_transaction_before_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            rel = "config/betterf3.toml"
            live = instance / rel
            transaction = (
                instance / launcher.CONFIGPACK_TRANSACTION_DIR_NAME
            )
            backup = transaction / "backup" / rel
            stage = transaction / "stage"
            live.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            stage.mkdir(parents=True)
            live.write_bytes(b"partial")
            backup.write_bytes(b"known-good")
            (transaction / "journal.json").write_text(
                json.dumps({
                    "version": 1,
                    "phase": "committing",
                    "targets": [rel],
                    "existed": [rel],
                }),
                encoding="utf-8",
            )

            def fail_after_recovery(*_args, **_kwargs):
                self.assertEqual(live.read_bytes(), b"known-good")
                live.write_bytes(b"later-damage")
                raise RuntimeError("later repair failure")

            with (
                mock.patch.multiple(
                    launcher, INSTANCE_DIR=instance, APP_DATA_DIR=app
                ),
                mock.patch.object(
                    launcher, "get_active_game_session", return_value=None
                ),
                mock.patch.object(
                    launcher, "recover_interrupted_modpack_update"
                ),
                mock.patch.object(
                    launcher,
                    "prepare_or_repair_client",
                    side_effect=fail_after_recovery,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "later repair failure"
                ):
                    launcher.repair_client()

            self.assertEqual(live.read_bytes(), b"known-good")
            self.assertFalse(transaction.exists())

    def test_snapshot_failure_aborts_repair_before_install_changes(self):
        with (
            mock.patch.object(
                launcher, "get_active_game_session", return_value=None
            ),
            mock.patch.object(
                launcher, "recover_interrupted_modpack_update"
            ),
            mock.patch.object(
                launcher, "recover_interrupted_configpack_update"
            ),
            mock.patch.object(
                launcher,
                "create_player_settings_snapshot",
                side_effect=RuntimeError("backup failed"),
            ),
            mock.patch.object(
                launcher, "prepare_or_repair_client"
            ) as prepare,
        ):
            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                launcher.repair_client()
        prepare.assert_not_called()

    def test_latest_backup_restores_nested_ui_settings_when_instance_is_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            backup_root = root / "latest-backup"
            rel = "journeymap/config/6.0/journeymap.minimap.config"
            source = instance / rel
            source.parent.mkdir(parents=True)
            source.write_bytes(b"player-map-layout")
            with mock.patch.multiple(
                launcher,
                INSTANCE_DIR=instance,
                PLAYER_SETTINGS_BACKUP_DIR=backup_root,
            ):
                launcher.backup_player_settings()
                source.unlink()
                launcher.restore_player_settings()
            self.assertEqual(source.read_bytes(), b"player-map-layout")

    def test_preflight_reports_insufficient_space_before_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=root / "instance",
                    APP_DATA_DIR=root / "app",
                ),
                mock.patch.object(
                    launcher.shutil, "disk_usage", return_value=(100, 100, 0)
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Недостаточно места"
                ):
                    launcher._check_installation_preconditions(1)

    def test_preflight_reports_unwritable_install_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=root / "instance",
                    APP_DATA_DIR=root / "app",
                ),
                mock.patch.object(
                    launcher.tempfile,
                    "NamedTemporaryFile",
                    side_effect=PermissionError("blocked"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "Нет доступа"):
                    launcher._check_installation_preconditions()

    def test_preflight_checks_app_download_and_instance_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            instance = root / "instance"
            seen = []

            def splitdrive(value):
                text = str(value)
                return (
                    ("C:", text)
                    if "app" in text else ("D:", text)
                )

            def disk_usage(folder):
                seen.append(Path(folder))
                free = 64 * 1024 * 1024 if Path(folder) == app else 10**12
                return (10**12, 0, free)

            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    APP_DATA_DIR=app,
                ),
                mock.patch.object(
                    launcher.os.path,
                    "splitdrive",
                    side_effect=splitdrive,
                ),
                mock.patch.object(
                    launcher.shutil,
                    "disk_usage",
                    side_effect=disk_usage,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Недостаточно места"
                ):
                    launcher._check_installation_preconditions(
                        1, app_required_bytes=512 * 1024 * 1024
                    )

            self.assertIn(app, seen)

    def test_configpack_hash_manifest_excludes_player_mutable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            immutable = (
                stage / "kubejs" / "server_scripts" / "era_gates.js"
            )
            mutable = stage / "config" / "fml.toml"
            quest_art = (
                stage
                / "resourcepacks"
                / "checkpoint_quest_art"
                / "pack.mcmeta"
            )
            loading_variant = (
                stage
                / "config"
                / "simple-custom-early-loading"
                / "variants"
                / "bar_01.apng"
            )
            changing_bar = (
                stage
                / "config"
                / "simple-custom-early-loading"
                / "bar.apng"
            )
            immutable.parent.mkdir(parents=True)
            mutable.parent.mkdir(parents=True)
            quest_art.parent.mkdir(parents=True)
            loading_variant.parent.mkdir(parents=True)
            immutable.write_text("immutable", encoding="utf-8")
            mutable.write_text("player value", encoding="utf-8")
            quest_art.write_text("art", encoding="utf-8")
            loading_variant.write_bytes(b"variant")
            changing_bar.write_bytes(b"selected variant")

            manifest = launcher._build_configpack_file_manifest(
                stage,
                [
                    "kubejs/server_scripts/era_gates.js",
                    "config/fml.toml",
                    "resourcepacks/checkpoint_quest_art/pack.mcmeta",
                    "config/simple-custom-early-loading/variants/bar_01.apng",
                    "config/simple-custom-early-loading/bar.apng",
                ],
            )

            self.assertEqual(
                [item["path"] for item in manifest],
                [
                    "config/simple-custom-early-loading/variants/bar_01.apng",
                    "kubejs/server_scripts/era_gates.js",
                    "resourcepacks/checkpoint_quest_art/pack.mcmeta",
                ],
            )

    def test_same_version_repair_only_replaces_damaged_immutable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app = root / "app"
            mutable = instance / "config" / "fml.toml"
            immutable = (
                instance / "kubejs" / "server_scripts" / "era_gates.js"
            )
            mutable.parent.mkdir(parents=True)
            immutable.parent.mkdir(parents=True)
            mutable.write_text("player value", encoding="utf-8")
            immutable.write_text("damaged", encoding="utf-8")

            marker = instance / ".configpack.json"
            marker.write_text(
                json.dumps({
                    "version": 48,
                    "owns": ["config", "kubejs"],
                    "verify": ["config", "kubejs"],
                    "files": [{
                        "path": "kubejs/server_scripts/era_gates.js",
                        "size": len("verified script"),
                        "sha256": hashlib.sha256(
                            b"verified script"
                        ).hexdigest(),
                    }],
                }),
                encoding="utf-8",
            )
            app.mkdir(parents=True)
            archive = app / "configpack_download.zip"
            with zipfile.ZipFile(
                archive, "w", zipfile.ZIP_DEFLATED
            ) as zf:
                zf.writestr(
                    "configpack.json",
                    json.dumps({
                        "version": 48,
                        "owns": ["config", "kubejs"],
                    }),
                )
                zf.writestr("config/fml.toml", "pack default")
                zf.writestr(
                    "kubejs/server_scripts/era_gates.js",
                    "verified script",
                )

            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    APP_DATA_DIR=app,
                    CONFIGPACK_MARKER_FILE=marker,
                ),
                mock.patch.dict(
                    launcher.CONFIG,
                    {
                        "CONFIGPACK_URL": "https://example.test/config.zip",
                        "CONFIGPACK_MIRROR_URL": "",
                    },
                ),
                mock.patch.object(
                    launcher, "get_remote_configpack_version",
                    return_value=48,
                ),
                mock.patch.object(
                    launcher, "_check_installation_preconditions"
                ),
                mock.patch.object(
                    launcher, "fetch_artifact_sha256",
                    return_value="a" * 64,
                ),
                mock.patch.object(
                    launcher, "verify_file_sha256", return_value=True
                ),
                mock.patch.object(
                    launcher, "download_with_mirror"
                ) as download,
            ):
                launcher.install_configpack(force_verify=True)

            download.assert_not_called()
            self.assertEqual(
                mutable.read_text(encoding="utf-8"), "player value"
            )
            self.assertEqual(
                immutable.read_text(encoding="utf-8"),
                "verified script",
            )
            file_paths = {
                item["path"]
                for item in json.loads(
                    marker.read_text(encoding="utf-8")
                )["files"]
            }
            self.assertIn(
                "kubejs/server_scripts/era_gates.js", file_paths
            )
            self.assertNotIn("config/fml.toml", file_paths)

    def test_configpack_sha_detects_same_size_corruption_and_old_marker_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            payload = instance / "config" / "industrial-horizon" / "ui.json"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"good")
            marker = instance / ".configpack.json"
            digest = hashlib.sha256(b"good").hexdigest()
            marker.write_text(
                json.dumps({
                    "version": 48,
                    "owns": ["config/industrial-horizon"],
                    "verify": ["config/industrial-horizon"],
                    "files": [{
                        "path": "config/industrial-horizon/ui.json",
                        "size": 4,
                        "sha256": digest,
                    }],
                }),
                encoding="utf-8",
            )
            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    CONFIGPACK_MARKER_FILE=marker,
                ),
                mock.patch.object(
                    launcher, "get_remote_configpack_version", return_value=48
                ),
                mock.patch.dict(
                    launcher.CONFIG, {"CONFIGPACK_URL": "https://example.test"}
                ),
            ):
                self.assertFalse(launcher.configpack_needs_install())
                payload.write_bytes(b"evil")
                self.assertTrue(launcher.configpack_needs_install())

                marker.write_text(
                    json.dumps({
                        "version": 48,
                        "owns": ["config/industrial-horizon"],
                        "verify": ["config/industrial-horizon"],
                    }),
                    encoding="utf-8",
                )
                self.assertFalse(launcher.configpack_needs_install())

    def test_newer_configpack_is_never_downgraded_to_old_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            marker = instance / ".configpack.json"
            marker.write_text(
                json.dumps({
                    "version": 50,
                    "owns": [],
                    "verify": [],
                }),
                encoding="utf-8",
            )
            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    CONFIGPACK_MARKER_FILE=marker,
                ),
                mock.patch.object(
                    launcher, "get_remote_configpack_version", return_value=49
                ),
                mock.patch.object(launcher, "runtime_log") as runtime_log,
                mock.patch.dict(
                    launcher.CONFIG, {"CONFIGPACK_URL": "https://example.test"}
                ),
            ):
                self.assertFalse(launcher.configpack_needs_install())
        self.assertTrue(runtime_log.called)

    def test_forced_repair_cannot_downgrade_newer_configpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            marker = instance / ".configpack.json"
            marker.write_text(
                json.dumps({"version": 50, "owns": [], "verify": []}),
                encoding="utf-8",
            )
            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    CONFIGPACK_MARKER_FILE=marker,
                ),
                mock.patch.object(
                    launcher, "get_remote_configpack_version", return_value=49
                ),
                mock.patch.object(launcher, "runtime_log"),
                mock.patch.object(
                    launcher, "download_with_mirror"
                ) as download,
            ):
                launcher.install_configpack(force_verify=True)
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
