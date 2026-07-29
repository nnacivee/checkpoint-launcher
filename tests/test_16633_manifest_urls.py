import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher


class ManifestUrlReleaseTests(unittest.TestCase):
    @staticmethod
    def _manifest(first_url=...):
        payloads = {
            "core-%02d.jar" % index: ("payload-%02d" % index).encode("ascii")
            for index in range(20)
        }
        files = []
        for name, payload in payloads.items():
            item = {
                "path": "mods/" + name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            if name == "core-00.jar" and first_url is not ...:
                item["url"] = first_url
            files.append(item)
        return {
            "version": 17,
            "modsOnly": True,
            "files": files,
        }, payloads

    def test_safe_relative_url_is_preserved_and_missing_url_is_canonical(self):
        manifest, _payloads = self._manifest(
            "files/mods/core-00-release-v17.jar"
        )
        files = launcher._normalise_modpack_manifest(manifest)
        self.assertEqual(
            files[0]["url"],
            "files/mods/core-00-release-v17.jar",
        )

        manifest, _payloads = self._manifest()
        manifest["files"][0]["path"] = (
            "mods/[Neoforge 1.21.1] Better Zoom v2.7.0.jar"
        )
        files = launcher._normalise_modpack_manifest(manifest)
        self.assertEqual(
            files[0]["url"],
            "files/mods/%5BNeoforge%201.21.1%5D%20Better%20Zoom%20v2.7.0.jar",
        )

    def test_unsafe_relative_urls_invalidate_the_whole_manifest(self):
        unsafe_urls = (
            "",
            None,
            "https://evil.example/files/mods/core.jar",
            "//evil.example/files/mods/core.jar",
            "/files/mods/core.jar",
            "files\\mods\\core.jar",
            "files/mods/../core.jar",
            "files/mods/%2e%2e.jar",
            "files/mods/sub/core.jar",
            "files/mods/core%2fextra.jar",
            "files/mods/core%5cextra.jar",
            "files/mods/%252e%252e.jar",
            "files/mods/core.jar?download=1",
            "files/mods/core.jar#fragment",
            "files/mods/core.txt",
        )
        for unsafe_url in unsafe_urls:
            with self.subTest(url=unsafe_url):
                manifest, _payloads = self._manifest(unsafe_url)
                self.assertEqual(
                    launcher._normalise_modpack_manifest(manifest),
                    [],
                )

    def test_delta_download_uses_manifest_owned_relative_url(self):
        custom_url = "files/mods/core-00-release-v17.jar"
        manifest, payloads = self._manifest(custom_url)
        target_name = "core-00.jar"
        target_payload = payloads[target_name]

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            for name, payload in payloads.items():
                if name != target_name:
                    (mods / name).write_bytes(payload)

            downloaded = []

            def fake_download(urls, destination, **_kwargs):
                downloaded.append(list(urls))
                destination.write_bytes(target_payload)

            response = mock.MagicMock()
            response.__enter__.return_value.headers = {
                "Content-Length": str(len(target_payload))
            }
            with (
                mock.patch.multiple(
                    launcher,
                    INSTANCE_DIR=instance,
                    MODPACK_VERSION_FILE=instance / ".modpack_version",
                ),
                mock.patch.object(
                    launcher, "recover_interrupted_modpack_update"
                ),
                mock.patch.object(
                    launcher, "_fetch_modpack_manifest", return_value=manifest
                ),
                mock.patch.object(
                    launcher, "get_local_modpack_version", return_value=16
                ),
                mock.patch.object(
                    launcher, "get_remote_modpack_version", return_value=17
                ),
                mock.patch.object(launcher, "_sha_index_load", return_value={}),
                mock.patch.object(launcher, "_sha_index_save"),
                mock.patch.object(launcher, "_check_installation_preconditions"),
                mock.patch.object(launcher, "_cache_modpack_manifest"),
                mock.patch.object(
                    launcher.urllib.request,
                    "urlopen",
                    return_value=response,
                ) as urlopen,
                mock.patch.object(
                    launcher, "_download_first", side_effect=fake_download
                ),
            ):
                applied = launcher.install_modpack_delta(
                    lambda _text: None,
                    lambda _percent: None,
                )

            expected_url = (
                "https://industrialhorizon.b-cdn.net/stable/"
                + custom_url
            )
            self.assertTrue(applied)
            self.assertEqual(
                urlopen.call_args.args[0].full_url,
                expected_url,
            )
            self.assertEqual(downloaded, [[expected_url]])
            self.assertEqual(
                (mods / target_name).read_bytes(),
                target_payload,
            )

    def test_almost_unified_materials_is_configpack_immutable(self):
        self.assertTrue(
            launcher._is_configpack_immutable_path(
                "config/almostunified/unification/materials.json"
            )
        )

    def test_github_release_does_not_depend_on_bunny_credentials(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "build.yml"
        ).read_text(encoding="utf-8")
        deploy_job, release_job = workflow.split("  release:", 1)
        deploy_job = deploy_job.split("  deploy-mirror:", 1)[1]
        self.assertIn("needs: [build, release]", deploy_job)
        self.assertIn("\n    needs: build\n", release_job)
        self.assertNotIn("needs: [build, deploy-mirror]", release_job)


if __name__ == "__main__":
    unittest.main()
