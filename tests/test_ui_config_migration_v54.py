import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher


NEAT_BEFORE = """\
[general]
\tdraw_background = true
\tshow_attributes = true
\tshow_max_hp = true
\tshow_current_hp = true
\tshow_hp_percentage = false
\tplate_size = 37
"""

FTB_BEFORE = """\
{
\tappearance: {
\t\tsaturation: 0.75d
\t}
\twaypoints: {
\t\tin_world_waypoints: true
\t\tdeath_waypoints: true
\t}
}
"""


class UiConfigMigrationV54Tests(unittest.TestCase):
    @staticmethod
    def _write_pack_marker(instance: Path, version: int) -> None:
        (instance / ".configpack.json").write_text(
            json.dumps({"version": version, "owns": []}),
            encoding="utf-8",
        )

    @staticmethod
    def _write_configs(instance: Path, *, local_override=False) -> None:
        config = instance / "config"
        config.mkdir(parents=True)
        (config / "neat-client.toml").write_text(
            NEAT_BEFORE, encoding="utf-8"
        )
        (config / "ftbchunks-client.snbt").write_bytes(
            FTB_BEFORE.replace("\n", "\r\n").encode("utf-8")
        )
        if local_override:
            local = instance / "local"
            local.mkdir()
            (local / "ftbchunks-client.snbt").write_text(
                FTB_BEFORE.replace("0.75d", "0.25d"),
                encoding="utf-8",
            )

    def test_migration_changes_only_required_booleans(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_pack_marker(instance, 54)
            self._write_configs(instance, local_override=True)

            with mock.patch.multiple(
                launcher,
                INSTANCE_DIR=instance,
                CONFIGPACK_MARKER_FILE=instance / ".configpack.json",
            ):
                self.assertTrue(launcher.install_ui_config_migration_v54())

            neat = (instance / "config" / "neat-client.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn("draw_background = false", neat)
            self.assertIn("show_attributes = false", neat)
            self.assertIn("show_max_hp = false", neat)
            self.assertIn("show_current_hp = false", neat)
            self.assertIn("show_hp_percentage = true", neat)
            self.assertIn("plate_size = 37", neat)

            ftb = (
                instance / "config" / "ftbchunks-client.snbt"
            ).read_text(encoding="utf-8")
            ftb_bytes = (
                instance / "config" / "ftbchunks-client.snbt"
            ).read_bytes()
            local_ftb = (
                instance / "local" / "ftbchunks-client.snbt"
            ).read_text(encoding="utf-8")
            self.assertIn("in_world_waypoints: false", ftb)
            self.assertIn("saturation: 0.75d", ftb)
            self.assertIn("death_waypoints: true", ftb)
            self.assertEqual(
                ftb_bytes.count(b"\r\n"), FTB_BEFORE.count("\n")
            )
            self.assertIn("in_world_waypoints: false", local_ftb)
            self.assertIn("saturation: 0.25d", local_ftb)

            marker = json.loads(
                (
                    instance / launcher.UI_CONFIG_MIGRATION_MARKER_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(marker["version"], 54)
            self.assertEqual(
                marker["patched_files"],
                [
                    "config/neat-client.toml",
                    "config/ftbchunks-client.snbt",
                    "local/ftbchunks-client.snbt",
                ],
            )

    def test_marker_makes_migration_one_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_pack_marker(instance, 54)
            self._write_configs(instance)

            with mock.patch.multiple(
                launcher,
                INSTANCE_DIR=instance,
                CONFIGPACK_MARKER_FILE=instance / ".configpack.json",
            ):
                self.assertTrue(launcher.install_ui_config_migration_v54())
                neat_path = instance / "config" / "neat-client.toml"
                neat_path.write_text(
                    neat_path.read_text(encoding="utf-8").replace(
                        "draw_background = false",
                        "draw_background = true",
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(launcher.install_ui_config_migration_v54())

            self.assertIn(
                "draw_background = true",
                neat_path.read_text(encoding="utf-8"),
            )

    def test_migration_waits_for_configpack_v54(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_pack_marker(instance, 53)
            self._write_configs(instance)
            neat_path = instance / "config" / "neat-client.toml"

            with mock.patch.multiple(
                launcher,
                INSTANCE_DIR=instance,
                CONFIGPACK_MARKER_FILE=instance / ".configpack.json",
            ):
                self.assertFalse(launcher.install_ui_config_migration_v54())

            self.assertEqual(
                neat_path.read_text(encoding="utf-8"), NEAT_BEFORE
            )
            self.assertFalse(
                (instance / launcher.UI_CONFIG_MIGRATION_MARKER_NAME).exists()
            )

    def test_missing_key_defers_without_partial_changes_or_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_pack_marker(instance, 54)
            self._write_configs(instance)
            neat_path = instance / "config" / "neat-client.toml"
            neat_path.write_text(
                NEAT_BEFORE.replace("\tshow_max_hp = true\n", ""),
                encoding="utf-8",
            )
            before = neat_path.read_bytes()

            with mock.patch.multiple(
                launcher,
                INSTANCE_DIR=instance,
                CONFIGPACK_MARKER_FILE=instance / ".configpack.json",
            ):
                self.assertFalse(launcher.install_ui_config_migration_v54())

            self.assertEqual(neat_path.read_bytes(), before)
            self.assertFalse(
                (instance / launcher.UI_CONFIG_MIGRATION_MARKER_NAME).exists()
            )


if __name__ == "__main__":
    unittest.main()
