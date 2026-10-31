"""Update source guards and persistent incomplete state, using temporary profiles."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_source = str(Path(__file__).with_name("dsh-plugin-updater.pyw"))
_spec = importlib.util.spec_from_loader(
    "safety_updater", importlib.machinery.SourceFileLoader("safety_updater", _source))
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)


class TestUpdateSafety(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.profile = self.home / "profiles" / "desktop"
        self.profile.mkdir(parents=True)
        self.manifest = self.profile / "package.json"
        self.pending = self.profile / ".plugin-updater-pending.json"
        self.expected = {"demo-plugin": "^1.0.0"}
        self.write_dependencies(self.expected)
        self.write_installed("1.0.0")
        for name, value in (("dsh_home", str(self.home)), ("find_dsh", "dsh.cmd")):
            mock = patch.object(updater, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        profile = patch.object(updater, "PROFILE", "desktop")
        profile.start()
        self.addCleanup(profile.stop)
        self.logs = []

    def write_dependencies(self, dependencies):
        self.manifest.write_text(json.dumps({"dependencies": dependencies}), encoding="utf-8")

    def write_installed(self, version):
        manifest = self.profile / "node_modules" / "demo-plugin" / "package.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"version": version}), encoding="utf-8")

    def run_update(self, version="1.1.0"):
        return updater.run_update([("demo-plugin", version)], self.logs.append, expected=self.expected)

    def test_changed_sources_or_removed_dependency_never_run_command(self):
        for dependencies in ({"demo-plugin": "file:../custom"}, {"demo-plugin": "^2.0.0"}, {}):
            with self.subTest(dependencies=dependencies):
                self.write_dependencies(dependencies)
                with patch.object(updater.subprocess, "run") as run:
                    self.assertEqual(self.run_update(), ([], [], ["demo-plugin"]))
                run.assert_not_called()
                self.assertFalse(self.pending.exists())

    def test_local_source_is_rejected_even_when_expected_matches(self):
        self.expected = {"demo-plugin": "file:../custom"}
        self.write_dependencies(self.expected)
        with patch.object(updater.subprocess, "run") as run:
            self.assertEqual(self.run_update(), ([], [], ["demo-plugin"]))
        run.assert_not_called()
        self.assertFalse(self.pending.exists())

    def failed_install(self, *args, **kwargs):
        self.assertEqual(updater.pending_updates(), {"demo-plugin": "1.1.0"})
        self.write_installed("1.1.0")
        return SimpleNamespace(returncode=1, stdout="ERR_PNPM_IGNORED_BUILDS", stderr="")

    def test_failed_install_survives_repeated_checks(self):
        with patch.object(updater.subprocess, "run", side_effect=self.failed_install):
            self.assertEqual(self.run_update(), ([], ["demo-plugin"], []))
        for _ in range(2):
            with patch.object(updater, "fetch_latest") as fetch:
                rows = updater.check_all()
            self.assertEqual(rows[0]["state"], "incomplete")
            self.assertEqual(rows[0]["latest"], "1.1.0")
            fetch.assert_not_called()
        self.assertEqual(json.loads(self.pending.read_text(encoding="utf-8")), {"demo-plugin": "1.1.0"})

    def test_successful_retry_clears_pending(self):
        with patch.object(updater.subprocess, "run", side_effect=self.failed_install):
            self.run_update()
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            self.assertEqual(self.run_update(), (["demo-plugin"], [], []))
        self.assertEqual(updater.pending_updates(), {})
        with patch.object(updater, "fetch_latest", return_value="1.1.0"):
            self.assertEqual(updater.check_all()[0]["state"], "same")

    def test_zero_exit_without_target_version_retains_pending(self):
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            self.assertEqual(self.run_update(), ([], [], ["demo-plugin"]))
        self.assertEqual(updater.pending_updates(), {"demo-plugin": "1.1.0"})

    def test_process_oserror_retains_pending(self):
        with patch.object(updater.subprocess, "run", side_effect=OSError("fixture launch failure")):
            self.assertEqual(self.run_update(), ([], [], ["demo-plugin"]))
        self.assertEqual(updater.pending_updates(), {"demo-plugin": "1.1.0"})
        self.assertEqual(updater.check_all()[0]["state"], "incomplete")

    def test_invalid_target_version_rejected_before_command_or_state_write(self):
        for version in ("latest", "^1.0.0", "1.0.0 & echo unsafe", ""):
            with self.subTest(version=version), patch.object(updater.subprocess, "run") as run:
                self.assertEqual(self.run_update(version), ([], [], ["demo-plugin"]))
                run.assert_not_called()
                self.assertFalse(self.pending.exists())

    def test_corrupt_pending_is_not_silently_ignored(self):
        for content in ("{broken", "[]", '{"demo-plugin": "latest"}', '{"demo-plugin": 123}'):
            with self.subTest(content=content):
                self.pending.write_text(content, encoding="utf-8")
                with patch.object(updater.subprocess, "run") as run:
                    with self.assertRaises(ValueError):
                        self.run_update()
                run.assert_not_called()
                with self.assertRaises(ValueError):
                    updater.check_all()
                self.assertEqual(self.pending.read_text(encoding="utf-8"), content)

    def test_save_pending_replace_failure_preserves_old_state_and_cleans_temp(self):
        updater.save_pending({"other-plugin": "2.0.0"})
        original = self.pending.read_bytes()
        with patch.object(updater.os, "replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaisesRegex(OSError, "fixture replace failure"):
                updater.save_pending({"demo-plugin": "1.1.0"})
        self.assertEqual(self.pending.read_bytes(), original)
        self.assertEqual(list(self.profile.glob(".updater-state-*")), [])


if __name__ == "__main__":
    unittest.main()
