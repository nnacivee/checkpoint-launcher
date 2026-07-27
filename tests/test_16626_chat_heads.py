import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("launcher_16626", ROOT / "launcher.py")
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


class ChatHeadsReleaseTests(unittest.TestCase):
    def test_release_version_and_changelog_are_current(self):
        self.assertEqual(launcher.CONFIG["LAUNCHER_VERSION"], "1.66.26")
        self.assertEqual(
            launcher.CONFIG["LAUNCHER_CHANGELOG"][0]["version"], "1.66.26"
        )

    def test_exact_neoforge_chat_heads_release_is_managed(self):
        entries = {
            item.get("slug"): item
            for item in launcher.CONFIG["EXTRA_CLIENT_MODS"]
        }
        entry = entries["chat-heads"]
        self.assertEqual(
            entry["url"],
            "https://cdn.modrinth.com/data/Wb5oqrBJ/versions/"
            "BBw4KFaY/chat_heads-0.15.3-neoforge-1.21.jar",
        )
        self.assertEqual(
            entry["filename"], "chat_heads-0.15.3-neoforge-1.21.jar"
        )
        self.assertEqual(
            entry["sha256"],
            "1AA41AA6D8E28D66D9379CCC02197AA540B607D1A7DDEAD3F1CCB72E57118BEB",
        )
        self.assertTrue(entry["mirror"])
        self.assertFalse(entry["required"])
        self.assertIn(
            "chat_heads-0.15.2-neoforge-1.21.jar",
            entry["replaces"],
        )

    def test_chat_heads_is_no_longer_blocked(self):
        blocked = [
            str(value).lower()
            for value in launcher.CONFIG["REMOVED_MODS"]
        ]
        self.assertFalse(
            any("chat_heads" in value for value in blocked),
            blocked,
        )


if __name__ == "__main__":
    unittest.main()
