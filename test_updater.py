# -*- coding: utf-8 -*-
"""版本、来源及更新/卸载安全自检：python -m unittest -v test_updater.py"""
import importlib.util
import os
import unittest
import json
import tempfile
from unittest.mock import patch
from types import SimpleNamespace

_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dsh-plugin-updater.pyw")
_spec = importlib.util.spec_from_loader("updater", importlib.machinery.SourceFileLoader("updater", _p))
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)


class TestCmpVer(unittest.TestCase):
    def test_basic_order(self):
        self.assertEqual(updater.cmp_ver("1.0.0", "1.0.1"), -1)
        self.assertEqual(updater.cmp_ver("1.2.0", "1.0.9"), 1)

    def test_missing_patch_is_equal(self):
        self.assertEqual(updater.cmp_ver("1.0", "1.0.0"), 0)

    def test_stable_beats_prerelease(self):
        self.assertEqual(updater.cmp_ver("1.0.0", "1.0.0-rc.1"), 1)
        self.assertEqual(updater.cmp_ver("1.0.0-rc.1", "1.0.0"), -1)

    def test_prerelease_order(self):
        self.assertEqual(updater.cmp_ver("1.0.0-rc.1", "1.0.0-rc.2"), -1)
        self.assertEqual(updater.cmp_ver("1.0.0-alpha", "1.0.0-beta"), -1)
        # 数字段按数值比，不是按字符串比
        self.assertEqual(updater.cmp_ver("1.0.0-rc.9", "1.0.0-rc.10"), -1)

    def test_garbage_is_unknown(self):
        self.assertIsNone(updater.cmp_ver("latest", "1.0.0"))
        self.assertIsNone(updater.cmp_ver("1.0.0", ""))


class TestIsLocalSpec(unittest.TestCase):
    def test_local_sources(self):
        for spec in ("file:../x", "link:../x", "github:matt-xb/x", "git+https://a/b.git",
                     "https://example.com/x.tgz"):
            self.assertTrue(updater.is_local_spec(spec), spec)

    def test_registry_sources(self):
        for spec in ("1.2.3", "^1.2.3", "~1.2.3", "1.2.3-rc.1"):
            self.assertFalse(updater.is_local_spec(spec), spec)


class TestUninstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        self.profile = os.path.join(self.home, "profiles", "desktop")
        os.makedirs(self.profile)
        self.manifest = os.path.join(self.profile, "package.json")
        self.write_manifest({"demo-plugin": "^1.0.0", "@deepseek-ai/dsh-base": "1.0.0"})
        self.home_patch = patch.object(updater, "dsh_home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.profile_patch = patch.object(updater, "PROFILE", "desktop")
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        self.cli_patch = patch.object(updater, "find_dsh", return_value="dsh.cmd")
        self.cli_patch.start()
        self.addCleanup(self.cli_patch.stop)
        self.logs = []

    def write_manifest(self, dependencies):
        with open(self.manifest, "w", encoding="utf-8") as f:
            json.dump({"dependencies": dependencies}, f)

    def fake_remove(self, cmd, **kwargs):
        self.assertEqual(kwargs["cwd"], self.profile)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["env"]["DSH_HOME"], os.path.abspath(self.home))
        self.write_manifest({"@deepseek-ai/dsh-base": "1.0.0"})
        return SimpleNamespace(returncode=0, stdout="removed", stderr="")

    def test_remove_official_command_and_backup(self):
        with open(self.manifest, "rb") as f:
            original = f.read()
        for name in ("pnpm-lock.yaml", "pnpm-workspace.yaml", "cordis.patch.yml"):
            with open(os.path.join(self.profile, name), "w", encoding="utf-8") as f:
                f.write("fixture: unchanged\n")
        with patch.object(updater.subprocess, "run", side_effect=self.fake_remove) as run:
            backup = updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_args.args[0][-6:],
                         ["plugin", "--profile", "desktop", "remove", "-w", "demo-plugin"])
        with open(os.path.join(backup, "package.json"), "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertEqual(sorted(os.listdir(backup)),
                         ["cordis.patch.yml", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"])
        with open(os.path.join(self.profile, "cordis.patch.yml"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "fixture: unchanged\n")

    def test_remove_without_workspace(self):
        with patch.object(updater.subprocess, "run", side_effect=self.fake_remove) as run:
            updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_args.args[0][-5:],
                         ["plugin", "--profile", "desktop", "remove", "demo-plugin"])
        self.assertNotIn("-w", run.call_args.args[0])

    def test_reject_unsafe_names_core_and_unknown(self):
        for name in ("--help", "../x", "demo&calc", "demo%PATH%", "demo\nplugin",
                     "demo@latest", "@deepseek-ai/dsh-base", "missing"):
            with self.subTest(name=name), patch.object(updater.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    updater.run_uninstall(name, "1.0.0", self.logs.append)
                run.assert_not_called()

    def test_reject_unsafe_profile(self):
        for profile in ("../desktop", "desktop&calc", "desktop%PATH%", "desktop profile"):
            with self.subTest(profile=profile), patch.object(updater, "PROFILE", profile), \
                    patch.object(updater.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "profile"):
                    updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
                run.assert_not_called()

    def test_changed_source_rejected(self):
        with patch.object(updater.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "来源已变化"):
                updater.run_uninstall("demo-plugin", "github:other/repo", self.logs.append)
            run.assert_not_called()

    def test_non_registry_source_can_be_removed(self):
        self.write_manifest({"@owner/demo-plugin": "github:owner/repo"})
        with patch.object(updater.subprocess, "run", side_effect=self.fake_remove) as run:
            updater.run_uninstall("@owner/demo-plugin", "github:owner/repo", self.logs.append)
        self.assertEqual(run.call_args.args[0][-1], "@owner/demo-plugin")

    def test_failure_retains_diagnostic_and_backup(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="pnpm fixture failure")
        with patch.object(updater.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "pnpm fixture failure"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertTrue(any("配置备份" in line for line in self.logs))
        self.assertIn("demo-plugin", dict((n, d) for n, d, _ in updater.installed_plugins()))

    def test_zero_exit_with_remaining_dependency_is_failure(self):
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "命令未移除该依赖"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)

    def test_backup_failure_does_not_run_remove(self):
        with patch.object(updater.shutil, "copy2", side_effect=OSError("backup denied")), \
                patch.object(updater.subprocess, "run") as run:
            with self.assertRaisesRegex(OSError, "backup denied"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
            run.assert_not_called()

    def test_revalidates_after_backup(self):
        def changed_copy(source, destination):
            self.write_manifest({"demo-plugin": "2.0.0"})
        with patch.object(updater.shutil, "copy2", side_effect=changed_copy), \
                patch.object(updater.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "来源已变化"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
            run.assert_not_called()

    def test_missing_cli_does_not_run_remove(self):
        with patch.object(updater, "find_dsh", return_value=None), \
                patch.object(updater.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "DSH_UPDATER_DSH"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
            run.assert_not_called()

    def test_update_uses_same_home_and_reports_progress(self):
        progress = []
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="", stderr="")) as run:
            result = updater.run_update([("demo-plugin", "1.1.0")], self.logs.append, progress.append)
        self.assertEqual(result, (["demo-plugin"], []))
        self.assertEqual(progress, [1])
        self.assertEqual(run.call_args.kwargs["env"]["DSH_HOME"], os.path.abspath(self.home))
        self.assertEqual(run.call_args.args[0][-5:],
                         ["plugin", "--profile", "desktop", "add", "demo-plugin@1.1.0"])

    def test_update_failure_is_reported(self):
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="fixture update failure")):
            result = updater.run_update([("demo-plugin", "1.1.0")], self.logs.append)
        self.assertEqual(result, ([], ["demo-plugin"]))
        self.assertTrue(any("fixture update failure" in line for line in self.logs))

    def test_confirmation_names_target_and_limits(self):
        text = updater.uninstall_confirm_text({"name": "demo-plugin", "installed": "1.0.0",
                                              "declared": "^1.0.0"})
        for expected in ("demo-plugin", "1.0.0", "^1.0.0", "退出 DSH", "不是完整回滚", "自定义配置"):
            self.assertIn(expected, text)


class TestFindDsh(unittest.TestCase):
    def test_explicit_cli_override(self):
        with patch.dict(os.environ, {"DSH_UPDATER_DSH": "custom/dsh.cmd"}), \
                patch.object(updater.os.path, "isfile", return_value=True):
            self.assertEqual(updater.find_dsh(), "custom/dsh.cmd")

    def test_invalid_override_does_not_fall_back(self):
        with patch.dict(os.environ, {"DSH_UPDATER_DSH": "missing/dsh.cmd"}), \
                patch.object(updater.os.path, "isfile", return_value=False):
            self.assertIsNone(updater.find_dsh())


if __name__ == "__main__":
    unittest.main()
