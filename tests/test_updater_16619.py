import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import webui


class Updater16619Tests(unittest.TestCase):
    @staticmethod
    def _immediate_thread(target, daemon=True):
        del daemon
        return mock.Mock(start=target)

    def test_version_probe_does_not_add_query_to_bunny(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"1.66.23\n"

        url = (
            "https://industrialhorizon.b-cdn.net/stable/"
            "launcher_version.txt"
        )
        with (
            mock.patch.dict(
                webui.L.CONFIG,
                {"LAUNCHER_VERSION_MIRROR_URL": url},
            ),
            mock.patch.object(
                webui.urllib.request,
                "urlopen",
                return_value=Response(),
            ) as urlopen,
        ):
            self.assertEqual(
                webui.Api._probe_launcher_version_marker(),
                "1.66.23",
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.headers["Cache-control"], "no-cache")

    def test_check_update_has_distinct_current_and_unavailable_contracts(self):
        api = webui.Api()
        with (
            mock.patch.object(webui.sys, "frozen", True, create=True),
            mock.patch.object(
                webui.L, "check_for_launcher_update", return_value=None
            ),
            mock.patch.object(
                api, "_probe_launcher_version_marker", return_value="1.66.18"
            ),
            mock.patch.dict(
                webui.L.CONFIG, {"LAUNCHER_VERSION": "1.66.18"}
            ),
        ):
            current = api.check_update()

        self.assertEqual(current["status"], "current")
        self.assertNotIn("version", current)
        self.assertEqual(api.get_update_check_state()["status"], "current")

        with (
            mock.patch.object(webui.sys, "frozen", True, create=True),
            mock.patch.object(
                webui.L, "check_for_launcher_update", return_value=None
            ),
            mock.patch.object(
                api,
                "_probe_launcher_version_marker",
                side_effect=OSError("offline"),
            ),
            mock.patch.object(
                api, "_load_verified_pending_update", return_value=None
            ),
        ):
            unavailable = api.check_update()

        self.assertEqual(unavailable["status"], "unavailable")
        self.assertIn("offline", unavailable["error"])
        self.assertEqual(
            api.get_update_check_state()["status"], "unavailable"
        )

    def test_available_update_keeps_legacy_shape_and_exact_release(self):
        api = webui.Api()
        release = {
            "version": "1.66.19",
            "exe_url": "https://cdn.example/CheckpointSetup-1.66.19.exe",
            "url": "https://example/releases/v1.66.19",
            "sha256": "a" * 64,
        }
        with (
            mock.patch.object(webui.sys, "frozen", True, create=True),
            mock.patch.object(
                webui.L, "check_for_launcher_update", return_value=release
            ),
        ):
            result = api.check_update()

        self.assertEqual(result, release)
        self.assertEqual(api._pending_update, release)

    def test_apply_update_downloads_detected_release_with_known_digest(self):
        api = webui.Api()
        api._js = mock.Mock()
        detected = {
            "version": "1.66.19",
            "exe_url": "https://cdn.example/CheckpointSetup-1.66.19.exe",
            "sha256": "b" * 64,
        }
        api._pending_update = dict(detected)

        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            popen = mock.Mock()
            with (
                mock.patch.object(webui.sys, "frozen", True, create=True),
                mock.patch.object(
                    webui.L, "APP_DATA_DIR", temp_dir / "state"
                ),
                mock.patch.object(
                    webui.tempfile, "gettempdir", return_value=str(temp_dir)
                ),
                mock.patch.object(
                    webui.L, "check_for_launcher_update"
                ) as redetect,
                mock.patch.object(webui.L, "download_file") as download,
                mock.patch.object(
                    webui.L, "verify_update_installer", return_value=True
                ) as verify,
                mock.patch.object(webui.subprocess, "Popen", popen),
                mock.patch.object(webui.time, "sleep"),
                mock.patch.object(api, "close"),
                mock.patch.object(
                    webui.threading,
                    "Thread",
                    side_effect=self._immediate_thread,
                ),
            ):
                result = api.apply_update()

            self.assertEqual(result, {"ok": True, "started": True})
            redetect.assert_not_called()
            download.assert_called_once()
            download_call = download.call_args
            self.assertEqual(download_call.args[0], detected["exe_url"])
            self.assertEqual(
                download_call.kwargs["expected_sha256"],
                detected["sha256"],
            )
            verify.assert_called_once_with(
                download_call.args[1], detected["sha256"]
            )

            helper = temp_dir / ("ih_update_%d.bat" % webui.os.getpid())
            script = helper.read_text(encoding="ascii")
            self.assertIn(":wait_for_launcher", script)
            self.assertIn('set "WAIT_LEFT=120"', script)
            self.assertIn("goto parent_timeout", script)
            self.assertIn(
                "echo error:parent_timeout:%VERSION%", script
            )
            self.assertIn('start "" /wait "%INSTALLER%"', script)
            self.assertEqual(script.count("call :run_installer"), 2)
            self.assertIn('echo error:%RC%:%VERSION%', script)
            self.assertIn('echo ok:%VERSION%', script)
            self.assertLess(
                script.index(":success"),
                script.index('del "%INSTALLER%"'),
            )

            command = popen.call_args.args[0]
            environment = popen.call_args.kwargs["env"]
            self.assertTrue(command.startswith('cmd.exe /d /s /c "'))
            self.assertTrue(command.endswith('"'))
            self.assertNotIn(str(temp_dir), command)
            self.assertEqual(
                environment["IH_UPDATER_ARG_1"],
                str(webui.os.getpid()),
            )
            self.assertEqual(
                environment["IH_UPDATER_ARG_3"], detected["version"]
            )
            self.assertTrue(
                any(
                    "updBanner('install', 100)" in call.args[0]
                    for call in api._js.call_args_list
                )
            )

    @unittest.skipUnless(os.name == "nt", "Windows cmd quoting")
    def test_batch_command_executes_from_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(
            prefix="ih updater & (space) "
        ) as tmp:
            root = Path(tmp)
            script = root / "helper & (space).bat"
            output = root / "result & (space).txt"
            installer = root / "installer & (space).exe"
            installer.write_bytes(b"test")
            script.write_text(
                "@echo off\r\n"
                'set "INSTALLER=%~1"\r\n'
                'if not exist "%INSTALLER%" exit /b 9\r\n'
                '> "%~2" echo ok\r\n'
                "exit /b 0\r\n",
                encoding="ascii",
            )
            command, environment = webui.Api._cmd_batch_command(
                script, installer, output
            )
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(encoding="ascii").strip(),
                "ok",
            )

    def test_apply_update_refuses_to_redetect_or_apply_without_known_sha(self):
        api = webui.Api()
        api._pending_update = {
            "version": "1.66.19",
            "exe_url": "https://cdn.example/CheckpointSetup-1.66.19.exe",
        }
        with (
            mock.patch.object(webui.sys, "frozen", True, create=True),
            mock.patch.object(
                webui.L, "check_for_launcher_update"
            ) as redetect,
            mock.patch.object(webui.threading, "Thread") as thread,
        ):
            result = api.apply_update()

        self.assertFalse(result["ok"])
        self.assertFalse(result["started"])
        redetect.assert_not_called()
        thread.assert_not_called()

    def test_verified_installer_is_reused_after_a_previous_apply_failure(self):
        api = webui.Api()
        api._pending_update = {
            "version": "1.66.19",
            "exe_url": "https://cdn.example/CheckpointSetup-1.66.19.exe",
            "sha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            installer = temp_dir / "CheckpointSetup_1.66.19.exe"
            installer.write_bytes(b"verified installer")
            with (
                mock.patch.object(webui.sys, "frozen", True, create=True),
                mock.patch.object(
                    webui.L, "APP_DATA_DIR", temp_dir / "state"
                ),
                mock.patch.object(
                    webui.tempfile, "gettempdir", return_value=str(temp_dir)
                ),
                mock.patch.object(
                    webui.L, "verify_update_installer", return_value=True
                ) as verify,
                mock.patch.object(webui.L, "download_file") as download,
                mock.patch.object(webui.subprocess, "Popen"),
                mock.patch.object(webui.time, "sleep"),
                mock.patch.object(api, "close"),
                mock.patch.object(
                    webui.threading,
                    "Thread",
                    side_effect=self._immediate_thread,
                ),
            ):
                result = api.apply_update()

        self.assertEqual(result, {"ok": True, "started": True})
        download.assert_not_called()
        self.assertEqual(verify.call_count, 2)

    def test_update_helper_result_is_consumed_on_next_start(self):
        api = webui.Api()
        events = []
        api._toast = lambda text, kind="ok": events.append(
            ("toast", text, kind)
        )
        api._js = lambda code: events.append(("js", code))

        with tempfile.TemporaryDirectory() as tmp:
            result_file = Path(tmp) / "launcher_update_result.txt"
            result_file.write_text(
                "error:5:1.66.19\r\n", encoding="ascii"
            )
            with mock.patch.object(
                api, "_update_result_file", return_value=result_file
            ):
                result = api._consume_update_result()

            self.assertEqual(
                result,
                {"status": "error", "code": "5", "version": "1.66.19"},
            )
            self.assertFalse(result_file.exists())

        self.assertEqual(events, [])

    def test_failed_apply_offers_verified_cached_installer_for_retry(self):
        api = webui.Api()
        release = {
            "version": "1.66.19",
            "exe_url": "https://cdn.example/CheckpointSetup-1.66.19.exe",
            "sha256": "d" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            state_dir = temp_dir / "state"
            installer = temp_dir / "CheckpointSetup_1.66.19.exe"
            installer.write_bytes(b"verified installer")
            result_file = state_dir / "launcher_update_result.txt"
            state_dir.mkdir(parents=True)
            result_file.write_text(
                "error:5:1.66.19\r\n", encoding="ascii"
            )
            pending_file = state_dir / "launcher_update_pending.json"
            pending_file.write_text(
                webui.json.dumps(release), encoding="utf-8"
            )
            with (
                mock.patch.object(webui.sys, "frozen", True, create=True),
                mock.patch.object(
                    webui.L, "APP_DATA_DIR", state_dir
                ),
                mock.patch.object(
                    webui.tempfile, "gettempdir", return_value=str(temp_dir)
                ),
                mock.patch.dict(
                    webui.L.CONFIG, {"LAUNCHER_VERSION": "1.66.18"}
                ),
                mock.patch.object(
                    webui.L, "verify_update_installer", return_value=True
                ),
                mock.patch.object(
                    webui.L, "check_for_launcher_update"
                ) as network_check,
            ):
                result = api.check_update()

        self.assertEqual(result["version"], "1.66.19")
        self.assertTrue(result["retry"])
        self.assertEqual(api._pending_update["sha256"], "d" * 64)
        network_check.assert_not_called()

    def test_repair_uses_safe_backend_and_finishes_before_complete(self):
        api = webui.Api()
        states = []
        api._repair_state = (
            lambda state, text="", progress=None:
            states.append((state, text, progress))
        )
        with (
            mock.patch.object(
                webui.L, "get_active_game_session", return_value=None
            ),
            mock.patch.object(
                webui.L,
                "repair_client",
                return_value={"ok": True, "detail": "Файлы готовы"},
                create=True,
            ) as repair,
            mock.patch.object(webui.L, "repair_installation") as legacy,
            mock.patch.object(
                webui.threading,
                "Thread",
                side_effect=self._immediate_thread,
            ),
        ):
            result = api.repair()

        self.assertEqual(result, {"ok": True, "started": True})
        repair.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual(states[-1], ("complete", "Файлы готовы", 100))
        self.assertEqual(
            api.get_maintenance_state()["state"], "complete"
        )
        self.assertFalse(api.get_maintenance_state()["busy"])

    def test_repair_polling_fallback_preserves_backend_failure(self):
        api = webui.Api()
        api._repair_state = lambda *_args, **_kwargs: None
        with (
            mock.patch.object(
                webui.L, "get_active_game_session", return_value=None
            ),
            mock.patch.object(
                webui.L,
                "repair_client",
                side_effect=RuntimeError("network unavailable"),
                create=True,
            ),
            mock.patch.object(
                webui.threading,
                "Thread",
                side_effect=self._immediate_thread,
            ),
        ):
            result = api.repair()

        state = api.get_maintenance_state()
        self.assertEqual(result, {"ok": True, "started": True})
        self.assertFalse(state["busy"])
        self.assertEqual(state["state"], "error")
        self.assertEqual(state["error"], "network unavailable")


if __name__ == "__main__":
    unittest.main()
