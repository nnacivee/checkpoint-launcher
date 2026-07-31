import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "modpack_release.py"
SPEC = importlib.util.spec_from_file_location("modpack_release_sftp", SCRIPT)
modpack_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = modpack_release
SPEC.loader.exec_module(modpack_release)


class SftpBatchTests(unittest.TestCase):
    def test_sftp_quote_escapes_glob_metacharacters_inside_quotes(self):
        self.assertEqual(
            modpack_release.sftp_quote(
                "[Neoforge 1.21.1] Better Zoom v2.7.0-v18.jar"
            ),
            '"\\[Neoforge 1.21.1\\] Better Zoom v2.7.0-v18.jar"',
        )
        self.assertEqual(
            modpack_release.sftp_quote("ordinary file.jar"),
            '"ordinary file.jar"',
        )

    def test_jar_batch_escapes_brackets_in_local_and_remote_paths(self):
        name = "[Neoforge 1.21.1] Better Zoom v2.7.0.jar"
        published_name = (
            "[Neoforge 1.21.1] Better Zoom v2.7.0-v18.jar"
        )
        metadata = {
            "jars": [
                {
                    "name": name,
                    "url": modpack_release.canonical_jar_url(name, 18),
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            metadata_path = temp / "validated.json"
            output_path = temp / "sftp-jars.batch"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            modpack_release.make_sftp_batch(
                metadata_path,
                "/stable",
                output_path,
                "jars",
                "unused-staging",
                "unused-rollback",
            )

            batch = output_path.read_text(encoding="utf-8")

        escaped_source = modpack_release.sftp_quote(
            f"deploy/extracted/mods/{name}"
        )
        escaped_destination = modpack_release.sftp_quote(
            f"/stable/files/mods/{published_name}"
        )
        self.assertIn(
            f"put {escaped_source} {escaped_destination}\n",
            batch,
        )
        self.assertNotIn(f'"deploy/extracted/mods/{name}"', batch)
        self.assertNotIn(
            f'"/stable/files/mods/{published_name}"',
            batch,
        )


if __name__ == "__main__":
    unittest.main()
