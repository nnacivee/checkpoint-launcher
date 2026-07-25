import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import launcher


class ConfigpackTransactionTests(unittest.TestCase):
    REMOTE_VERSION = 48

    @staticmethod
    def _write_marker(instance: Path, version: int, owns) -> Path:
        marker = instance / ".configpack.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({
                "version": version,
                "owns": list(owns),
                "verify": list(owns),
            }),
            encoding="utf-8",
        )
        return marker

    @staticmethod
    def _make_archive(
        archive: Path,
        *,
        version: int = REMOTE_VERSION,
        owns=("config/industrial-horizon",),
        payload=None,
    ) -> str:
        payload = payload or {
            "config/industrial-horizon/settings.json": b'{"new": true}\n',
        }
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "configpack.json",
                json.dumps({"version": version, "owns": list(owns)}),
            )
            for name, data in payload.items():
                zf.writestr(name, data)
        return hashlib.sha256(archive.read_bytes()).hexdigest()

    @staticmethod
    def _globals(instance: Path, app_data: Path):
        return mock.patch.multiple(
            launcher,
            INSTANCE_DIR=instance,
            APP_DATA_DIR=app_data,
            CONFIGPACK_MARKER_FILE=instance / ".configpack.json",
        )

    def _install_from_local_archive(
        self,
        instance: Path,
        app_data: Path,
        digest: str,
        *,
        extra_patches=(),
    ):
        statuses = []
        download_patcher = mock.patch.object(
            launcher, "download_with_mirror"
        )
        patches = [
            self._globals(instance, app_data),
            mock.patch.dict(
                launcher.CONFIG,
                {
                    "CONFIGPACK_URL": "https://unit.test/configpack.zip",
                    "CONFIGPACK_MIRROR_URL": "",
                },
            ),
            mock.patch.object(
                launcher,
                "get_remote_configpack_version",
                return_value=self.REMOTE_VERSION,
            ),
            mock.patch.object(
                launcher, "fetch_artifact_sha256", return_value=digest
            ),
            download_patcher,
        ]
        patches.extend(extra_patches)
        entered = []
        download = None
        try:
            for patcher in patches:
                entered.append(patcher)
                active = patcher.__enter__()
                if patcher is download_patcher:
                    download = active
            launcher.install_configpack(statuses.append, lambda _pct: None)
            download.assert_not_called()
        finally:
            for patcher in reversed(entered):
                patcher.__exit__(None, None, None)
        return statuses

    def test_successful_install_atomically_swaps_owned_paths_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app_data = root / "app"
            old_root = instance / "config" / "industrial-horizon"
            old_root.mkdir(parents=True)
            (old_root / "old.json").write_text("old", encoding="utf-8")
            old_marker = self._write_marker(
                instance, 47, ["config/industrial-horizon"]
            )
            archive = app_data / "configpack_download.zip"
            digest = self._make_archive(
                archive,
                owns=(
                    "config/industrial-horizon",
                    "mods/ih-managed.jar",
                ),
                payload={
                    "config/industrial-horizon/settings.json": b"new-settings",
                    "mods/ih-managed.jar": b"new-mod",
                },
            )

            self._install_from_local_archive(
                instance, app_data, digest
            )

            self.assertFalse((old_root / "old.json").exists())
            self.assertEqual(
                (old_root / "settings.json").read_bytes(), b"new-settings"
            )
            self.assertEqual(
                (instance / "mods" / "ih-managed.jar").read_bytes(), b"new-mod"
            )
            marker = json.loads(old_marker.read_text(encoding="utf-8"))
            self.assertEqual(marker["version"], self.REMOTE_VERSION)
            self.assertEqual(marker["archive_sha256"], digest)
            self.assertFalse(
                (instance / launcher.CONFIGPACK_TRANSACTION_DIR_NAME).exists()
            )
            self.assertFalse(archive.exists())

    def test_extraction_error_keeps_old_live_files_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app_data = root / "app"
            old_file = (
                instance / "config" / "industrial-horizon" / "working.json"
            )
            old_file.parent.mkdir(parents=True)
            old_file.write_text("known-good", encoding="utf-8")
            marker = self._write_marker(
                instance, 47, ["config/industrial-horizon"]
            )
            archive = app_data / "configpack_download.zip"
            digest = self._make_archive(archive)

            extract_error = mock.patch.object(
                launcher.zipfile.ZipFile,
                "extract",
                autospec=True,
                side_effect=OSError("simulated extraction failure"),
            )
            self._install_from_local_archive(
                instance,
                app_data,
                digest,
                extra_patches=(extract_error,),
            )

            self.assertEqual(old_file.read_text(encoding="utf-8"), "known-good")
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8"))["version"], 47
            )
            self.assertFalse(
                (instance / launcher.CONFIGPACK_TRANSACTION_DIR_NAME).exists()
            )

    def test_persistent_commit_permission_error_is_bounded_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app_data = root / "app"
            owned = instance / "config" / "industrial-horizon"
            owned.mkdir(parents=True)
            old_file = owned / "working.json"
            old_file.write_text("known-good", encoding="utf-8")
            marker = self._write_marker(
                instance, 47, ["config/industrial-horizon"]
            )
            archive = app_data / "configpack_download.zip"
            digest = self._make_archive(archive)

            real_replace = os.replace
            failed_attempts = 0

            def fail_staged_swap(src, dst):
                nonlocal failed_attempts
                source = Path(src)
                destination = Path(dst)
                if (
                    launcher.CONFIGPACK_TRANSACTION_DIR_NAME in source.parts
                    and "stage" in source.parts
                    and destination == owned
                ):
                    failed_attempts += 1
                    raise PermissionError("persistent simulated antivirus lock")
                return real_replace(src, dst)

            replace_error = mock.patch.object(
                launcher.os, "replace", side_effect=fail_staged_swap
            )
            no_wait = mock.patch.object(launcher.time, "sleep")
            self._install_from_local_archive(
                instance,
                app_data,
                digest,
                extra_patches=(replace_error, no_wait),
            )

            self.assertGreaterEqual(failed_attempts, 2)
            self.assertLessEqual(failed_attempts, 10)
            self.assertEqual(old_file.read_text(encoding="utf-8"), "known-good")
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8"))["version"], 47
            )
            self.assertFalse(
                (instance / launcher.CONFIGPACK_TRANSACTION_DIR_NAME).exists()
            )

    def test_commit_retries_brief_permission_error_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            target = instance / "config" / "industrial-horizon"
            target.mkdir(parents=True)
            (target / "old.json").write_text("old", encoding="utf-8")

            with mock.patch.object(launcher, "INSTANCE_DIR", instance):
                transaction, stage_root = (
                    launcher._begin_configpack_transaction()
                )
                staged = stage_root / "config" / "industrial-horizon"
                staged.mkdir(parents=True)
                (staged / "new.json").write_text("new", encoding="utf-8")

                real_replace = os.replace
                staged_attempts = 0

                def briefly_locked(src, dst):
                    nonlocal staged_attempts
                    if Path(src) == staged and Path(dst) == target:
                        staged_attempts += 1
                        if staged_attempts <= 2:
                            raise PermissionError(
                                "brief simulated antivirus lock"
                            )
                    return real_replace(src, dst)

                with (
                    mock.patch.object(
                        launcher.os, "replace", side_effect=briefly_locked
                    ),
                    mock.patch.object(launcher.time, "sleep"),
                ):
                    launcher._commit_configpack_paths(
                        transaction, ["config/industrial-horizon"]
                    )

            self.assertEqual(staged_attempts, 3)
            self.assertFalse((target / "old.json").exists())
            self.assertEqual(
                (target / "new.json").read_text(encoding="utf-8"), "new"
            )
            self.assertFalse(transaction.exists())

    def test_recovery_from_committing_restores_backups_and_removes_new_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            transaction = (
                instance / launcher.CONFIGPACK_TRANSACTION_DIR_NAME
            )
            backup_root = transaction / "backup"
            stage_root = transaction / "stage"
            stage_root.mkdir(parents=True)

            live_owned = instance / "config" / "industrial-horizon"
            live_owned.mkdir(parents=True)
            (live_owned / "partial.json").write_text(
                "partial", encoding="utf-8"
            )
            backup_owned = (
                backup_root / "config" / "industrial-horizon"
            )
            backup_owned.mkdir(parents=True)
            (backup_owned / "working.json").write_text(
                "known-good", encoding="utf-8"
            )

            new_only = instance / "mods" / "new-only.jar"
            new_only.parent.mkdir(parents=True)
            new_only.write_bytes(b"partial-new")

            old_marker_data = {
                "version": 47,
                "owns": ["config/industrial-horizon"],
                "verify": ["config/industrial-horizon"],
            }
            new_marker_data = {
                "version": self.REMOTE_VERSION,
                "owns": ["config/industrial-horizon", "mods/new-only.jar"],
            }
            (instance / ".configpack.json").write_text(
                json.dumps(new_marker_data), encoding="utf-8"
            )
            (backup_root / ".configpack.json").parent.mkdir(
                parents=True, exist_ok=True
            )
            (backup_root / ".configpack.json").write_text(
                json.dumps(old_marker_data), encoding="utf-8"
            )
            (transaction / "journal.json").write_text(
                json.dumps({
                    "version": 1,
                    "phase": "committing",
                    "targets": [
                        "config/industrial-horizon",
                        "mods/new-only.jar",
                        ".configpack.json",
                    ],
                    "existed": [
                        "config/industrial-horizon",
                        ".configpack.json",
                    ],
                }),
                encoding="utf-8",
            )

            with mock.patch.object(launcher, "INSTANCE_DIR", instance):
                self.assertTrue(
                    launcher.recover_interrupted_configpack_update()
                )

            self.assertFalse((live_owned / "partial.json").exists())
            self.assertEqual(
                (live_owned / "working.json").read_text(encoding="utf-8"),
                "known-good",
            )
            self.assertFalse(new_only.exists())
            marker = json.loads(
                (instance / ".configpack.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker, old_marker_data)
            self.assertFalse(transaction.exists())

    def test_recovery_retries_brief_permission_error_then_restores_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            transaction = (
                instance / launcher.CONFIGPACK_TRANSACTION_DIR_NAME
            )
            backup = (
                transaction
                / "backup"
                / "config"
                / "industrial-horizon"
            )
            backup.mkdir(parents=True)
            (backup / "working.json").write_text(
                "known-good", encoding="utf-8"
            )
            (transaction / "stage").mkdir()

            target = instance / "config" / "industrial-horizon"
            target.mkdir(parents=True)
            (target / "partial.json").write_text("partial", encoding="utf-8")
            (transaction / "journal.json").write_text(
                json.dumps({
                    "version": 1,
                    "phase": "committing",
                    "targets": ["config/industrial-horizon"],
                    "existed": ["config/industrial-horizon"],
                }),
                encoding="utf-8",
            )

            real_replace = os.replace
            recovery_attempts = 0

            def briefly_locked(src, dst):
                nonlocal recovery_attempts
                if Path(src) == backup and Path(dst) == target:
                    recovery_attempts += 1
                    if recovery_attempts <= 2:
                        raise PermissionError(
                            "brief simulated antivirus lock"
                        )
                return real_replace(src, dst)

            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(
                    launcher.os, "replace", side_effect=briefly_locked
                ),
                mock.patch.object(launcher.time, "sleep"),
            ):
                self.assertTrue(
                    launcher.recover_interrupted_configpack_update()
                )

            self.assertEqual(recovery_attempts, 3)
            self.assertFalse((target / "partial.json").exists())
            self.assertEqual(
                (target / "working.json").read_text(encoding="utf-8"),
                "known-good",
            )
            self.assertFalse(transaction.exists())

    def test_embedded_version_mismatch_does_not_touch_live_pack_or_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app_data = root / "app"
            old_file = (
                instance / "config" / "industrial-horizon" / "working.json"
            )
            old_file.parent.mkdir(parents=True)
            old_file.write_text("known-good", encoding="utf-8")
            marker = self._write_marker(
                instance, 47, ["config/industrial-horizon"]
            )
            archive = app_data / "configpack_download.zip"
            digest = self._make_archive(
                archive, version=self.REMOTE_VERSION + 1
            )

            self._install_from_local_archive(
                instance,
                app_data,
                digest,
            )

            self.assertEqual(old_file.read_text(encoding="utf-8"), "known-good")
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8"))["version"], 47
            )
            self.assertFalse(
                (instance / launcher.CONFIGPACK_TRANSACTION_DIR_NAME).exists()
            )

    def test_overlapping_and_unsafe_owned_paths_are_rejected(self):
        invalid_sets = (
            ["config", "config/industrial-horizon"],
            ["config/industrial-horizon", "config"],
            ["../outside"],
            ["/absolute/path"],
            ["C:/Windows/System32"],
            ["config/./../outside"],
            [""],
        )
        for owns in invalid_sets:
            with self.subTest(owns=owns):
                with self.assertRaises(ValueError):
                    launcher._normalise_configpack_paths(owns)


if __name__ == "__main__":
    unittest.main()
