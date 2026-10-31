# -*- coding: utf-8 -*-
"""真实 tkinter 控件烟测；仅使用模拟检查/安装，不操作真实插件。"""
import importlib.machinery
import importlib.util
import os
import time
import unittest
from unittest.mock import patch

path = os.path.join(os.path.dirname(__file__), "dsh-plugin-updater.pyw")
spec = importlib.util.spec_from_loader("updater_ui", importlib.machinery.SourceFileLoader("updater_ui", path))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class TestUI(unittest.TestCase):
    def test_selected_update_and_button_recovery(self):
        import tkinter as tk
        from tkinter import ttk
        rows = [{"name": n, "declared": "^1.0.0", "installed": "1.0.0",
                 "latest": "1.1.0", "state": "newer"} for n in ("demo-one", "demo-two")]
        with patch.object(updater, "check_all", return_value=rows), \
                patch.object(updater, "profile_dir", return_value="mock-profile"), \
                patch.object(updater, "installed_plugins", return_value=[
                    ("demo-one", "1.1.0", "1.1.0"), ("demo-two", "^1.0.0", "1.0.0")]), \
                patch.object(updater, "run_update", return_value=(["demo-one"], [], [])) as run, \
                patch("tkinter.messagebox.askyesno", return_value=True):
            try:
                root = updater.build_ui()
            except tk.TclError as e:
                self.skipTest("tkinter display unavailable: %s" % e)
            try:
                def widgets(w):
                    for c in w.winfo_children():
                        yield c
                        yield from widgets(c)
                all_widgets = list(widgets(root))
                buttons = {w.cget("text"): w for w in all_widgets if isinstance(w, ttk.Button)}
                tree = next(w for w in all_widgets if isinstance(w, ttk.Treeview))
                selected = buttons["更新选中插件"]
                def pump_until(condition):
                    deadline = time.monotonic() + 5
                    def poll():
                        if condition() or time.monotonic() >= deadline:
                            root.quit()
                        else:
                            root.after(10, poll)
                    root.after(10, poll)
                    root.mainloop()
                    self.assertTrue(condition())
                pump_until(lambda: len(tree.get_children()) == 2)
                self.assertTrue(selected.instate(["disabled"]))
                tree.selection_set(tree.get_children()[0])
                tree.event_generate("<<TreeviewSelect>>")
                root.update()
                self.assertFalse(selected.instate(["disabled"]))
                selected.invoke()
                self.assertTrue(selected.instate(["disabled"]))
                pump_until(lambda: run.called and not buttons["检查更新"].instate(["disabled"]))
                self.assertEqual(run.call_args.args[0], [("demo-one", "1.1.0")])
                self.assertEqual(run.call_args.kwargs["expected"], {"demo-one": "^1.0.0"})
                self.assertFalse(buttons["一键更新"].instate(["disabled"]))
                tree.selection_set(tree.get_children()[1])
                tree.event_generate("<<TreeviewSelect>>")
                root.update()
                self.assertFalse(selected.instate(["disabled"]))
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
