# -*- coding: utf-8 -*-
"""DSH 插件自动更新

双击运行：检查 DeepSeek Harness 桌面 profile 里装的所有插件有没有新版本，
在列表里点「一键更新」就按官方方式装好（走 dsh plugin add，和 dshmarket 商店同一个流程）。

装完需要重启 DeepSeek Harness 才生效。@deepseek-ai/ 开头的宿主包自动跳过。
命令行：--check 只检查并打印，--update 直接更新所有可更新项（给自测/计划任务用）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROFILE = os.environ.get("DSH_UPDATER_PROFILE", "desktop")
REGISTRY = os.environ.get("DSH_UPDATER_REGISTRY", "https://registry.npmjs.org").rstrip("/")
TIMEOUT = 15
SKIP = ("@deepseek-ai/",)  # 宿主运行时自带，不在 npm 主线上升级
LOCAL_SPEC = ("file:", "link:", "portal:", "workspace:", "git+", "github:", "http:", "https:")


def is_local_spec(spec):
    """package.json 里写成 file:/git/网址 的依赖不来自 npm，查 latest 没意义。"""
    return str(spec).strip().startswith(LOCAL_SPEC)


def dsh_home():
    env = os.environ.get("DSH_HOME")
    if env:
        return env
    cand = [os.path.expanduser("~/.dsh"),
            os.path.join(os.environ.get("APPDATA", ""), ".dsh"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "DeepSeekHarnessData", ".dsh"),
            r"D:\DeepSeekHarnessData\.dsh"]  # 旧版安装器的默认数据目录
    for p in cand:
        if p and os.path.isdir(p):
            return p
    return cand[0]


def profile_dir():
    return os.path.join(dsh_home(), "profiles", PROFILE)


def find_dsh():
    cand = [shutil.which("dsh"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DeepSeek Harness",
                         "resources", "runtime", "cli", "bin", "dsh.cmd")]
    for p in cand:
        if p and os.path.isfile(p):
            return p
    return None


def installed_plugins():
    """[(包名, package.json 里写的版本, node_modules 里实际的版本)]"""
    with open(os.path.join(profile_dir(), "package.json"), encoding="utf-8") as f:
        deps = json.load(f).get("dependencies", {})
    out = []
    for name, declared in deps.items():
        if name.startswith(SKIP):
            continue
        real = declared
        try:
            with open(os.path.join(profile_dir(), "node_modules", name, "package.json"),
                      encoding="utf-8") as f:
                real = json.load(f)["version"]
        except Exception:
            pass
        out.append((name, declared, real))
    return out


def _parse(v):
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$",
                 str(v))
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")]
    nums += [0] * (4 - len(nums))
    pre = m.group(2)
    tags = [] if pre is None else [(0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")]
    return nums[:4], tags


def cmp_ver(a, b):
    """a<b 返回 -1，相等 0，a>b 1；不是纯版本号返回 None。"""
    pa, pb = _parse(a), _parse(b)
    if not pa or not pb:
        return None
    if pa[0] != pb[0]:
        return -1 if pa[0] < pb[0] else 1
    if pa[1] == pb[1]:
        return 0
    if not pa[1]:
        return 1            # 正式版比预发布版新
    if not pb[1]:
        return -1
    return -1 if pa[1] < pb[1] else 1


def fetch_latest(name):
    url = "%s/%s/latest" % (REGISTRY, name.replace("/", "%2F"))
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "dsh-plugin-updater"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))["version"]


def check_all():
    rows = [{"name": n, "installed": real, "declared": d, "latest": "-", "state": "same"}
            for n, d, real in installed_plugins()]

    def one(r):
        if is_local_spec(r["declared"]):
            r["state"] = "skip"
            return
        try:
            r["latest"] = fetch_latest(r["name"])
            r["state"] = {0: "same", -1: "newer", 1: "preview"}.get(
                cmp_ver(r["installed"], r["latest"]), "same")
        except Exception as e:  # 单个包查询失败不影响其它
            r["state"] = "error"
            r["error"] = str(e)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(one, rows))
    return rows


def run_update(todo, log, on_progress=None):
    """todo: [(包名, 版本)]，串行安装（并发动同一个 node_modules 会打架）。返回 (成功, 失败)。"""
    dsh = find_dsh()
    if not dsh:
        raise RuntimeError("找不到 dsh 命令：DeepSeek Harness 装到别的路径了？")
    ok, failed = [], []
    for i, (name, ver) in enumerate(todo, 1):
        log("(%d/%d) 安装 %s@%s …" % (i, len(todo), name, ver))
        cmd = (["cmd", "/c", dsh] if os.name == "nt" else [dsh]) + \
              ["plugin", "--profile", PROFILE, "add"]
        # profile 里有 pnpm-workspace.yaml 时 pnpm 要求 -w，否则直接 add
        if os.path.exists(os.path.join(profile_dir(), "pnpm-workspace.yaml")):
            cmd.append("-w")
        cmd.append("%s@%s" % (name, ver))
        p = subprocess.run(cmd, cwd=profile_dir(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if p.returncode == 0:
            ok.append(name)
            log("    ✓ %s 完成" % name)
        else:
            failed.append(name)
            log("    ✗ %s 失败：%s" % (name, out[-600:] or "(无输出)"))
        if on_progress:
            on_progress(i)
    return ok, failed


STATE_TEXT = {"newer": "有更新", "same": "已是最新", "preview": "本地更新（预览版）",
              "error": "查询失败", "skip": "跳过（非 npm）"}


def confirm_text(rows, todo):
    lines = ["%s  %s → %s" % (n, next(r["installed"] for r in rows if r["name"] == n), v)
             for n, v in todo]
    return "将更新 %d 个插件：\n\n%s\n\n更新期间请不要关闭 DeepSeek Harness。" % (
        len(todo), "\n".join(lines))


def build_ui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title("DSH 插件自动更新")
    root.geometry("840x580+%d+%d" % ((root.winfo_screenwidth() - 840) // 2,
                                     (root.winfo_screenheight() - 580) // 3))
    root.minsize(720, 460)

    font = ("Microsoft YaHei UI", 9)
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass
    style.configure(".", font=font)
    style.configure("Treeview", rowheight=26, font=font)
    style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 13, "bold"))

    top = ttk.Frame(root, padding=(14, 12, 14, 4))
    top.pack(fill="x")
    ttk.Label(top, text="DSH 插件自动更新", style="Title.TLabel").pack(side="left")
    lbl_status = ttk.Label(top, text="", foreground="#666")
    lbl_status.pack(side="right")
    ttk.Label(root, text="profile：%s" % profile_dir(), foreground="#888",
              font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=16)

    bar = ttk.Frame(root, padding=(14, 8, 14, 8))
    bar.pack(fill="x")
    btn_check = ttk.Button(bar, text="检查更新")
    btn_check.pack(side="left")
    btn_all = ttk.Button(bar, text="一键更新")
    btn_all.pack(side="left", padx=(8, 0))
    btn_all.state(["disabled"])
    progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
    progress.pack(side="right")

    wrap = ttk.Frame(root, padding=(14, 0, 14, 8))
    wrap.pack(fill="both", expand=True)
    tree = ttk.Treeview(wrap, columns=("name", "installed", "latest", "state"),
                        show="headings", height=10)
    for c, t, w, a in (("name", "插件", 380, "w"), ("installed", "当前版本", 120, "center"),
                       ("latest", "最新版本", 120, "center"), ("state", "状态", 160, "w")):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor=a, stretch=(c == "name"))
    for tag, color in (("newer", "#0a7d32"), ("error", "#b3261e"), ("preview", "#8a6d00"),
                       ("skip", "#777777")):
        tree.tag_configure(tag, foreground=color)
    ys = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    tree.pack(side="left", fill="both", expand=True)
    ys.pack(side="right", fill="y")

    logbox = tk.Text(root, height=8, wrap="word", state="disabled", font=("Consolas", 9),
                     background="#1e1e1e", foreground="#d4d4d4", relief="flat")
    logbox.pack(fill="x", padx=14, pady=(0, 12))

    state = {"rows": [], "busy": False}

    def log(msg):
        def _():
            logbox.configure(state="normal")
            logbox.insert("end", msg.rstrip() + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, _)

    def set_status(text):
        root.after(0, lambda: lbl_status.configure(text=text))

    def paint(rows):
        tree.delete(*tree.get_children())
        for r in rows:
            tree.insert("", "end", tags=(r["state"],),
                        values=(r["name"], r["installed"], r["latest"],
                                STATE_TEXT.get(r["state"], r["state"])))

    def do_check(after=None):
        if state["busy"]:
            return
        state["busy"] = True
        btn_check.state(["disabled"])
        btn_all.state(["disabled"])
        progress.start(12)
        set_status("正在检查…")

        def work():
            try:
                rows = check_all()
                state["rows"] = rows
                newer = [r for r in rows if r["state"] == "newer"]
                root.after(0, lambda: paint(rows))
                root.after(0, lambda: btn_all.state(["!disabled"] if newer else ["disabled"]))
                set_status("共 %d 个插件，%d 个可更新 · %s"
                           % (len(rows), len(newer), time.strftime("%H:%M:%S")))
                log("检查完成：%d 个插件，%d 个可更新" % (len(rows), len(newer)))
            except Exception as e:
                set_status("检查失败")
                log("检查失败：%s" % e)
            finally:
                state["busy"] = False
                root.after(0, lambda: btn_check.state(["!disabled"]))
                root.after(0, progress.stop)
                if after:
                    after()

        threading.Thread(target=work, daemon=True).start()

    def do_update(after=None):
        if state["busy"]:
            return
        todo = [(r["name"], r["latest"]) for r in state["rows"] if r["state"] == "newer"]
        if not todo:
            messagebox.showinfo("DSH 插件自动更新", "没有需要更新的插件。")
            return
        if not messagebox.askyesno("确认更新", confirm_text(state["rows"], todo)):
            return
        state["busy"] = True
        btn_check.state(["disabled"])
        btn_all.state(["disabled"])
        progress.stop()
        progress.configure(mode="determinate", value=0, maximum=len(todo))
        set_status("正在更新…")

        def work():
            ok, failed = [], []
            try:
                ok, failed = run_update(
                    todo, log, on_progress=lambda i: root.after(
                        0, lambda: progress.configure(value=i)))
            except Exception as e:
                log("更新失败：%s" % e)
                failed = [n for n, _ in todo]
            finally:
                state["busy"] = False
                root.after(0, lambda: progress.configure(mode="indeterminate", value=0))
                set_status(("完成 %d 个，失败 %d 个" % (len(ok), len(failed))) if failed
                           else "更新完成，重启 DeepSeek Harness 后生效")
                log("—— 成功 %d，失败 %d ——" % (len(ok), len(failed)))
                root.after(0, lambda: btn_check.state(["!disabled"]))
                if after:
                    after()
                elif not failed:
                    root.after(200, lambda: do_check())

        threading.Thread(target=work, daemon=True).start()

    btn_check.configure(command=do_check)
    btn_all.configure(command=do_update)
    log("DSH 插件自动更新 · profile %s" % profile_dir())
    root.after(250, do_check)
    return root


def main():
    argv = sys.argv[1:]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--check" in argv:
        rows = check_all()
        for r in rows:
            print("%-42s %-12s -> %-12s %s"
                  % (r["name"], r["installed"], r["latest"], r["state"]))
        return 0
    if "--update" in argv:
        rows = check_all()
        todo = [(r["name"], r["latest"]) for r in rows if r["state"] == "newer"]
        if not todo:
            print("没有需要更新的插件")
            return 0
        ok, failed = run_update(todo, print)
        print("成功 %d，失败 %d" % (len(ok), len(failed)))
        return 1 if failed else 0
    build_ui().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
