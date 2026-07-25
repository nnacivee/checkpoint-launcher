import hashlib
import io
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import launcher


def _jar_bytes(mod_id="test_mod"):
    output = io.BytesIO()
    metadata = (
        'modLoader="javafml"\n'
        'loaderVersion="[4,)"\n'
        'license="All Rights Reserved"\n'
        '[[mods]]\n'
        f'modId="{mod_id}"\n'
        'version="1.0.0"\n'
        f'displayName="{mod_id}"\n'
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as jar:
        jar.writestr("META-INF/neoforge.mods.toml", metadata)
        jar.writestr("assets/test/payload.bin", bytes(range(256)) * 8)
    return output.getvalue()


def _modrinth_metadata(data):
    return {
        "size": len(data),
        "hashes": {
            "sha512": hashlib.sha512(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
        },
        "source": "modrinth",
    }


class ExtraModIntegrityTests(unittest.TestCase):
    def _install_patches(self, root, entries):
        return (
            mock.patch.object(launcher, "APP_DATA_DIR", root / "app"),
            mock.patch.object(launcher, "INSTANCE_DIR", root / "instance"),
            mock.patch.dict(
                launcher.CONFIG,
                {
                    "EXTRA_CLIENT_MODS": entries,
                    "MC_VERSION": "1.21.1",
                    "MOD_LOADER": "neoforge",
                    "MOD_MIRROR_BASE": "https://mirror.example/mods/",
                    "REMOVED_MODS": [],
                },
                clear=False,
            ),
        )

    def test_modrinth_lookup_keeps_two_tuple_and_optionally_returns_integrity(self):
        data = _jar_bytes()
        file_info = {
            "filename": "test.jar",
            "url": "https://cdn.modrinth.com/test.jar",
            "primary": True,
            "size": len(data),
            "hashes": {
                "sha512": hashlib.sha512(data).hexdigest().upper(),
                "sha1": hashlib.sha1(data).hexdigest().upper(),
            },
        }
        versions = [{
            "loaders": ["neoforge"],
            "files": [file_info],
        }]
        with mock.patch.object(
            launcher, "_modrinth_api_get", return_value=versions
        ):
            legacy = launcher._find_modrinth_download(
                "test", "1.21.1", ["neoforge"]
            )
            detailed = launcher._find_modrinth_download(
                "test", "1.21.1", ["neoforge"], include_metadata=True
            )

        self.assertEqual(
            legacy, ("test.jar", "https://cdn.modrinth.com/test.jar")
        )
        self.assertEqual(len(legacy), 2)
        self.assertEqual(detailed[:2], legacy)
        self.assertEqual(detailed[2]["size"], len(data))
        self.assertEqual(
            detailed[2]["hashes"]["sha512"],
            hashlib.sha512(data).hexdigest(),
        )
        self.assertEqual(
            detailed[2]["hashes"]["sha1"], hashlib.sha1(data).hexdigest()
        )

    def test_direct_modrinth_url_resolves_exact_version_and_filename_metadata(self):
        wanted = _jar_bytes("wanted")
        other = _jar_bytes("other")
        version = {
            "files": [
                {
                    "filename": "other.jar",
                    "size": len(other),
                    "hashes": {
                        "sha512": hashlib.sha512(other).hexdigest(),
                        "sha1": hashlib.sha1(other).hexdigest(),
                    },
                },
                {
                    "filename": "wanted+file.jar",
                    "size": len(wanted),
                    "hashes": {
                        "sha512": hashlib.sha512(wanted).hexdigest().upper(),
                        "sha1": hashlib.sha1(wanted).hexdigest().upper(),
                    },
                },
            ],
        }
        url = (
            "https://cdn.modrinth.com/data/project-id/versions/ExactV123/"
            "wanted%2Bfile.jar"
        )

        with mock.patch.object(
            launcher, "_modrinth_api_get", return_value=version
        ) as api_get:
            metadata = launcher._modrinth_metadata_for_direct_url(
                url, "wanted+file.jar"
            )

        api_get.assert_called_once_with(
            "https://api.modrinth.com/v2/version/ExactV123",
            timeout=8,
        )
        self.assertEqual(metadata["size"], len(wanted))
        self.assertEqual(
            metadata["hashes"],
            {
                "sha512": hashlib.sha512(wanted).hexdigest(),
                "sha1": hashlib.sha1(wanted).hexdigest(),
            },
        )
        self.assertEqual(metadata["source"], "modrinth")

    def test_explicit_modrinth_url_resolves_integrity_before_download(self):
        data = _jar_bytes()
        metadata = _modrinth_metadata(data)
        sequence = []
        calls = []
        url = (
            "https://cdn.modrinth.com/data/project-id/versions/ExactV123/"
            "test.jar"
        )
        entry = {
            "slug": "test",
            "label": "Required Test",
            "required": True,
            "url": url,
            "filename": "test.jar",
        }

        def resolve(resolved_url, filename):
            sequence.append("resolve")
            self.assertEqual(resolved_url, url)
            self.assertEqual(filename, "test.jar")
            return metadata

        def download(download_url, target, progress_cb=None, **kwargs):
            sequence.append("download")
            calls.append((download_url, kwargs))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if progress_cb:
                progress_cb(100)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._install_patches(root, [entry])
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    launcher,
                    "_modrinth_metadata_for_direct_url",
                    side_effect=resolve,
                ),
                mock.patch.object(
                    launcher, "download_file", side_effect=download
                ),
            ):
                missing = launcher.install_extra_client_mods()

        self.assertEqual(missing, [])
        self.assertEqual(sequence, ["resolve", "download"])
        self.assertEqual(calls[0][0], url)
        self.assertEqual(calls[0][1]["expected_size"], len(data))

    def test_explicit_modrinth_url_without_resolved_hash_never_downloads(self):
        entry = {
            "slug": "test",
            "label": "Required Test",
            "required": True,
            "url": (
                "https://cdn.modrinth.com/data/project-id/versions/ExactV123/"
                "test.jar"
            ),
            "filename": "test.jar",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._install_patches(root, [entry])
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    launcher,
                    "_modrinth_metadata_for_direct_url",
                    return_value={},
                ) as resolve,
                mock.patch.object(launcher, "download_file") as download,
            ):
                missing = launcher.install_extra_client_mods()

        self.assertEqual(missing, ["Required Test"])
        resolve.assert_called_once()
        download.assert_not_called()

    def test_bunny_without_valid_sidecar_is_skipped_for_official_modrinth(self):
        data = _jar_bytes()
        urls = []
        download_kwargs = []
        entry = {
            "slug": "test",
            "label": "Test",
            "required": True,
            "mirror": True,
        }

        def download(url, target, progress_cb=None, **kwargs):
            urls.append(url)
            download_kwargs.append(kwargs)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if progress_cb:
                progress_cb(100)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._install_patches(root, [entry])
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    launcher,
                    "_find_modrinth_download",
                    return_value=(
                        "test.jar",
                        "https://cdn.modrinth.com/test.jar",
                        _modrinth_metadata(data),
                    ),
                ),
                mock.patch.object(
                    launcher, "_fetch_tiny_text", return_value="not-a-digest"
                ),
                mock.patch.object(
                    launcher, "download_file", side_effect=download
                ),
            ):
                missing = launcher.install_extra_client_mods()

        self.assertEqual(missing, [])
        self.assertEqual(urls, ["https://cdn.modrinth.com/test.jar"])
        self.assertEqual(download_kwargs[0]["expected_size"], len(data))

    def test_bunny_with_valid_sidecar_is_used_and_sha256_is_required(self):
        data = _jar_bytes()
        digest = hashlib.sha256(data).hexdigest()
        calls = []
        entry = {
            "slug": "test",
            "label": "Test",
            "required": True,
            "mirror": True,
        }

        def download(url, target, progress_cb=None, **kwargs):
            calls.append((url, kwargs.get("expected_sha256")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if progress_cb:
                progress_cb(100)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._install_patches(root, [entry])
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    launcher,
                    "_find_modrinth_download",
                    return_value=(
                        "test.jar",
                        "https://cdn.modrinth.com/test.jar",
                        _modrinth_metadata(data),
                    ),
                ),
                mock.patch.object(
                    launcher, "_fetch_tiny_text", return_value=digest
                ),
                mock.patch.object(
                    launcher, "download_file", side_effect=download
                ),
            ):
                missing = launcher.install_extra_client_mods()

        self.assertEqual(missing, [])
        self.assertEqual(
            calls,
            [("https://mirror.example/mods/test.jar", digest)],
        )

    def test_official_modrinth_file_must_match_every_published_hash(self):
        data = _jar_bytes()
        metadata = _modrinth_metadata(data)
        metadata["hashes"]["sha1"] = "0" * 40
        entry = {
            "slug": "test",
            "label": "Required Test",
            "required": True,
        }

        def download(_url, target, progress_cb=None, **_kwargs):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if progress_cb:
                progress_cb(100)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._install_patches(root, [entry])
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    launcher,
                    "_find_modrinth_download",
                    return_value=(
                        "test.jar",
                        "https://cdn.modrinth.com/test.jar",
                        metadata,
                    ),
                ),
                mock.patch.object(
                    launcher, "download_file", side_effect=download
                ),
            ):
                missing = launcher.install_extra_client_mods()
                cached = root / "app" / "extra_client_mods_cache" / "test.jar"

            self.assertEqual(missing, ["Required Test"])
            self.assertFalse(cached.exists())

    def test_parallel_download_progress_is_live_aggregate_and_monotonic(self):
        data_a = _jar_bytes("mod_a")
        data_b = _jar_bytes("mod_b")
        payloads = {
            "https://official.example/a.jar": data_a,
            "https://official.example/b.jar": data_b,
        }
        entries = [
            {
                "slug": "a",
                "label": "A",
                "required": True,
                "url": "https://official.example/a.jar",
                "filename": "a.jar",
                "size": len(data_a),
            },
            {
                "slug": "b",
                "label": "B",
                "required": True,
                "url": "https://official.example/b.jar",
                "filename": "b.jar",
                "size": len(data_b),
            },
        ]
        rendezvous = threading.Barrier(3)
        progress = []
        status = []

        def download(url, target, progress_cb=None, **_kwargs):
            progress_cb(50)
            rendezvous.wait(timeout=2.0)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payloads[url])
            progress_cb(100)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._install_patches(root, entries)
            result = {}
            with (
                patches[0],
                patches[1],
                patches[2],
                mock.patch.object(
                    launcher, "download_file", side_effect=download
                ),
            ):
                worker = threading.Thread(
                    target=lambda: result.setdefault(
                        "missing",
                        launcher.install_extra_client_mods(
                            status.append, progress.append
                        ),
                    )
                )
                worker.start()
                rendezvous.wait(timeout=2.0)
                worker.join(timeout=5.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result["missing"], [])
        self.assertIn(25, progress)
        self.assertIn(50, progress)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(progress, sorted(progress))
        self.assertTrue(any("50%" in text for text in status))


if __name__ == "__main__":
    unittest.main()
