import hashlib
import sys
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import launcher


def java_offline_uuid(username: str) -> str:
    """Reference implementation of UUID.nameUUIDFromBytes."""
    digest = hashlib.md5(
        f"OfflinePlayer:{username}".encode("utf-8"),
        usedforsecurity=False,
    ).digest()
    return str(uuid.UUID(bytes=digest, version=3))


class OfflineUuidTests(unittest.TestCase):
    def test_matches_known_server_uuids(self):
        expected = {
            "nnacivee": "28a0db2d-73d3-3eff-9094-cbbab1468ee5",
            "Dimylechka": "ad2cea0e-c7a6-36ed-9509-467f32d463c3",
            "nnacivee1": "8fd64c87-25e1-347b-a781-7f6082cf3de4",
            "nnacivee12": "d34f19d6-957e-3f25-8bd9-23e005aa34f4",
        }
        for username, server_uuid in expected.items():
            with self.subTest(username=username):
                self.assertEqual(launcher.offline_uuid(username), server_uuid)

    def test_matches_java_algorithm_for_unicode_and_case(self):
        for username in ("Player", "player", "Техник_1", "A1"):
            with self.subTest(username=username):
                self.assertEqual(
                    launcher.offline_uuid(username),
                    java_offline_uuid(username),
                )

    def test_does_not_use_an_rfc_namespace(self):
        username = "nnacivee12"
        wrong = str(uuid.uuid3(uuid.NAMESPACE_OID, f"OfflinePlayer:{username}"))
        self.assertNotEqual(launcher.offline_uuid(username), wrong)


if __name__ == "__main__":
    unittest.main()
