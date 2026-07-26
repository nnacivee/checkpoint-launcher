import tempfile
import os
import time
import unittest
from pathlib import Path
from unittest import mock

import launcher


class CrashDiagnosisTests(unittest.TestCase):
    def diagnose(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "latest_launch.log").write_text(text, encoding="utf-8")
            with mock.patch.object(launcher, "INSTANCE_DIR", instance):
                return launcher.diagnose_game_exit()

    def test_out_of_memory_is_explained(self):
        self.assertEqual(
            self.diagnose("java.lang.OutOfMemoryError: Java heap space"),
            "IH_DIAG_MEMORY",
        )

    def test_mod_loading_failure_is_explained(self):
        self.assertEqual(
            self.diagnose("Loading errors encountered: Mixin apply failed"),
            "IH_DIAG_MODS",
        )

    def test_video_driver_failure_is_explained(self):
        self.assertEqual(
            self.diagnose("GLFW error 65542: WGL driver does not support OpenGL"),
            "IH_DIAG_VIDEO",
        )

    def test_unknown_exit_has_a_clear_fallback(self):
        self.assertEqual(
            self.diagnose("Process ended without a recognizable signature"),
            "IH_DIAG_UNKNOWN",
        )

    def test_old_crash_report_cannot_override_current_unknown_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            current = instance / "latest_launch.log"
            current.write_text("current unrecognized failure", encoding="utf-8")
            crash_dir = instance / "crash-reports"
            crash_dir.mkdir()
            old = crash_dir / "crash-old.txt"
            old.write_text("java.lang.OutOfMemoryError", encoding="utf-8")
            old_time = time.time() - 3600
            os.utime(old, (old_time, old_time))
            with mock.patch.object(launcher, "INSTANCE_DIR", instance):
                result = launcher.diagnose_game_exit()
        self.assertEqual(result, "IH_DIAG_UNKNOWN")

    def test_access_violation_alone_is_not_blamed_on_video_driver(self):
        self.assertEqual(
            self.diagnose("EXCEPTION_ACCESS_VIOLATION in an unknown module"),
            "IH_DIAG_UNKNOWN",
        )

    def test_all_diagnoses_have_ru_ua_en_player_text(self):
        root = Path(__file__).parents[1]
        html = (root / "ui" / "center-control-layouts.html").read_text(
            encoding="utf-8"
        )
        translations = (root / "ui" / "assets" / "i18n.js").read_text(
            encoding="utf-8"
        )
        for code in (
            "IH_DIAG_MEMORY",
            "IH_DIAG_DISK",
            "IH_DIAG_VIDEO",
            "IH_DIAG_MODS",
            "IH_DIAG_JAVA",
            "IH_DIAG_UNKNOWN",
        ):
            self.assertIn(code, html)
        self.assertIn(
            "Причину вылета определить не удалось", translations
        )
        self.assertIn(
            "Не вдалося визначити причину вильоту", translations
        )
        self.assertIn(
            "The crash cause could not be identified", translations
        )


if __name__ == "__main__":
    unittest.main()
