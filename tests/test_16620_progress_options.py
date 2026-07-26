import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher


class Launcher16620ProgressTests(unittest.TestCase):
    @staticmethod
    def _progress():
        statuses = []
        values = []
        progress = launcher.LaunchProgress(
            statuses.append,
            values.append,
            [
                ("Minecraft", 28),
                ("NeoForge", 18),
                ("Сборка модов", 34),
                ("Моды и дополнения", 14),
                ("Настройки сборки", 6),
            ],
        )
        return progress, statuses, values

    def test_resettable_mll_counters_do_not_finish_stages_early(self):
        progress, statuses, values = self._progress()
        checkpoints = {}

        def install_minecraft(_version, _directory, callback):
            callback["setStatus"]("Download Libraries")
            callback["setMax"](2)
            for value in (1, 2, 3):
                callback["setProgress"](value)
            checkpoints["after_libraries"] = list(values)

            callback["setStatus"]("Download Assets")
            callback["setMax"](99)
            for value in (1, 50, 100):
                callback["setProgress"](value)
            checkpoints["after_assets"] = list(values)

            callback["setStatus"]("Install java runtime")
            callback["setMax"](9)
            callback["setProgress"](10)
            checkpoints["after_runtime"] = list(values)

        class FakeLoader:
            @staticmethod
            def is_minecraft_version_supported(_version):
                return True

            @staticmethod
            def install(
                _version,
                _directory,
                *,
                loader_version,
                callback,
                java,
            ):
                self = FakeLoader
                self.loader_version = loader_version
                self.java = java
                checkpoints["loader_started"] = list(values)
                callback["setStatus"]("Download Libraries")
                callback["setMax"](3)
                callback["setProgress"](4)
                callback["setStatus"]("Running installer")
                checkpoints["installer_running"] = list(values)
                return "neoforge-test"

        with (
            mock.patch.object(launcher, "_read_install_marker", return_value={}),
            mock.patch.object(launcher, "_check_installation_preconditions"),
            mock.patch.object(
                launcher.mll.install,
                "install_minecraft_version",
                side_effect=install_minecraft,
            ),
            mock.patch.object(
                launcher.mll.mod_loader,
                "get_mod_loader",
                return_value=FakeLoader(),
            ),
            mock.patch.object(
                launcher, "_find_bundled_java", return_value="java.exe"
            ),
            mock.patch.object(launcher, "_write_install_marker") as marker,
            mock.patch.dict(
                launcher.CONFIG,
                {
                    "MC_VERSION": "1.21.1",
                    "MOD_LOADER": "neoforge",
                    "LOADER_VERSION": "21.1.241",
                },
            ),
        ):
            version = launcher.install_minecraft_and_modloader(progress)

        self.assertEqual(version, "neoforge-test")
        self.assertEqual(checkpoints["after_libraries"], [])
        self.assertEqual(checkpoints["after_assets"], [])
        self.assertEqual(checkpoints["after_runtime"], [])
        self.assertEqual(checkpoints["loader_started"], [28])
        self.assertEqual(checkpoints["installer_running"], [28])
        self.assertEqual(values, [28, 46])
        self.assertTrue(
            any("Библиотеки · 3/3" in text for text in statuses)
        )
        self.assertTrue(
            any("Ресурсы игры · 100/100" in text for text in statuses)
        )
        self.assertTrue(
            any("Установка NeoForge" in text for text in statuses)
        )
        marker.assert_called_once_with("neoforge-test")

    def test_failed_loader_is_not_marked_complete(self):
        progress, _statuses, values = self._progress()

        class FailingLoader:
            @staticmethod
            def is_minecraft_version_supported(_version):
                return True

            @staticmethod
            def install(*_args, **_kwargs):
                raise RuntimeError("installer failed")

        with (
            mock.patch.object(launcher, "_read_install_marker", return_value={}),
            mock.patch.object(launcher, "_check_installation_preconditions"),
            mock.patch.object(
                launcher.mll.install,
                "install_minecraft_version",
                return_value=None,
            ),
            mock.patch.object(
                launcher.mll.mod_loader,
                "get_mod_loader",
                return_value=FailingLoader(),
            ),
            mock.patch.object(
                launcher, "_find_bundled_java", return_value="java.exe"
            ),
            mock.patch.object(launcher, "_write_install_marker") as marker,
            mock.patch.dict(
                launcher.CONFIG,
                {
                    "MC_VERSION": "1.21.1",
                    "MOD_LOADER": "neoforge",
                    "LOADER_VERSION": "21.1.241",
                },
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "installer failed"):
                launcher.install_minecraft_and_modloader(progress)

        self.assertEqual(values, [28])
        marker.assert_not_called()


class Launcher16620OptionsTests(unittest.TestCase):
    def test_fresh_options_have_current_version_and_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "runtime_log"),
            ):
                launcher.seed_default_keybinds()
                path = instance / "options.txt"
                first = path.read_bytes()
                lines = path.read_text(encoding="utf-8").splitlines()
                launcher.seed_default_keybinds()
                second = path.read_bytes()

        self.assertEqual(lines[0], "version:3955")
        self.assertIn("onboardAccessibility:false", lines)
        self.assertIn("guiScale:2", lines)
        self.assertIn("key_key.ezactions.open:key.keyboard.g", lines)
        self.assertIn(
            "key_key.voice_chat_group:key.keyboard.unknown", lines
        )
        self.assertEqual(first, second)

    def test_partial_modern_options_are_healed_before_minecraft_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            instance.mkdir()
            path = instance / "options.txt"
            path.write_text(
                "version:0\n"
                "key_key.ezactions.open:key.keyboard.g\n"
                "key_key.voice_chat_group:key.keyboard.unknown\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "runtime_log"),
            ):
                launcher.seed_default_keybinds()
            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(lines[0], "version:3955")
        self.assertEqual(
            lines.count("key_key.voice_chat_group:key.keyboard.unknown"), 1
        )

    def test_live_style_g_conflicts_and_forced_video_options_are_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            instance.mkdir()
            path = instance / "options.txt"
            path.write_text(
                "version:3955\n"
                "enableVsync:true\n"
                "guiScale:0\n"
                "maxFps:120\n"
                "key_key.curios.open.desc:key.keyboard.g\n"
                "key_key.jetpack.toggle_active.description:key.keyboard.g\n"
                "key_key.voice_chat_group:key.keyboard.g\n"
                "key_key.journeymap.toggle_entity_names:key.keyboard.g\n"
                "key_key.ezactions.open:key.keyboard.grave.accent\n"
                "key_key.guideme.guide:key.keyboard.g\n"
                "key_key.voice_chat:key.keyboard.v\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(launcher, "runtime_log"),
            ):
                launcher.seed_default_keybinds()
                launcher.apply_forced_options()
            lines = path.read_text(encoding="utf-8").splitlines()

        plain_g = [
            line for line in lines if line.endswith(":key.keyboard.g")
        ]
        self.assertEqual(
            plain_g, ["key_key.ezactions.open:key.keyboard.g"]
        )
        self.assertIn("key_key.curios.open.desc:key.keyboard.unknown", lines)
        self.assertIn(
            "key_key.jetpack.toggle_active.description:"
            "key.keyboard.semicolon",
            lines,
        )
        self.assertIn(
            "key_key.voice_chat_group:key.keyboard.unknown", lines
        )
        self.assertIn(
            "key_key.journeymap.toggle_entity_names:key.keyboard.unknown",
            lines,
        )
        self.assertIn("key_key.guideme.guide:key.keyboard.unknown", lines)
        self.assertIn("key_key.voice_chat:key.keyboard.v", lines)
        self.assertEqual(lines[0], "version:3955")
        self.assertIn("guiScale:2", lines)
        self.assertIn("maxFps:260", lines)
        self.assertIn("enableVsync:false", lines)

    def test_numeric_tab_ping_mod_is_retired(self):
        extra_slugs = {
            str(entry.get("slug", "")).lower()
            for entry in launcher.CONFIG["EXTRA_CLIENT_MODS"]
        }
        removed = {
            str(value).lower()
            for value in launcher.CONFIG["REMOVED_MODS"]
        }

        self.assertNotIn("ping-in-tablist", extra_slugs)
        self.assertIn("pingintablist", removed)

    def test_final_player_backup_keeps_the_healed_options_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            backup = root / "backup"
            with (
                mock.patch.object(launcher, "INSTANCE_DIR", instance),
                mock.patch.object(
                    launcher, "PLAYER_SETTINGS_BACKUP_DIR", backup
                ),
                mock.patch.object(launcher, "runtime_log"),
            ):
                launcher.seed_default_keybinds()
                launcher.backup_player_settings()

            saved = (backup / "options.txt").read_text(encoding="utf-8")

        self.assertTrue(saved.startswith("version:3955\n"))


