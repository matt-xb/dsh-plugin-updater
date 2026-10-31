"""Patch temporary-file failures, isolated from real DSH profiles."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_source = str(Path(__file__).with_name("dsh-plugin-updater.pyw"))
_spec = importlib.util.spec_from_loader(
    "patch_cleanup_updater", importlib.machinery.SourceFileLoader("patch_cleanup_updater", _source))
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)


class TestPatchCleanup(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.profile = Path(directory.name)
        self.workspace = self.profile / "pnpm-workspace.yaml"
        self.original = b"patchedDependencies:\n  demo-plugin@1.0.0: patches/demo.patch\n"
        self.workspace.write_bytes(self.original)
        profile_patch = patch.object(updater, "profile_dir", return_value=str(self.profile))
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        self.output = "ERR_PNPM_UNUSED_PATCH The following patches were not used: demo-plugin@1.0.0"

    def check_failure(self, phase):
        create = tempfile.NamedTemporaryFile

        class FailingFile:
            def __init__(self, **kwargs):
                self.file = create(**kwargs)
                self.name = self.file.name

            def __enter__(self):
                return self

            def write(self, content):
                if phase == "write":
                    self.file.write(content[:5])
                    raise OSError("fixture write failure")
                return self.file.write(content)

            def __exit__(self, *args):
                self.file.close()
                if phase == "close":
                    raise OSError("fixture close failure")

        with patch.object(updater.tempfile, "NamedTemporaryFile", side_effect=FailingFile), \
                patch.object(updater.os, "replace") as replace:
            with self.assertRaisesRegex(OSError, "fixture " + phase + " failure"):
                updater.remove_unused_patch_entries("demo-plugin", self.output, lambda _: None)
            replace.assert_not_called()
        self.assertEqual(self.workspace.read_bytes(), self.original)
        self.assertEqual(list(self.profile.glob(".updater-patch-*")), [])

    def test_write_failure_cleans_temporary_and_preserves_config(self):
        self.check_failure("write")

    def test_close_failure_cleans_temporary_and_preserves_config(self):
        self.check_failure("close")

    def test_creation_failure_preserves_config(self):
        with patch.object(updater.tempfile, "NamedTemporaryFile", side_effect=OSError("fixture create failure")):
            with self.assertRaisesRegex(OSError, "fixture create failure"):
                updater.remove_unused_patch_entries("demo-plugin", self.output, lambda _: None)
        self.assertEqual(self.workspace.read_bytes(), self.original)
        self.assertEqual(list(self.profile.glob(".updater-patch-*")), [])

    def test_success_replaces_config_and_leaves_no_temporary(self):
        self.assertTrue(updater.remove_unused_patch_entries("demo-plugin", self.output, lambda _: None))
        self.assertEqual(self.workspace.read_bytes(), b"patchedDependencies: {}\n")
        self.assertEqual(list(self.profile.glob(".updater-patch-*")), [])


if __name__ == "__main__":
    unittest.main()
