"""Status/CLI regressions; no real registry or plugin commands."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

_path = os.path.join(os.path.dirname(__file__), "dsh-plugin-updater.pyw")
_spec = importlib.util.spec_from_loader("status_updater", importlib.machinery.SourceFileLoader("status_updater", _path))
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)


class TestStatus(unittest.TestCase):
    def test_home_requires_target_profile(self):
        with tempfile.TemporaryDirectory() as root:
            empty, valid = os.path.join(root, "empty"), os.path.join(root, "appdata", ".dsh")
            os.makedirs(empty)
            profile = os.path.join(valid, "profiles", updater.PROFILE)
            os.makedirs(profile)
            with open(os.path.join(profile, "package.json"), "w") as f:
                f.write("{}")
            with patch.dict(os.environ, {"APPDATA": os.path.join(root, "appdata")}, clear=True), \
                    patch.object(updater.os.path, "expanduser", return_value=empty):
                self.assertEqual(updater.dsh_home(), valid)
            with patch.dict(os.environ, {"DSH_HOME": empty}):
                self.assertEqual(updater.dsh_home(), empty)

    def test_missing_or_invalid_installed_version_is_unknown(self):
        with tempfile.TemporaryDirectory() as profile:
            with open(os.path.join(profile, "package.json"), "w") as f:
                json.dump({"dependencies": {"demo": "1.0.0"}}, f)
            with patch.object(updater, "profile_dir", return_value=profile):
                self.assertEqual(updater.installed_plugins(), [("demo", "1.0.0", "未知")])
                package = os.path.join(profile, "node_modules", "demo")
                os.makedirs(package)
                for version in (None, "invalid", 42):
                    with open(os.path.join(package, "package.json"), "w") as f:
                        json.dump({"version": version}, f)
                    self.assertEqual(updater.installed_plugins()[0][2], "未知")

    def check_rows(self, installed, latest="1.0.0", pending=None):
        with patch.object(updater, "installed_plugins", return_value=installed), \
                patch.object(updater, "fetch_latest", return_value=latest), \
                patch.object(updater, "pending_updates", return_value=pending or {}, create=True):
            return updater.check_all()

    def test_unknown_comparison_never_same(self):
        for real, latest in (("未知", "1.0.0"), ("^1.0.0", "2.0.0"), ("1.0.0", "invalid")):
            self.assertEqual(self.check_rows([("demo", "1.0.0", real)], latest)[0]["state"], "unknown")

    def test_pending_uses_original_target_even_when_version_equal(self):
        rows = self.check_rows([("demo", "1.0.0", "1.0.0")], "2.0.0", {"demo": "1.0.0"})
        self.assertEqual((rows[0]["state"], rows[0]["latest"]), ("incomplete", "1.0.0"))

    def test_local_source_skips_pending(self):
        rows = self.check_rows([("demo", "file:../demo", "1.0.0")], pending={"demo": "1.0.0"})
        self.assertEqual(rows[0]["state"], "skip")

    def cli(self, argument, rows, result=([], [], [])):
        output = io.StringIO()
        with patch.object(updater.sys, "argv", ["updater", argument]), \
                patch.object(updater, "check_all", return_value=rows), \
                patch.object(updater, "run_update", return_value=result) as run, \
                contextlib.redirect_stdout(output):
            code = updater.main()
        return code, output.getvalue(), run

    @staticmethod
    def row(state, name="demo"):
        return {"name": name, "declared": "^1.0.0", "installed": "1.0.0", "latest": "1.1.0", "state": state}

    def test_cli_check_problem_states_nonzero(self):
        for state in ("error", "unknown", "incomplete"):
            code, output, run = self.cli("--check", [self.row(state)])
            self.assertEqual(code, 1)
            self.assertIn(state, output)
            run.assert_not_called()

    def test_cli_query_failure_not_no_updates(self):
        for state in ("error", "unknown"):
            code, output, run = self.cli("--update", [self.row(state)])
            self.assertEqual(code, 1)
            self.assertNotIn("没有需要更新的插件", output)
            run.assert_not_called()

    def test_cli_pending_repair_passes_expected(self):
        code, output, run = self.cli("--update", [self.row("incomplete")], (["demo"], [], []))
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [("demo", "1.1.0")])
        self.assertEqual(run.call_args.kwargs["expected"], {"demo": "^1.0.0"})

    def test_cli_unresolved_check_error_keeps_nonzero_after_other_success(self):
        code, output, run = self.cli("--update", [self.row("newer"), self.row("error", "broken")], (["demo"], [], []))
        self.assertEqual(code, 1)

    def test_cli_failed_repair_nonzero(self):
        code, output, run = self.cli("--update", [self.row("incomplete")], ([], ["demo"], []))
        self.assertEqual(code, 1)

    def test_cli_profile_failure_is_reported(self):
        for argument in ("--check", "--update"):
            with patch.object(updater.sys, "argv", ["updater", argument]), \
                    patch.object(updater, "check_all", side_effect=OSError("missing profile")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(updater.main(), 1)
                self.assertIn("missing profile", output.getvalue())


if __name__ == "__main__":
    unittest.main()
