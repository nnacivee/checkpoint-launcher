import hashlib
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import launcher


class _FakeResponse:
    def __init__(self, body=b"", *, status=200, headers=None):
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = dict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self._body.read(size)


class _TrickleResponse(_FakeResponse):
    def __init__(self, *, total=10_000, max_reads=20):
        super().__init__(
            status=200,
            headers={"Content-Length": str(total)},
        )
        self.read_calls = 0
        self.max_reads = max_reads

    def read(self, size=-1):
        self.read_calls += 1
        if self.read_calls <= self.max_reads:
            return b"x"
        return b""


class DownloadResilienceTests(unittest.TestCase):
    def test_parse_content_range_accepts_exact_byte_ranges(self):
        cases = {
            "bytes 0-9/10": (0, 9, 10),
            "bytes 10-19/*": (10, 19, None),
            "  BYTES  7-7/8  ": (7, 7, 8),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(launcher._parse_content_range(raw), expected)

    def test_parse_content_range_rejects_malformed_values(self):
        for raw in (
            None,
            "",
            "0-9/10",
            "items 0-9/10",
            "bytes */10",
            "bytes 0-9",
            "bytes -9/10",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(launcher._parse_content_range(raw))

    def test_serial_resume_requires_and_commits_exact_206_range(self):
        payload = b"0123456789abcdef"
        prefix = payload[:6]
        response = _FakeResponse(
            payload[len(prefix):],
            status=206,
            headers={
                "Content-Range": "bytes 6-15/16",
                "Content-Length": "10",
            },
        )
        requests = []

        def open_url(request, timeout):
            requests.append((request, timeout))
            return response

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            dest.with_name(dest.name + ".part").write_bytes(prefix)
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request, "urlopen", side_effect=open_url
                ),
            ):
                launcher.download_file(
                    "https://cdn.example/artifact.bin",
                    dest,
                    retries=1,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)
            self.assertFalse(dest.with_name(dest.name + ".part").exists())
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0].get_header("Range"), "bytes=6-")

    def test_short_body_is_resumed_instead_of_downloaded_again(self):
        payload = b"0123456789abcdef"
        prefix = payload[:6]
        responses = [
            _FakeResponse(
                prefix,
                status=200,
                headers={"Content-Length": str(len(payload))},
            ),
            _FakeResponse(
                payload[len(prefix):],
                status=206,
                headers={
                    "Content-Range": "bytes 6-15/16",
                    "Content-Length": "10",
                },
            ),
        ]
        requests = []

        def open_url(request, timeout):
            requests.append(request)
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request, "urlopen", side_effect=open_url
                ),
                mock.patch.object(launcher.time, "sleep"),
            ):
                launcher.download_file(
                    "https://cdn.example/artifact.bin",
                    dest,
                    retries=2,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(len(requests), 2)
            self.assertIsNone(requests[0].get_header("Range"))
            self.assertEqual(requests[1].get_header("Range"), "bytes=6-")

    def test_range_ignored_with_200_restarts_from_zero(self):
        payload = b"complete-payload"
        requests = []

        def open_url(request, timeout):
            requests.append(request)
            return _FakeResponse(
                payload,
                status=200,
                headers={"Content-Length": str(len(payload))},
            )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            dest.with_name(dest.name + ".part").write_bytes(b"stale-prefix")
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request, "urlopen", side_effect=open_url
                ),
            ):
                launcher.download_file(
                    "https://cdn.example/artifact.bin",
                    dest,
                    retries=1,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(requests[0].get_header("Range"), "bytes=12-")

    def test_wrong_content_range_clears_partial_and_preserves_good_dest(self):
        payload = b"0123456789"
        previous = b"previous-good-version"
        response = _FakeResponse(
            payload[4:],
            status=206,
            headers={
                "Content-Range": "bytes 0-9/10",
                "Content-Length": "6",
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            dest.write_bytes(previous)
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(payload[:4])
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request, "urlopen", return_value=response
                ),
            ):
                with self.assertRaises(RuntimeError):
                    launcher.download_file(
                        "https://cdn.example/artifact.bin",
                        dest,
                        retries=1,
                        expected_size=len(payload),
                    )

            self.assertEqual(dest.read_bytes(), previous)
            self.assertFalse(part.exists())

    def test_stale_416_clears_partial_then_retries_clean(self):
        payload = b"fresh-complete-payload"
        requests = []
        range_error = urllib.error.HTTPError(
            "https://cdn.example/artifact.bin",
            416,
            "Range Not Satisfiable",
            hdrs=None,
            fp=None,
        )
        responses = [
            range_error,
            _FakeResponse(
                payload,
                status=200,
                headers={"Content-Length": str(len(payload))},
            ),
        ]

        def open_url(request, timeout):
            requests.append(request)
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            dest.with_name(dest.name + ".part").write_bytes(b"stale")
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request, "urlopen", side_effect=open_url
                ),
                mock.patch.object(launcher.time, "sleep"),
            ):
                launcher.download_file(
                    "https://cdn.example/artifact.bin",
                    dest,
                    retries=2,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0].get_header("Range"), "bytes=5-")
            self.assertIsNone(requests[1].get_header("Range"))
        range_error.close()

    def test_sha_mismatch_never_replaces_existing_destination(self):
        expected_payload = b"expected"
        corrupt_payload = b"corrupt!"
        previous = b"last-known-good"

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            dest.write_bytes(previous)
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request,
                    "urlopen",
                    return_value=_FakeResponse(
                        corrupt_payload,
                        headers={
                            "Content-Length": str(len(corrupt_payload)),
                        },
                    ),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    launcher.download_file(
                        "https://cdn.example/artifact.bin",
                        dest,
                        retries=1,
                        expected_size=len(corrupt_payload),
                        expected_sha256=hashlib.sha256(
                            expected_payload
                        ).hexdigest(),
                    )

            self.assertEqual(dest.read_bytes(), previous)
            self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_complete_sha_valid_part_commits_without_network(self):
        payload = b"already-downloaded-and-verified"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(payload)
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ) as parallel_download,
                mock.patch.object(
                    launcher.urllib.request, "urlopen"
                ) as open_url,
            ):
                launcher.download_file(
                    "https://cdn.example/artifact.bin",
                    dest,
                    retries=2,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)
            self.assertFalse(part.exists())
            parallel_download.assert_not_called()
            open_url.assert_not_called()

    def test_serial_local_file_source_is_treated_as_complete_response(self):
        payload = b"developer-configpack"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.zip"
            dest = Path(tmp) / "artifact.zip"
            source.write_bytes(payload)
            with mock.patch.object(
                launcher, "_download_parallel", return_value=False
            ):
                launcher.download_file(
                    source.resolve().as_uri(),
                    dest,
                    retries=1,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)

    def test_temporary_replace_failure_retries_commit_without_redownload(self):
        payload = b"downloaded-once"
        response = _FakeResponse(
            payload,
            headers={"Content-Length": str(len(payload))},
        )
        real_replace = launcher.os.replace
        replace_calls = []

        def replace_once_locked(source, target):
            replace_calls.append((Path(source), Path(target)))
            if len(replace_calls) == 1:
                raise PermissionError("simulated antivirus lock")
            return real_replace(source, target)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request,
                    "urlopen",
                    return_value=response,
                ) as open_url,
                mock.patch.object(
                    launcher.os, "replace", side_effect=replace_once_locked
                ),
                mock.patch.object(launcher.time, "sleep"),
            ):
                launcher.download_file(
                    "https://cdn.example/artifact.bin",
                    dest,
                    retries=2,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(open_url.call_count, 1)
            self.assertEqual(len(replace_calls), 2)

    def test_mirror_fallback_preserves_completed_verified_part(self):
        payload = b"complete-payload-waiting-for-rename"
        digest = hashlib.sha256(payload).hexdigest()
        calls = []
        progress = []

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            part = dest.with_name(dest.name + ".part")

            def fake_download(url, target, *args, **kwargs):
                calls.append(url)
                report = args[0] if args else kwargs.get("progress_cb")
                if len(calls) == 1:
                    report(94)
                    part.write_bytes(payload)
                    raise RuntimeError("target temporarily locked")
                self.assertTrue(part.exists())
                for value in (0, 50, 100):
                    report(value)
                launcher.os.replace(part, target)

            with mock.patch.object(
                launcher, "download_file", side_effect=fake_download
            ):
                launcher.download_with_mirror(
                    "https://origin.example/artifact.bin",
                    "https://mirror.example/artifact.bin",
                    dest,
                    progress_cb=progress.append,
                    expected_size=len(payload),
                    expected_sha256=digest,
                )

            self.assertEqual(calls, [
                "https://mirror.example/artifact.bin",
                "https://origin.example/artifact.bin",
            ])
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(progress, sorted(progress))
            self.assertEqual(progress[-1], 100)

    def test_parallel_state_cleanup_after_success_is_best_effort(self):
        payload = b"valid-payload"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            state_dir = launcher._parallel_state_dir(dest)
            state_dir.mkdir()
            (state_dir / "stale.part").write_bytes(b"stale")
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request,
                    "urlopen",
                    return_value=_FakeResponse(
                        payload,
                        headers={"Content-Length": str(len(payload))},
                    ),
                ),
                mock.patch.object(
                    launcher.shutil,
                    "rmtree",
                    side_effect=PermissionError("simulated scanner lock"),
                ),
            ):
                launcher.download_file(
                    "https://cdn.example/artifact.bin",
                    dest,
                    retries=1,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )

            self.assertEqual(dest.read_bytes(), payload)

    def test_serial_206_rejects_content_length_range_span_mismatch(self):
        prefix = b"0123"
        previous = b"last-known-good"
        response = _FakeResponse(
            b"45678",
            status=206,
            headers={
                # The range promises six bytes, while Content-Length/body
                # contain only five.  Matching start/total alone is not enough.
                "Content-Range": "bytes 4-9/10",
                "Content-Length": "5",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            dest.write_bytes(previous)
            dest.with_name(dest.name + ".part").write_bytes(prefix)
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request,
                    "urlopen",
                    return_value=response,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    launcher.download_file(
                        "https://cdn.example/artifact.bin",
                        dest,
                        retries=1,
                    )

            self.assertEqual(dest.read_bytes(), previous)

    def test_serial_request_without_range_rejects_unexpected_partial_206(self):
        previous = b"last-known-good"
        response = _FakeResponse(
            b"01234",
            status=206,
            headers={
                "Content-Range": "bytes 0-4/10",
                "Content-Length": "5",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            dest.write_bytes(previous)
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request,
                    "urlopen",
                    return_value=response,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    launcher.download_file(
                        "https://cdn.example/artifact.bin",
                        dest,
                        retries=1,
                    )

            self.assertEqual(dest.read_bytes(), previous)

    def test_serial_trickle_obeys_wall_clock_deadline(self):
        response = _TrickleResponse(total=10_000, max_reads=20)
        clock = {"now": -100.0}

        def monotonic():
            clock["now"] += 100.0
            return clock["now"]

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request,
                    "urlopen",
                    return_value=response,
                ),
                mock.patch.object(
                    launcher.time, "monotonic", side_effect=monotonic
                ),
            ):
                with self.assertRaises(RuntimeError):
                    launcher.download_file(
                        "https://cdn.example/trickle.bin",
                        dest,
                        retries=1,
                        deadline_seconds=200,
                    )

            self.assertLessEqual(
                response.read_calls,
                5,
                "wall-clock deadline did not stop a continuously trickling body",
            )

    def test_large_pack_deadline_allows_slow_but_healthy_connection(self):
        # 233 MiB at the reported ~84 KiB/s needs roughly 45 minutes.
        self.assertGreater(
            launcher._download_deadline_seconds(233_366_069),
            45 * 60,
        )

    def test_progress_reporter_is_monotonic_and_percent_throttled(self):
        seen = []
        report = launcher._progress_reporter(seen.append)

        for done in (0, 1, 9, 10, 19, 18, 20, 20, 999, 1000, 999):
            report(done, 1000)

        self.assertEqual(seen, [0, 1, 2, 99, 100])
        self.assertEqual(seen, sorted(set(seen)))

    def test_permanent_404_is_not_retried(self):
        error = urllib.error.HTTPError(
            "https://cdn.example/missing.bin",
            404,
            "Not Found",
            hdrs=None,
            fp=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifact.bin"
            with (
                mock.patch.object(
                    launcher, "_download_parallel", return_value=False
                ),
                mock.patch.object(
                    launcher.urllib.request, "urlopen", side_effect=error
                ) as open_url,
                mock.patch.object(launcher.time, "sleep") as sleep,
            ):
                with self.assertRaises(RuntimeError):
                    launcher.download_file(
                        "https://cdn.example/missing.bin",
                        dest,
                        retries=5,
                        expected_size=10,
                    )

            self.assertEqual(open_url.call_count, 1)
            sleep.assert_not_called()
        error.close()


if __name__ == "__main__":
    unittest.main()
