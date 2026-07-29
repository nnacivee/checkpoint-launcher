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
REPAIR_WORKFLOW = (
    ROOT / ".github" / "workflows" / "repair-launcher-mirror.yml"
).read_text(encoding="utf-8")
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


class ManifestOwnedClientReleaseTests(unittest.TestCase):
    def test_release_and_modpack_fallback_are_current(self):
        self.assertEqual(launcher.CONFIG["LAUNCHER_VERSION"], "1.66.32")
        self.assertEqual(
            launcher.CONFIG["LAUNCHER_CHANGELOG"][0]["version"], "1.66.32"
        )
        self.assertEqual(launcher.CONFIG["MODPACK_VERSION"], 15)

    def test_manifest_has_only_the_pinned_jei_compatibility_exception(self):
        extras = launcher.CONFIG["EXTRA_CLIENT_MODS"]
        self.assertEqual(len(extras), 1)
        jei = extras[0]
        self.assertEqual(
            jei["filename"],
            "jei-1.21.1-neoforge-19.39.0.369.jar",
        )
        self.assertTrue(jei["required"])
        self.assertEqual(jei["size"], 1635413)
        self.assertEqual(
            jei["sha256"].lower(),
            "79b6d034fa233cc87c5fe486387f69cdb"
            "54078e1262b440b5c7e7853a0254adf",
        )
        self.assertTrue(
            jei["url"].startswith(
                "https://industrialhorizon.b-cdn.net/stable/mods/"
            )
        )
        optional = launcher.CONFIG["OPTIONAL_MODS"]
        self.assertTrue(optional)
        self.assertTrue(all(mod.get("default") is False for mod in optional))
        self.assertTrue(all(mod.get("url") for mod in optional))
        self.assertTrue(all(mod.get("hashes") for mod in optional))
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
                mock.patch.dict(
                    launcher.CONFIG,
                    {"EXTRA_CLIENT_MODS": []},
                ),
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
        self.assertIn(
            "storage.bunnycdn.com/industrial-horizon-downloads/stable",
            WORKFLOW,
        )
        self.assertIn("BUNNY_STORAGE_ACCESS_KEY", WORKFLOW)
        self.assertIn("BUNNY_API_KEY", WORKFLOW)
        self.assertNotIn("lftp -c", WORKFLOW)
        upload = WORKFLOW.split(
            "- name: Upload installer payload to Bunny Storage", 1
        )[1].split("- name: Purge and verify installer payload", 1)[0]
        self.assertIn('upload "$VERSIONED"', upload)
        self.assertIn('upload "$VERSIONED.sha256"', upload)
        marker = WORKFLOW.split("- name: Publish version marker last", 1)[1]
        self.assertLess(
            WORKFLOW.index("- name: Purge and verify installer payload"),
            WORKFLOW.index("- name: Publish version marker last"),
        )
        self.assertIn(
            'BASE="https://industrialhorizon.b-cdn.net/stable"',
            WORKFLOW,
        )
        self.assertIn("--upload-file deploy/launcher_version.txt", marker)

    def test_existing_release_can_repair_bunny_without_rebuilding(self):
        self.assertIn("gh release download", REPAIR_WORKFLOW)
        self.assertIn("BUNNY_STORAGE_ACCESS_KEY", REPAIR_WORKFLOW)
        self.assertIn("BUNNY_API_KEY", REPAIR_WORKFLOW)
        self.assertIn("CheckpointSetup-${VERSION}.exe", REPAIR_WORKFLOW)
        self.assertLess(
            REPAIR_WORKFLOW.index(
                "- name: Purge and verify payload before activation"
            ),
            REPAIR_WORKFLOW.index(
                "- name: Activate marker last and verify"
            ),
        )


if __name__ == "__main__":
    unittest.main()
