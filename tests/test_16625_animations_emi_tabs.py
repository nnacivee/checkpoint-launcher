import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import launcher


PINNED_CLIENT_ADDITIONS = {
    "emi-tabs-1-0-0": {
        "filename": "EmiTabs-neoforge-1.0.0+1.21.1.jar",
        "mirror": True,
        "url": (
            "https://cdn.modrinth.com/data/Rz9g2Db4/versions/q44wR6Jf/"
            "EmiTabs-neoforge-1.0.0%2B1.21.1.jar"
        ),
    },
    "playeranimator-2-0-4": {
        "filename": "player-animation-lib-forge-2.0.4+1.21.1.jar",
        "mirror": True,
        "sha256": (
            "DBE5DE45F5CD60C0E5E47AF14E6D564534A98456E973CF670CB881F6938EEE92"
        ),
        "url": (
            "https://cdn.modrinth.com/data/gedNE4y2/versions/HJZB6bmA/"
            "player-animation-lib-forge-2.0.4%2B1.21.1.jar"
        ),
    },
    "vintage-animations-1-4-0": {
        "filename": "vintage_animations-neoforge-1.4.0.jar",
        "mirror": False,
        "sha256": (
            "8C574E7EFFAC9DC89F7F11891372F6C3526DA9C31B89020A64078897FB89A490"
        ),
        "url": (
            "https://cdn.modrinth.com/data/yY9ix3J0/versions/mWrDw9oM/"
            "vintage_animations-neoforge-1.4.0.jar"
        ),
    },
}


def _valid_jar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = (
        'modLoader="javafml"\n'
        'loaderVersion="[4,)"\n'
        'license="MIT"\n'
        '[[mods]]\n'
        'modId="emitabs"\n'
        'version="1.0.0"\n'
        'displayName="EMI Tabs"\n'
    )
    with zipfile.ZipFile(path, "w") as jar:
        jar.writestr("META-INF/neoforge.mods.toml", metadata)
        jar.writestr("assets/emitabs/payload.bin", bytes(range(256)) * 8)


class Launcher16625ClientAdditionsTests(unittest.TestCase):
    def test_versions_urls_and_optional_contract_are_pinned(self):
        entries = {
            entry["slug"]: entry
            for entry in launcher.CONFIG["EXTRA_CLIENT_MODS"]
            if entry.get("slug") in PINNED_CLIENT_ADDITIONS
        }

        self.assertEqual(set(entries), set(PINNED_CLIENT_ADDITIONS))
        for slug, expected in PINNED_CLIENT_ADDITIONS.items():
            self.assertEqual(entries[slug]["filename"], expected["filename"])
            self.assertEqual(entries[slug]["url"], expected["url"])
            if "sha256" in expected:
                self.assertEqual(entries[slug]["sha256"], expected["sha256"])
            self.assertIs(entries[slug]["mirror"], expected["mirror"])
            self.assertIs(entries[slug]["required"], False)

        self.assertEqual(
            entries["playeranimator-2-0-4"]["atomic_group"],
            "vintage-animations",
        )
        self.assertEqual(
            entries["vintage-animations-1-4-0"]["atomic_group"],
            "vintage-animations",
        )
        self.assertEqual(
            entries["emi-tabs-1-0-0"]["enabled_with_optional_mod"], "emi"
        )
        self.assertEqual(
            entries["emi-tabs-1-0-0"]["sha256"],
            "2E5B1C35F7E345BD1620AFA645C570743D295C1F49292E38AFF33C177EA901DA",
        )

    def test_emi_tabs_is_removed_when_player_disables_emi(self):
        entry = next(
            item for item in launcher.CONFIG["EXTRA_CLIENT_MODS"]
            if item.get("slug") == "emi-tabs-1-0-0"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            instance = root / "instance"
            cache = app_dir / "extra_client_mods_cache"
            cached_jar = cache / entry["filename"]
            installed_jar = instance / "mods" / entry["filename"]
            _valid_jar(cached_jar)
            installed_jar.parent.mkdir(parents=True, exist_ok=True)
            installed_jar.write_bytes(cached_jar.read_bytes())
            cache.joinpath(".installed.json").write_text(
                json.dumps({entry["slug"]: entry["filename"]}),
                encoding="utf-8",
            )
            cache.joinpath(".integrity.json").write_text(
                json.dumps({
                    entry["filename"]: launcher._jar_cache_record(cached_jar)
                }),
                encoding="utf-8",
            )

            with (
                mock.patch.object(launcher, "APP_DATA_DIR", app_dir),
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.dict(
                    launcher.CONFIG,
                    {"EXTRA_CLIENT_MODS": [entry], "REMOVED_MODS": []},
                    clear=False,
                ),
                mock.patch.object(
                    launcher,
                    "get_optional_mods_selection",
                    return_value={"emi": False},
                ),
            ):
                self.assertEqual(launcher.install_extra_client_mods(), [])

            self.assertFalse(cached_jar.exists())
            self.assertFalse(installed_jar.exists())
            installed = json.loads(
                cache.joinpath(".installed.json").read_text(encoding="utf-8")
            )
            self.assertEqual(installed, {})

    def test_incomplete_animation_pair_is_removed_without_blocking_launch(self):
        entries = [
            item for item in launcher.CONFIG["EXTRA_CLIENT_MODS"]
            if item.get("atomic_group") == "vintage-animations"
        ]
        player = next(
            item for item in entries
            if item["slug"] == "playeranimator-2-0-4"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            instance = root / "instance"
            cache = app_dir / "extra_client_mods_cache"
            player_cache = cache / player["filename"]
            player_installed = instance / "mods" / player["filename"]
            _valid_jar(player_cache)
            player_installed.parent.mkdir(parents=True, exist_ok=True)
            player_installed.write_bytes(player_cache.read_bytes())
            cache.joinpath(".installed.json").write_text(
                json.dumps({player["slug"]: player["filename"]}),
                encoding="utf-8",
            )
            cache.joinpath(".integrity.json").write_text(
                json.dumps({
                    player["filename"]:
                        launcher._jar_cache_record(player_cache)
                }),
                encoding="utf-8",
            )

            with (
                mock.patch.object(launcher, "APP_DATA_DIR", app_dir),
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.dict(
                    launcher.CONFIG,
                    {"EXTRA_CLIENT_MODS": entries, "REMOVED_MODS": []},
                    clear=False,
                ),
                mock.patch.object(
                    launcher,
                    "get_optional_mods_selection",
                    return_value={"emi": True},
                ),
                mock.patch.object(
                    launcher, "_fetch_tiny_text", return_value=""
                ),
                mock.patch.object(
                    launcher,
                    "_modrinth_metadata_for_direct_url",
                    return_value={},
                ),
                mock.patch.object(launcher, "download_file") as download,
            ):
                self.assertEqual(launcher.install_extra_client_mods(), [])

            download.assert_not_called()
            self.assertFalse(player_cache.exists())
            self.assertFalse(player_installed.exists())
            installed = json.loads(
                cache.joinpath(".installed.json").read_text(encoding="utf-8")
            )
            self.assertEqual(installed, {})

    def test_launcher_version_and_changelog_are_in_sync(self):
        self.assertEqual(launcher.CONFIG["LAUNCHER_VERSION"], "1.66.25")
        self.assertEqual(
            launcher.CONFIG["LAUNCHER_CHANGELOG"][0]["version"], "1.66.25"
        )


if __name__ == "__main__":
    unittest.main()
