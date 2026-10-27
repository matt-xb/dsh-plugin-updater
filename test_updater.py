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

    def write_workspace(self, text):
        path = os.path.join(self.profile, "pnpm-workspace.yaml")
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        return path

    def unused_patch_error(self, keys="demo-plugin@1.0.0"):
        return SimpleNamespace(returncode=1, stdout=(
            "[ERR_PNPM_UNUSED_PATCH] The following patches were not used: " + keys + "\n"), stderr="")

    def test_unused_selected_patch_cleanup_and_one_retry(self):
        original = "packages:\n  - .\npatchedDependencies:\n  demo-plugin@1.0.0: patches/demo.patch\n"
        workspace = self.write_workspace(original)
        patch_file = os.path.join(self.profile, "demo.patch")
        with open(patch_file, "w") as f:
            f.write("preserve patch contents")
        calls = []
        def remove(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return self.unused_patch_error()
            with open(workspace) as f:
                self.assertEqual(f.read(), "packages:\n  - .\npatchedDependencies: {}\n")
            return self.fake_remove(cmd, **kwargs)
        with patch.object(updater.subprocess, "run", side_effect=remove):
            backup = updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        with open(os.path.join(backup, "pnpm-workspace.yaml")) as f:
            self.assertEqual(f.read(), original)
        with open(patch_file) as f:
            self.assertEqual(f.read(), "preserve patch contents")

    def test_patch_cleanup_preserves_other_versions_policies_and_crlf(self):
        before = ("\ufeffpackages:\r\n  - .\r\npatchedDependencies: # patches\r\n"
                  "  'demo-plugin@1.0.0': patches/old.patch\r\n"
                  "  \"demo-plugin@2.0.0\": patches/used.patch\r\n"
                  "  demo-plugin-extra@1.0.0: patches/other.patch\r\n"
                  "allowBuilds:\r\n  sharp@0.32.6: true\r\n")
        path = self.write_workspace(before)
        error = self.unused_patch_error("demo-plugin@1.0.0, demo-plugin-extra@1.0.0")
        self.assertTrue(updater.remove_unused_patch_entries("demo-plugin", error.stdout, self.logs.append))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before.replace("  'demo-plugin@1.0.0': patches/old.patch\r\n", "").encode("utf-8"))

    def test_scoped_patch_and_unversioned_key(self):
        path = self.write_workspace("patchedDependencies:\n  '@owner/demo': patches/a.patch\n")
        error = self.unused_patch_error("@owner/demo")
        self.assertTrue(updater.remove_unused_patch_entries("@owner/demo", error.stdout, self.logs.append))
        with open(path) as f:
            self.assertEqual(f.read(), "patchedDependencies: {}\n")

    def test_unsupported_patch_yaml_is_unchanged(self):
        for original in (
                'patchedDependencies: {demo-plugin@1.0.0: patches/a.patch}\n',
                'patchedDependencies:\n  demo-plugin@1.0.0: &shared patches/a.patch\n',
                'patchedDependencies:\n  demo-plugin@1.0.0: |\n    patches/a.patch\n',
                'patchedDependencies:\n  <<: *patches\n',
                'patchedDependencies:\n  demo-plugin@1.0.0: a\n  demo-plugin@1.0.0: b\n'):
            with self.subTest(original=original):
                path = self.write_workspace(original)
                with self.assertRaisesRegex(RuntimeError, "未自动修改"):
                    updater.remove_unused_patch_entries("demo-plugin", self.unused_patch_error().stdout, self.logs.append)
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), original.encode("utf-8"))

    def test_unused_unrelated_patch_does_not_modify_or_retry(self):
        original = "patchedDependencies: {other@1.0.0: patches/other.patch}\n"
        path = self.write_workspace(original)
        with patch.object(updater.subprocess, "run", return_value=self.unused_patch_error("other@1.0.0")) as run:
            with self.assertRaisesRegex(RuntimeError, "UNUSED_PATCH"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_count, 1)
        with open(path) as f:
            self.assertEqual(f.read(), original)

    def test_used_transitive_patch_is_never_removed(self):
        original = "patchedDependencies:\n  demo-plugin@1.0.0: patches/a.patch\n"
        path = self.write_workspace(original)
        with patch.object(updater.subprocess, "run", side_effect=self.fake_remove) as run:
            updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_count, 1)
        with open(path) as f:
            self.assertEqual(f.read(), original)

    def test_patch_retry_failure_stops_after_two_calls(self):
        self.write_workspace("patchedDependencies:\n  demo-plugin@1.0.0: patches/a.patch\n")
        with patch.object(updater.subprocess, "run", side_effect=[self.unused_patch_error(),
                SimpleNamespace(returncode=1, stdout="second failure", stderr="")]) as run:
            with self.assertRaisesRegex(RuntimeError, "second failure"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_count, 2)

    def test_partial_manifest_change_stops_before_patch_cleanup(self):
        original = "patchedDependencies:\n  demo-plugin@1.0.0: patches/a.patch\n"
        path = self.write_workspace(original)
        def partial_remove(*args, **kwargs):
            self.write_manifest({})
            return self.unused_patch_error()
        with patch.object(updater.subprocess, "run", side_effect=partial_remove) as run:
            with self.assertRaisesRegex(RuntimeError, "已不在当前 profile"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_count, 1)
        with open(path) as f:
            self.assertEqual(f.read(), original)

    def test_source_change_after_first_error_stops_without_patch_edits(self):
        original = "patchedDependencies:\n  demo-plugin@1.0.0: patches/a.patch\n"
        path = self.write_workspace(original)
        def change_source(*args, **kwargs):
            self.write_manifest({"demo-plugin": "2.0.0"})
            return self.unused_patch_error()
        with patch.object(updater.subprocess, "run", side_effect=change_source) as run:
            with self.assertRaisesRegex(RuntimeError, "来源已变化"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_count, 1)
        with open(path) as f:
            self.assertEqual(f.read(), original)

    def test_atomic_replace_failure_preserves_config_and_cleans_temp(self):
        original = "patchedDependencies:\n  demo-plugin@1.0.0: patches/a.patch\n"
        path = self.write_workspace(original)
        with patch.object(updater.os, "replace", side_effect=PermissionError("replace denied")):
            with self.assertRaisesRegex(PermissionError, "replace denied"):
                updater.remove_unused_patch_entries("demo-plugin", self.unused_patch_error().stdout, self.logs.append)
        with open(path) as f:
            self.assertEqual(f.read(), original)
        self.assertFalse(any(name.startswith(".updater-patch-") for name in os.listdir(self.profile)))

    def test_snapshot_change_preserved_and_not_retried(self):
        path = self.write_workspace("patchedDependencies:\n  demo-plugin@1.0.0: patches/a.patch\n")
        original_temp = updater.tempfile.NamedTemporaryFile
        def change_workspace(**kwargs):
            self.write_workspace("concurrent: changed\n")
            return original_temp(**kwargs)
        with patch.object(updater.tempfile, "NamedTemporaryFile", side_effect=change_workspace), \
                patch.object(updater.subprocess, "run", return_value=self.unused_patch_error()) as run:
            with self.assertRaisesRegex(RuntimeError, "其他程序修改"):
                updater.run_uninstall("demo-plugin", "^1.0.0", self.logs.append)
        self.assertEqual(run.call_count, 1)
        with open(path) as f:
            self.assertEqual(f.read(), "concurrent: changed\n")
        self.assertFalse(any(name.startswith(".updater-patch-") for name in os.listdir(self.profile)))

    def write_installed_version(self, version):
        folder = os.path.join(self.profile, "node_modules", "demo-plugin")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"version": version}, f)

    def test_update_uses_same_home_and_reports_progress(self):
        self.write_installed_version("1.1.0")
        progress = []
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="", stderr="")) as run:
            result = updater.run_update([("demo-plugin", "1.1.0")], self.logs.append, progress.append)
        self.assertEqual(result, (["demo-plugin"], [], []))
        self.assertEqual(progress, [1])
        self.assertEqual(run.call_args.kwargs["env"]["DSH_HOME"], os.path.abspath(self.home))
        self.assertEqual(run.call_args.args[0][-5:],
                         ["plugin", "--profile", "desktop", "add", "demo-plugin@1.1.0"])

    def test_update_failure_is_reported(self):
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="fixture update failure")):
            result = updater.run_update([("demo-plugin", "1.1.0")], self.logs.append)
        self.assertEqual(result, ([], [], ["demo-plugin"]))
        self.assertTrue(any("fixture update failure" in line for line in self.logs))

    def test_updated_files_with_blocked_builds_are_incomplete(self):
        self.write_installed_version("1.1.0")
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="ERR_PNPM_IGNORED_BUILDS", stderr="")):
            result = updater.run_update([("demo-plugin", "1.1.0")], self.logs.append)
        self.assertEqual(result, ([], ["demo-plugin"], []))
        self.assertTrue(any("版本已更新，但安装未完成" in line for line in self.logs))
        self.assertTrue(any("不要关闭整体安全检查" in line for line in self.logs))

    def test_other_error_with_updated_files_remains_incomplete(self):
        self.write_installed_version("1.1.0")
        with patch.object(updater.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="other error")):
            result = updater.run_update([("demo-plugin", "1.1.0")], self.logs.append)
        self.assertEqual(result, ([], ["demo-plugin"], []))

    def test_success_exit_without_target_files_is_failure(self):
        for version in (None, "1.0.0"):
            if version:
                self.write_installed_version(version)
            with self.subTest(version=version), patch.object(updater.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
                result = updater.run_update([("demo-plugin", "1.1.0")], self.logs.append)
            self.assertEqual(result, ([], [], ["demo-plugin"]))

    def test_cli_incomplete_update_has_nonzero_exit(self):
        rows = [{"name": "demo-plugin", "latest": "1.1.0", "state": "newer"}]
        with patch.object(updater.sys, "argv", ["updater", "--update"]), \
                patch.object(updater, "check_all", return_value=rows), \
                patch.object(updater, "run_update", return_value=([], ["demo-plugin"], [])), \
                patch("builtins.print"):
            self.assertEqual(updater.main(), 1)

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
