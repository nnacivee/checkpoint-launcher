import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("launcher_16627", ROOT / "launcher.py")
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)

INSTALLER = (ROOT / "installer.iss").read_text(encoding="utf-8")
WORKFLOW = (
    ROOT / ".github" / "workflows" / "build.yml"
).read_text(encoding="utf-8")


class SilentRelaunchReleaseTests(unittest.TestCase):
    def test_release_version_and_changelog_are_current(self):
        self.assertEqual(launcher.CONFIG["LAUNCHER_VERSION"], "1.66.28")
        self.assertEqual(
            launcher.CONFIG["LAUNCHER_CHANGELOG"][0]["version"], "1.66.28"
        )

    def test_silent_update_launches_new_launcher(self):
        self.assertIn("Check: ShouldLaunchAfterSilentInstall", INSTALLER)
        self.assertIn(
            "function ShouldLaunchAfterSilentInstall(): Boolean;", INSTALLER
        )
        self.assertIn("WizardSilent", INSTALLER)
        self.assertIn("{param:NOAUTOLAUNCH|0}", INSTALLER)

    def test_ci_suppresses_autolaunch_during_smoke_install(self):
        self.assertIn('"/NOAUTOLAUNCH=1"', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
