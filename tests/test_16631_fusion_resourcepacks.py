import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "launcher_16631", ROOT / "launcher.py"
)
launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launcher)


class FusionResourcePacksReleaseTests(unittest.TestCase):
    def test_fusion_packs_are_optional_catalog_entries(self):
        expected = {
            "fusion-connected-glass",
            "fusion-connected-blocks",
            "fusion-block-transitions",
        }
        configured = {
            pack["slug"]
            for pack in launcher.CONFIG["RECOMMENDED_RESOURCE_PACKS"]
        }
        self.assertTrue(expected <= configured)
        self.assertEqual(launcher.CONFIG["AUTO_RESOURCE_PACKS"], [])

    def test_resource_pack_slugs_remain_unique(self):
        slugs = [
            pack["slug"]
            for pack in launcher.CONFIG["RECOMMENDED_RESOURCE_PACKS"]
        ]
        self.assertEqual(len(slugs), len(set(slugs)))


if __name__ == "__main__":
    unittest.main()
