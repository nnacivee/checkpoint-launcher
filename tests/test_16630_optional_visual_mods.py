import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "launcher_16630_optional", ROOT / "launcher.py"
)
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


class OptionalVisualModsTests(unittest.TestCase):
    def test_catalog_is_pinned_default_off_and_excludes_shulker_tooltip(self):
        entries = launcher.CONFIG["OPTIONAL_MODS"]
        visible = {
            entry["id"] for entry in entries if entry.get("visible", True)
        }

        self.assertEqual(len(visible), 16)
        self.assertNotIn("shulker_box_tooltip", visible)
        self.assertIn("smooth_swapping", visible)
        self.assertTrue(all(entry.get("default") is False for entry in entries))
        self.assertTrue(all(entry.get("url") for entry in entries))
        self.assertTrue(all(entry.get("hashes") for entry in entries))
        self.assertEqual(
            next(
                entry for entry in entries if entry["id"] == "more_culling"
            )["filename"],
            "moreculling-neoforge-1.21.1-1.0.8.jar",
        )
        smooth = next(
            entry for entry in entries if entry["id"] == "smooth_swapping"
        )
        smooth_config = json.loads(smooth["config_seeds"][0]["content"])
        self.assertEqual(smooth_config["animation_speed"], 300)
        self.assertIn("curve_points", smooth_config)
        self.assertNotIn("animationSpeed", smooth_config)
        self.assertIn("chunksfadein", launcher._RENDER_MOD_PATTERNS)

    def test_dependencies_are_derived_and_exclusive_choice_prefers_new_toggle(self):
        empty = launcher.normalise_optional_mods_selection({})
        self.assertFalse(any(empty.values()))

        legendary = launcher.normalise_optional_mods_selection(
            {"legendary_tooltips": True}
        )
        self.assertTrue(legendary["legendary_tooltips"])
        self.assertTrue(legendary["prism"])
        self.assertFalse(legendary["stylish_effects"])

        status = launcher.normalise_optional_mods_selection(
            {"status_effect_bars": True}
        )
        self.assertTrue(status["status_effect_bars"])
        self.assertTrue(status["stylish_effects"])

        orphan = launcher.normalise_optional_mods_selection({"prism": True})
        self.assertFalse(orphan["prism"])

        pickup = launcher.normalise_optional_mods_selection(
            {"item_highlighter": True, "pick_up_notifier": True},
            preferred_id="pick_up_notifier",
        )
        self.assertFalse(pickup["item_highlighter"])
        self.assertTrue(pickup["pick_up_notifier"])

    def test_exact_download_requires_matching_pinned_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar = root / "source.jar"
            with zipfile.ZipFile(
                jar, "w", compression=zipfile.ZIP_STORED
            ) as archive:
                archive.writestr("META-INF/neoforge.mods.toml", os.urandom(2048))
            payload = jar.read_bytes()
            hashes = {
                "sha1": hashlib.sha1(payload).hexdigest(),
                "sha512": hashlib.sha512(payload).hexdigest(),
            }
            entry = {
                "id": "test",
                "name": "Test",
                "slug": "test",
                "filename": "test.jar",
                "url": "https://cdn.modrinth.com/data/test/versions/test/test.jar",
                "hashes": hashes,
            }

            def download(_url, destination, **_kwargs):
                Path(destination).write_bytes(payload)

            with (
                mock.patch.object(
                    launcher, "OPTIONAL_CACHE_DIR", root / "cache"
                ),
                mock.patch.object(launcher, "download_file", side_effect=download),
            ):
                self.assertTrue(
                    launcher._download_optional_from_modrinth(entry)
                )
                cached = root / "cache" / "test.jar"
                self.assertTrue(
                    launcher._optional_mod_file_is_valid(cached, entry)
                )

                bad = {**entry, "hashes": {**hashes, "sha1": "0" * 40}}
                cached.unlink()
                self.assertFalse(
                    launcher._download_optional_from_modrinth(bad)
                )
                self.assertFalse(cached.exists())

    def test_dependency_failure_never_leaves_parent_jar_enabled(self):
        entries = [
            {
                "id": "library",
                "name": "Library",
                "filename": "library.jar",
                "default": False,
            },
            {
                "id": "feature",
                "name": "Feature",
                "filename": "feature.jar",
                "requires": ["library"],
                "default": False,
            },
        ]
        calls = []

        def apply(mod, enabled, _status=None):
            calls.append((mod["id"], enabled))
            return "failed" if mod["id"] == "library" and enabled else ""

        with (
            mock.patch.dict(
                launcher.CONFIG, {"OPTIONAL_MODS": entries}, clear=False
            ),
            mock.patch.object(
                launcher,
                "get_optional_mods_selection",
                return_value={"library": True, "feature": True},
            ),
            mock.patch.object(
                launcher, "load_settings", return_value={"no_sodium": False}
            ),
            mock.patch.object(
                launcher, "_apply_one_optional_mod", side_effect=apply
            ),
        ):
            failed = launcher.apply_optional_mods()

        self.assertEqual(calls, [("library", True), ("feature", False)])
        self.assertEqual(failed, ["Library", "Feature"])

    def test_sodium_dependent_option_is_temporarily_disabled_in_safe_gpu_mode(self):
        entry = {
            "id": "visual",
            "name": "Visual",
            "filename": "visual.jar",
            "requires_sodium": True,
            "default": False,
        }
        calls = []
        with (
            mock.patch.dict(
                launcher.CONFIG, {"OPTIONAL_MODS": [entry]}, clear=False
            ),
            mock.patch.object(
                launcher,
                "get_optional_mods_selection",
                return_value={"visual": True},
            ),
            mock.patch.object(
                launcher, "load_settings", return_value={"no_sodium": True}
            ),
            mock.patch.object(
                launcher,
                "_apply_one_optional_mod",
                side_effect=lambda mod, enabled, _status=None: (
                    calls.append((mod["id"], enabled)) or ""
                ),
            ),
        ):
            self.assertEqual(launcher.apply_optional_mods(), [])

        self.assertEqual(calls, [("visual", False)])

    def test_config_seeds_and_modernui_tooltip_are_targeted_and_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            app_data = instance / "launcher-state"
            seed = {
                "config_seeds": [{
                    "path": "config/example.json",
                    "content": "{\"value\": 1}\n",
                }],
            }
            modernui = instance / "config" / "ModernUI" / "client.toml"
            modernui.parent.mkdir(parents=True)
            modernui.write_text(
                "[general]\nenable = true\n\n"
                "[tooltip]\n\tenable = true\n\troundedShape = true\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "APP_DATA_DIR", app_data),
            ):
                launcher._seed_optional_mod_configs(seed)
                seeded = instance / "config" / "example.json"
                self.assertEqual(seeded.read_text(encoding="utf-8"), "{\"value\": 1}\n")
                seeded.write_text("{\"value\": 7}\n", encoding="utf-8")
                launcher._seed_optional_mod_configs(seed)
                self.assertEqual(seeded.read_text(encoding="utf-8"), "{\"value\": 7}\n")

                launcher._reconcile_modernui_tooltip(True)
                marker = launcher._modernui_tooltip_state_path()
                self.assertTrue(marker.is_file())
                marker_before = marker.read_text(encoding="utf-8")
                modernui.write_text(
                    modernui.read_text(encoding="utf-8")
                    + "blur = 7\n",
                    encoding="utf-8",
                )
                launcher._reconcile_modernui_tooltip(True)
                self.assertEqual(
                    marker.read_text(encoding="utf-8"), marker_before
                )
                launcher._reconcile_modernui_tooltip(False)

            patched = modernui.read_text(encoding="utf-8")
            self.assertIn("[general]\nenable = true", patched)
            self.assertIn("[tooltip]\n\tenable = true", patched)
            self.assertIn("blur = 7", patched)
            self.assertFalse(marker.exists())

    def test_modernui_tooltip_absent_file_is_created_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "APP_DATA_DIR", root / "state"),
            ):
                launcher._reconcile_modernui_tooltip(True)
                modernui = instance / "config" / "ModernUI" / "client.toml"
                self.assertIn(
                    "[tooltip]\nenable = false",
                    modernui.read_text(encoding="utf-8"),
                )
                modernui.write_text(
                    modernui.read_text(encoding="utf-8")
                    + "fontScale = 1.25\n",
                    encoding="utf-8",
                )
                launcher._reconcile_modernui_tooltip(False)
                restored = modernui.read_text(encoding="utf-8")
                self.assertIn("enable = true", restored)
                self.assertIn("fontScale = 1.25", restored)

    def test_modernui_read_errors_fail_closed_without_losing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            app_data = root / "state"
            modernui = instance / "config" / "ModernUI" / "client.toml"
            modernui.parent.mkdir(parents=True)
            modernui.write_text(
                "[tooltip]\nenable = true\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "APP_DATA_DIR", app_data),
            ):
                launcher._reconcile_modernui_tooltip(True)
                marker = launcher._modernui_tooltip_state_path()
                marker_before = marker.read_text(encoding="utf-8")
                with mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=OSError("locked"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "Modern UI"
                    ):
                        launcher._reconcile_modernui_tooltip(False)
                self.assertTrue(marker.is_file())
                self.assertEqual(
                    marker.read_text(encoding="utf-8"),
                    marker_before,
                )

    def test_retired_legendary_catalog_does_not_restore_modernui_blindly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = (
                root
                / "state"
                / "optional_mod_state"
                / "modernui_tooltip.json"
            )
            marker.parent.mkdir(parents=True)
            marker.write_text(
                '{"previous_enabled": true}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(launcher, "APP_DATA_DIR", root / "state"),
                mock.patch.dict(
                    launcher.CONFIG,
                    {"OPTIONAL_MODS": []},
                    clear=False,
                ),
                mock.patch.object(
                    launcher,
                    "_reconcile_modernui_tooltip",
                ) as reconcile,
            ):
                launcher._reconcile_legendary_tooltip_if_configured()
            reconcile.assert_not_called()
            self.assertTrue(marker.is_file())

    def test_disabled_legendary_without_state_never_reads_modernui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", root / "instance"),
                mock.patch.object(launcher, "APP_DATA_DIR", root / "state"),
                mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=OSError("locked"),
                ) as read_text,
                mock.patch.object(
                    launcher,
                    "_atomic_write_text",
                ) as write_text,
            ):
                launcher._reconcile_modernui_tooltip(False)
            read_text.assert_not_called()
            write_text.assert_not_called()

    def test_only_selected_optional_files_are_managed_and_preferred(self):
        entries = [
            {
                "id": "selected",
                "filename": "selected.jar",
                "default": False,
            },
            {
                "id": "disabled",
                "filename": "disabled.jar",
                "default": False,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(
                    launcher.CONFIG,
                    {"OPTIONAL_MODS": entries, "EXTRA_CLIENT_MODS": []},
                    clear=False,
                ),
                mock.patch.object(
                    launcher,
                    "get_optional_mods_selection",
                    return_value={"selected": True, "disabled": False},
                ),
                mock.patch.object(
                    launcher, "_load_cached_modpack_manifest", return_value={}
                ),
                mock.patch.object(
                    launcher, "_read_configpack_marker", return_value={}
                ),
                mock.patch.object(launcher, "APP_DATA_DIR", Path(tmp)),
            ):
                managed = launcher._managed_mod_filenames()
                preferred = launcher._preferred_mod_filenames()

        self.assertIn("selected.jar", managed)
        self.assertIn("selected.jar", preferred)
        self.assertNotIn("disabled.jar", managed)
        self.assertNotIn("disabled.jar", preferred)

    def test_disabled_optional_jar_must_be_physically_absent_before_launch(self):
        entry = {
            "id": "old_choice",
            "name": "Old Choice",
            "filename": "old-choice.jar",
            "default": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            jar = instance / "mods" / entry["filename"]
            jar.parent.mkdir(parents=True)
            jar.write_bytes(b"locked")
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.dict(
                    launcher.CONFIG, {"OPTIONAL_MODS": [entry]}, clear=False
                ),
                mock.patch.object(
                    launcher,
                    "get_optional_mods_selection",
                    return_value={"old_choice": False},
                ),
                mock.patch.object(
                    launcher, "load_settings", return_value={}
                ),
                mock.patch.object(
                    launcher,
                    "_apply_one_optional_mod",
                    return_value="file is busy",
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Не удалось отключить"
                ):
                    launcher._ensure_disabled_optional_mods_absent()

    def test_legendary_compatibility_follows_the_physical_jar(self):
        entry = {
            "id": "legendary_tooltips",
            "name": "Legendary Tooltips",
            "filename": "legendary.jar",
            "default": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            jar = instance / "mods" / entry["filename"]
            jar.parent.mkdir(parents=True)
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr(
                    "META-INF/neoforge.mods.toml", os.urandom(2048)
                )
            modernui = instance / "config" / "ModernUI" / "client.toml"
            modernui.parent.mkdir(parents=True)
            modernui.write_text(
                "[tooltip]\nenable = true\n", encoding="utf-8"
            )
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "APP_DATA_DIR", root / "state"),
                mock.patch.dict(
                    launcher.CONFIG, {"OPTIONAL_MODS": [entry]}, clear=False
                ),
            ):
                launcher._reconcile_legendary_tooltip_if_configured()

            self.assertIn(
                "enable = false", modernui.read_text(encoding="utf-8")
            )

    def test_retired_optional_cache_is_never_restored_into_mods(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            stale = cache / "old-optional.jar"
            stale.write_bytes(b"old")
            instance = root / "instance"

            with (
                mock.patch.object(launcher, "OPTIONAL_CACHE_DIR", cache),
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.dict(
                    launcher.CONFIG,
                    {"OPTIONAL_MODS": [], "REMOVED_MODS": []},
                    clear=False,
                ),
            ):
                launcher.restore_no_longer_optional_mods()

            self.assertTrue(stale.exists())
            self.assertFalse((instance / "mods" / stale.name).exists())


if __name__ == "__main__":
    unittest.main()
