# -*- coding: utf-8 -*-
"""Focused tests for launcher update payload integrity helpers."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher


class UpdateIntegrityTests(unittest.TestCase):
    def test_parse_sha256_sidecar_formats(self):
        digest = "a1" * 32
        self.assertEqual(launcher.parse_sha256_sidecar(digest), digest)
        self.assertEqual(
            launcher.parse_sha256_sidecar(
                digest.upper() + "  CheckpointSetup.exe\n"),
            digest,
        )
        self.assertEqual(
            launcher.parse_sha256_sidecar(
                "SHA256 (CheckpointSetup.exe) = " + digest),
            digest,
        )
        self.assertEqual(launcher.parse_sha256_sidecar("not a digest"), "")

    def test_sidecar_url_preserves_cache_buster(self):
        self.assertEqual(
            launcher.update_sha256_sidecar_url(
                "https://example.test/CheckpointSetup.exe?v=1661#ignored"),
            "https://example.test/CheckpointSetup.exe.sha256?v=1661",
        )
        self.assertEqual(launcher.update_sha256_sidecar_url("not-a-url"), "")
        self.assertEqual(
            launcher.update_sha256_sidecar_url(
                "http://example.test/CheckpointSetup.exe"),
            "",
        )

    def test_fetch_sidecar_distinguishes_missing_from_malformed(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return self.body

        digest = "b2" * 32
        with mock.patch.object(
                launcher.urllib.request, "urlopen",
                return_value=Response((digest + "  CheckpointSetup.exe").encode())):
            self.assertEqual(
                launcher.fetch_update_sha256(
                    "https://example.test/CheckpointSetup.exe"),
                digest,
            )
        with mock.patch.object(
                launcher.urllib.request, "urlopen",
                return_value=Response(b"not-a-checksum")):
            self.assertEqual(
                launcher.fetch_update_sha256(
                    "https://example.test/CheckpointSetup.exe"),
                "",
            )
        with mock.patch.object(
                launcher.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(
                launcher.fetch_update_sha256(
                    "https://example.test/CheckpointSetup.exe"))

    def test_installer_verification_requires_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / "CheckpointSetup.exe"
            installer.write_bytes(b"MZ" + (b"IndustrialHorizon" * 190_000))
            self.assertGreater(installer.stat().st_size, 3_000_000)

            digest = launcher.calculate_file_sha256(installer)
            # Executable updates fail closed when the checksum is unavailable.
            self.assertFalse(launcher.verify_update_installer(installer, None))
            self.assertTrue(launcher.verify_update_installer(installer, digest))
            self.assertTrue(
                launcher.verify_update_installer(
                    installer, digest + "  CheckpointSetup.exe"))
            self.assertFalse(
                launcher.verify_update_installer(installer, "0" * 64))
            # A sidecar that exists but is malformed must not disable checking.
            self.assertFalse(launcher.verify_update_installer(installer, ""))

    def test_installer_shape_is_always_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "small.exe"
            small.write_bytes(b"MZ" + b"x" * 100)
            self.assertFalse(launcher.verify_update_installer(small, None))

            not_pe = Path(tmp) / "not-pe.exe"
            not_pe.write_bytes(b"PK" + b"x" * 3_000_100)
            self.assertFalse(launcher.verify_update_installer(not_pe, None))

    def test_github_version_keeps_versioned_asset_and_checksum_paired(self):
        asset_url = (
            "https://github.com/nnacivee/checkpoint-launcher/releases/"
            "download/v1.66.7/CheckpointSetup.exe"
        )
        releases = [{
            "tag_name": "v1.66.7",
            "draft": False,
            "prerelease": False,
            "html_url": "https://github.com/example/release/v1.66.7",
            "assets": [{
                "name": "CheckpointSetup.exe",
                "browser_download_url": asset_url,
            }],
        }]
        digest = "c3" * 32
        with (
            # Keep this fixture newer than the running launcher even after the
            # project version is bumped.  The test exercises URL/checksum
            # pairing, not the current release number.
            mock.patch.dict(
                launcher.CONFIG, {"LAUNCHER_VERSION": "1.66.6"}
            ),
            mock.patch.object(launcher, "_check_update_via_mirror", return_value=None),
            mock.patch.object(launcher, "_modrinth_api_get", return_value=releases),
            mock.patch.object(
                launcher, "fetch_update_sha256", return_value=digest
            ) as fetch_digest,
        ):
            info = launcher.check_for_launcher_update()

        self.assertEqual(info["version"], "1.66.7")
        self.assertEqual(info["exe_url"], asset_url)
        self.assertEqual(info["sha256"], digest)
        fetch_digest.assert_called_once_with(asset_url)


if __name__ == "__main__":
    unittest.main()
