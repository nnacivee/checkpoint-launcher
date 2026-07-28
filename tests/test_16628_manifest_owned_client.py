import importlib.util
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "launcher_16628", ROOT / "launcher.py"
)
WORKFLOW = (
    ROOT / ".github" / "workflows" / "build.yml"
).read_text(encoding="utf-8")
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


class ManifestOwnedClientReleaseTests(unittest.TestCase):
    def test_release_and_modpack_fallback_are_current(self):
        self.assertEqual(launcher.CONFIG["LAUNCHER_VERSION"], "1.66.28")
        self.assertEqual(
            launcher.CONFIG["LAUNCHER_CHANGELOG"][0]["version"], "1.66.28"
        )
        self.assertEqual(launcher.CONFIG["MODPACK_VERSION"], 15)

    def test_manifest_is_the_only_active_jar_source(self):
        self.assertEqual(launcher.CONFIG["EXTRA_CLIENT_MODS"], [])
        self.assertEqual(launcher.CONFIG["OPTIONAL_MODS"], [])
        self.assertFalse(launcher.CONFIG["SET_GAME_WINDOW_ICON"])
        self.assertEqual(launcher.CONFIG["MOD_SHOWCASE"], {})

    def test_required_xaero_world_map_is_not_blocked(self):
        blocked = [
            str(pattern).lower()
            for pattern in launcher.CONFIG["REMOVED_MODS"]
        ]
        self.assertFalse(
            any("xaeroworldmap" in pattern for pattern in blocked), blocked
        )

    def test_retired_ui_settings_are_not_backed_up_or_seeded(self):
        protected = {
            str(path).replace("\\", "/").lower()
            for path in launcher.PLAYER_SETTINGS_FILES
        }
        seeded = {
            str(path).replace("\\", "/").lower()
            for path in launcher.CONFIGPACK_SEED_ONLY_FILES
        }
        for paths in (protected, seeded):
            self.assertFalse(
                any("journeymap" in path for path in paths), paths
            )
            self.assertFalse(
                any("inventoryhud" in path for path in paths), paths
            )

    def test_normal_launch_no_longer_runs_retired_installers(self):
        launch_source = inspect.getsource(launcher.launch_game)
        repair_source = inspect.getsource(launcher.prepare_or_repair_client)
        for retired in (
            "install_minimal_ui_defaults_script(",
            "install_ultimine_sticky(",
            "fix_early_loading_provider(",
            "select_loading_bar_variant(",
        ):
            self.assertNotIn(retired, launch_source)
            self.assertNotIn(retired, repair_source)

    def test_disabled_jar_writers_do_not_touch_mods(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "APP_DATA_DIR", root / "app"),
                mock.patch.object(
                    launcher, "_find_modrinth_download"
                ) as find_download,
                mock.patch.object(launcher, "download_file") as download,
            ):
                self.assertEqual(launcher.install_extra_client_mods(), [])
                launcher.install_game_window_icon()

            find_download.assert_not_called()
            download.assert_not_called()
            self.assertEqual(
                list((instance / "mods").glob("*.jar"))
                if (instance / "mods").is_dir()
                else [],
                [],
            )

    def test_journeymap_has_no_launcher_keybind_seed(self):
        seeds = repr(launcher.DEFAULT_KEYBIND_SEEDS).lower()
        conflict_source = inspect.getsource(
            launcher.fix_key_conflicts_once
        ).lower()
        self.assertNotIn("journeymap", seeds)
        self.assertNotIn("journeymap", conflict_source)

    def test_pinned_server_is_preserved(self):
        self.assertEqual(
            launcher.CONFIG["PINNED_SERVER"],
            {
                "name": "Industrial Horizon",
                "ip": "95.216.30.64:25760",
            },
        )

    def test_release_publishes_the_versioned_installer_before_marker(self):
        self.assertIn(
            '$versionedInstaller = "CheckpointSetup-$version.exe"',
            WORKFLOW,
        )
        upload = WORKFLOW.split(
            'lftp -c "', 1
        )[1].split('"', 1)[0]
        self.assertIn("put deploy/$VERSIONED", upload)
        self.assertIn("put deploy/$VERSIONED.sha256", upload)
        self.assertLess(
            upload.index("put deploy/$VERSIONED"),
            upload.index("put deploy/launcher_version.txt"),
        )
        self.assertIn(
            'BASE="https://industrialhorizon.b-cdn.net/stable"',
            WORKFLOW,
        )


if __name__ == "__main__":
    unittest.main()