class Launcher16620UiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (
            root / "ui" / "center-control-layouts.html"
        ).read_text(encoding="utf-8")
        cls.css = (
            root / "ui" / "assets" / "release-polish.css"
        ).read_text(encoding="utf-8")
        cls.i18n = (
            root / "ui" / "assets" / "i18n.js"
        ).read_text(encoding="utf-8")

    def test_live_timer_never_changes_the_real_percentage(self):
        self.assertIn("function renderClientHeartbeat()", self.html)
        heartbeat = self.html.split(
            "function renderClientHeartbeat()", 1
        )[1].split("function startClientActivity()", 1)[0]
        self.assertNotIn("clientUpdateProgress=", heartbeat)
        self.assertNotIn("--play-progress", heartbeat)
        self.assertNotIn("aria-valuenow", heartbeat)
        self.assertIn("formatClientClock(elapsed)", heartbeat)

    def test_stage_detail_is_visible_without_noisy_file_counter(self):
        self.assertIn("detail:percentMatch?'':detail", self.html)
        self.assertIn("stage.detail||stage.title", self.html)
        compact = self.html.split(
            "function compactLaunchPhase", 1
        )[1].split("function parseLaunchStage", 1)[0]
        self.assertIn(r"replace(/\b\d+\s*\/\s*\d+\b/g,'')", compact)
        self.assertNotIn("{current}/{total}", compact)
        self.assertIn(
            '["Библиотеки","Бібліотеки","Libraries"]', self.i18n
        )

    def test_activity_timer_fits_the_compact_arrow_column(self):
        self.assertIn("if(value>=5940)return'99+'", self.html)
        self.assertIn(
            '.play[data-progress-active="true"] .arrow', self.css
        )
        self.assertIn("font-variant-numeric:tabular-nums", self.css)
        self.assertIn("font-size:9px", self.css)

    def test_live_progress_animation_respects_reduced_motion(self):
        self.assertIn(
            '[data-progress-active="true"][data-progress-known="true"]',
            self.css,
        )
        reduced = self.css.split(
            "@media(prefers-reduced-motion:reduce)", 1
        )[1]
        self.assertIn("animation:none", reduced)


if __name__ == "__main__":
    unittest.main()
