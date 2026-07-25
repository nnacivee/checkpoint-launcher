import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import launcher


def _write_mod_jar(path: Path, mod_id: str, dependencies=()) -> Path:
    """Create a small but realistic NeoForge JAR accepted by the client gate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = (
        'modLoader="javafml"\n'
        'loaderVersion="[4,)"\n'
        'license="All Rights Reserved"\n'
        '[[mods]]\n'
        f'modId="{mod_id}"\n'
        'version="1.0.0"\n'
        f'displayName="{mod_id}"\n'
    )
    for dependency in dependencies:
        metadata += (
            f'[[dependencies.{mod_id}]]\n'
            f'modId="{dependency}"\n'
            'type="required"\n'
            'versionRange="[1,)"\n'
            'ordering="NONE"\n'
            'side="BOTH"\n'
        )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as jar:
        jar.writestr("META-INF/neoforge.mods.toml", metadata)
        # _valid_cached_jar intentionally rejects suspiciously tiny archives.
        jar.writestr("assets/test/payload.bin", bytes(range(256)) * 8)
    return path


class ClientGateTests(unittest.TestCase):
    def _gate_patches(
        self,
        instance: Path,
        app_data: Path,
        *,
        managed=(),
        preferred=(),
    ):
        return (
            mock.patch.object(launcher, "INSTANCE_DIR", instance),
            mock.patch.object(launcher, "APP_DATA_DIR", app_data),
            mock.patch.object(
                launcher, "_managed_mod_filenames",
                return_value={name.lower() for name in managed},
            ),
            mock.patch.object(
                launcher, "_preferred_mod_filenames",
                return_value={name.lower() for name in preferred},
            ),
            mock.patch.dict(
                launcher.CONFIG,
                {"EXTRA_CLIENT_MODS": [], "REMOVED_MODS": []},
                clear=False,
            ),
        )

    def test_valid_cached_jar_checks_zip_and_integrity_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = _write_mod_jar(Path(tmp) / "example.jar", "example")
            record = launcher._jar_cache_record(jar)

            self.assertTrue(launcher._valid_cached_jar(jar))
            self.assertTrue(launcher._valid_cached_jar(jar, record))

            jar.write_bytes(jar.read_bytes() + b"changed")
            self.assertTrue(launcher._valid_cached_jar(jar))
            self.assertFalse(launcher._valid_cached_jar(jar, record))

            broken = Path(tmp) / "broken.jar"
            broken.write_bytes(b"not a zip" * 200)
            self.assertFalse(launcher._valid_cached_jar(broken))

    def test_atomic_copy_replaces_target_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "cache" / "mod.jar"
            target = root / "instance" / "mods" / "mod.jar"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"new complete payload")
            target.write_bytes(b"old payload")

            launcher._atomic_copy_file(source, target)

            self.assertEqual(target.read_bytes(), b"new complete payload")
            self.assertEqual(
                list(target.parent.glob(f".{target.name}.*.tmp")), []
            )

    def test_corrupt_managed_jar_blocks_launch_and_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            broken = mods / "managed.jar"
            broken.write_bytes(b"broken" * 300)

            patches = self._gate_patches(
                instance, root / "app", managed={"managed.jar"}
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with self.assertRaises(RuntimeError):
                    launcher.validate_client_before_launch()

            self.assertFalse(broken.exists())

    def test_corrupt_unmanaged_jar_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            broken = mods / "unknown.jar"
            broken.write_bytes(b"broken" * 300)
            app_data = root / "app"

            patches = self._gate_patches(instance, app_data)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                launcher.validate_client_before_launch()

            self.assertFalse(broken.exists())
            self.assertTrue((app_data / "quarantine_mods" / broken.name).is_file())

    def test_duplicate_mod_id_keeps_single_preferred_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            mods = instance / "mods"
            preferred = _write_mod_jar(mods / "new.jar", "same_mod")
            stale = _write_mod_jar(mods / "old.jar", "same_mod")
            app_data = root / "app"

            patches = self._gate_patches(
                instance, app_data, preferred={"new.jar"}
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                launcher.validate_client_before_launch()

            self.assertTrue(preferred.is_file())
            self.assertFalse(stale.exists())
            self.assertTrue((app_data / "quarantine_mods" / stale.name).is_file())

    def test_two_managed_jars_with_same_mod_id_block_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            mods = instance / "mods"
            first = _write_mod_jar(mods / "managed-a.jar", "same_mod")
            second = _write_mod_jar(mods / "managed-b.jar", "same_mod")

            patches = self._gate_patches(
                instance,
                root / "app",
                managed={"managed-a.jar", "managed-b.jar"},
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with self.assertRaises(RuntimeError):
                    launcher.validate_client_before_launch()

            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_dependency_mod_ids_are_not_treated_as_installed_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            mods = instance / "mods"
            first = _write_mod_jar(
                mods / "first.jar", "first", dependencies=("minecraft", "create")
            )
            second = _write_mod_jar(
                mods / "second.jar", "second", dependencies=("minecraft", "create")
            )

            self.assertEqual(launcher._jar_mod_ids(first), {"first"})
            self.assertEqual(launcher._jar_mod_ids(second), {"second"})

            patches = self._gate_patches(instance, root / "app")
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                launcher.validate_client_before_launch()

            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_successful_gate_reports_100_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            _write_mod_jar(instance / "mods" / "healthy.jar", "healthy")
            progress = []

            patches = self._gate_patches(instance, root / "app")
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                launcher.validate_client_before_launch(
                    progress_cb=progress.append
                )

            self.assertTrue(progress)
            self.assertEqual(progress[-1], 100)


if __name__ == "__main__":
    unittest.main()
