# -*- coding: utf-8 -*-
"""版本比较和依赖来源判断的自检：python -m unittest -v test_updater.py"""
import importlib.util
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
