# -*- coding: utf-8 -*-
"""DSH 插件自动更新

双击运行：检查 DeepSeek Harness 桌面 profile 里装的所有插件有没有新版本，
点「一键更新」走官方 dsh plugin add；选中插件可确认卸载（官方 remove）。

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
import tempfile
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
    override = os.environ.get("DSH_UPDATER_DSH")
    if override:
        return override if os.path.isfile(override) else None
    cand = [shutil.which("dsh"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DeepSeek Harness",
                         "resources", "runtime", "cli", "bin", "dsh.cmd")]
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                                name = winreg.QueryValueEx(app, "DisplayName")[0]
                                if name.startswith("DeepSeek Harness"):
                                    location = winreg.QueryValueEx(app, "InstallLocation")[0]
                                    cand.append(os.path.join(location, "resources", "runtime",
                                                             "cli", "bin", "dsh.cmd"))
                        except OSError:
                            continue
            except OSError:
                continue
    return next((p for p in cand if p and os.path.isfile(p)), None)


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
    """todo: [(包名, 版本)]，串行安装（并发动同一个 node_modules 会打架）。返回 (成功, 未完成, 失败)；非零退出但版本已落盘不等于安装成功。"""
    dsh = find_dsh()
    if not dsh:
        raise RuntimeError("找不到 dsh 命令：DeepSeek Harness 装到别的路径了？")
    ok, incomplete, failed = [], [], []
    for i, (name, ver) in enumerate(todo, 1):
        log("(%d/%d) 安装 %s@%s …" % (i, len(todo), name, ver))
        cmd = (["cmd", "/c", dsh] if os.name == "nt" else [dsh]) + \
              ["plugin", "--profile", PROFILE, "add"]
        # profile 里有 pnpm-workspace.yaml 时 pnpm 要求 -w，否则直接 add
        if os.path.exists(os.path.join(profile_dir(), "pnpm-workspace.yaml")):
            cmd.append("-w")
        cmd.append("%s@%s" % (name, ver))
        p = subprocess.run(cmd, cwd=profile_dir(), capture_output=True, text=True,
                           env={**os.environ, "DSH_HOME": os.path.abspath(dsh_home())},
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        try:
            with open(os.path.join(profile_dir(), "node_modules", name, "package.json"),
                      encoding="utf-8") as f:
                installed = json.load(f)["version"]
        except (OSError, ValueError, KeyError):
            installed = None
        if p.returncode == 0 and installed == ver:
            ok.append(name)
            log("    ✓ %s 完成" % name)
        elif installed == ver:
            incomplete.append(name)
            log("    ⚠ %s 版本已更新，但安装未完成：%s" % (name, out[-600:] or "(无输出)"))
        else:
            failed.append(name)
            log("    ✗ %s 失败（未确认目标版本）：%s" % (name, out[-600:] or "(无输出)"))
        if "ERR_PNPM_IGNORED_BUILDS" in out:
            log("    依赖安装脚本被拦截；请核对后仅批准必要版本，不要关闭整体安全检查。")
        if on_progress:
            on_progress(i)
    return ok, incomplete, failed


def validate_uninstall(name, declared):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", PROFILE):
        raise ValueError("无效的 profile 名称，只支持字母、数字、点、下划线和连字符")
    if (not isinstance(name, str) or len(name) > 214 or
            not re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", name)):
        raise ValueError("无效的插件包名")
    if name.startswith(SKIP):
        raise ValueError("不能卸载 DSH 官方宿主包")
    current = {n: d for n, d, _ in installed_plugins()}
    if name not in current:
        raise ValueError("插件已不在当前 profile，请重新检查")
    if current[name] != declared:
        raise ValueError("插件来源已变化，请重新检查后再卸载")


def remove_unused_patch_entries(name, output, log):
    """只移除 pnpm 已确认闲置、且属于所选插件的补丁登记；不删除补丁文件。"""
    if "ERR_PNPM_UNUSED_PATCH" not in output:
        return False
    message = re.search(r"The following patches were not used: ([^\r\n]+)", output)
    if not message:
        return False
    keys = {key.strip() for key in message[1].split(",")
            if key.strip() == name or key.strip().startswith(name + "@")}
    if not keys:
        return False
    path = os.path.join(profile_dir(), "pnpm-workspace.yaml")
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        original = f.read()
    lines = original.decode("utf-8-sig").splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines)
               if re.match(r"^(?:patchedDependencies|'patchedDependencies'|\"patchedDependencies\"):", line)]
    if not headers:
        return False
    unsupported = "补丁配置不是简单块映射，未自动修改；请核对补丁登记后再卸载"
    if len(headers) != 1:
        raise RuntimeError(unsupported)
    start = headers[0]
    if not re.fullmatch(r"[^:]+:\s*(?:#[^\r\n]*)?\r?\n?", lines[start]):
        raise RuntimeError(unsupported)
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t", "#"))):
        end += 1
    entries, indent = {}, None
    for i in range(start + 1, end):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entry = re.fullmatch(r"( +)(?:\"([^\"]+)\"|'([^']+)'|([^\s:'\"]+)):[ \t]+([^\r\n]+)\r?\n?", line)
        if not entry or any(c in entry[5] for c in "&*{}[]|>"):
            raise RuntimeError(unsupported)
        key = entry[2] or entry[3] or entry[4]
        if indent is None:
            indent = entry[1]
        if indent != entry[1] or key in entries or key == "<<":
            raise RuntimeError(unsupported)
        entries[key] = i
    selected = keys.intersection(entries)
    if not selected:
        return False
    removed = {entries[key] for key in selected}
    if len(removed) == len(entries):
        lines[start] = lines[start].replace(":", ": {}", 1)
    updated = "".join(line for i, line in enumerate(lines) if i not in removed).encode("utf-8")
    if original.startswith(b"\xef\xbb\xbf"):
        updated = b"\xef\xbb\xbf" + updated
    # shortcut: 仅支持普通块映射；出现复杂 YAML 时需使用正式 YAML 编辑器处理。
    with tempfile.NamedTemporaryFile(dir=profile_dir(), prefix=".updater-patch-", delete=False) as f:
        f.write(updated)
        temporary = f.name
    try:
        with open(path, "rb") as f:
            if f.read() != original:
                raise RuntimeError("补丁配置已被其他程序修改，停止卸载重试（未替换配置）")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    log("已移除所选插件的闲置补丁登记（保留补丁文件）：%s" % ", ".join(sorted(selected)))
    return True


def run_uninstall(name, declared, log):
    """只移除已安装的第三方 direct dependency；保留账号、聊天和插件数据。"""
    validate_uninstall(name, declared)
    dsh = find_dsh()
    if not dsh:
        raise RuntimeError("找不到 dsh 命令，请配置 DSH_UPDATER_DSH 为 dsh.cmd 的完整路径")
    backup_root = os.path.join(dsh_home(), "backups", "plugin-updater")
    os.makedirs(backup_root, exist_ok=True)
    backup = tempfile.mkdtemp(prefix="uninstall-", dir=backup_root)
    for filename in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "cordis.patch.yml"):
        source = os.path.join(profile_dir(), filename)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(backup, filename))
    log("卸载前配置备份：%s（不含插件代码，不可仅复制清单还原）" % backup)
    validate_uninstall(name, declared)
    cmd = (["cmd", "/c", dsh] if os.name == "nt" else [dsh]) + \
          ["plugin", "--profile", PROFILE, "remove"]
    if os.path.exists(os.path.join(profile_dir(), "pnpm-workspace.yaml")):
        cmd.append("-w")
    cmd.append(name)
    log("卸载 %s …" % name)
    p = subprocess.run(cmd, cwd=profile_dir(), capture_output=True, text=True,
                       env={**os.environ, "DSH_HOME": os.path.abspath(dsh_home())},
                       encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode and "ERR_PNPM_UNUSED_PATCH" in out:
        try:
            validate_uninstall(name, declared)
            if remove_unused_patch_entries(name, out, log):
                validate_uninstall(name, declared)
                log("闲置补丁登记已清理，重试官方卸载（仅一次）…")
                p = subprocess.run(cmd, cwd=profile_dir(), capture_output=True, text=True,
                                   env={**os.environ, "DSH_HOME": os.path.abspath(dsh_home())},
                                   encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = ((p.stdout or "") + (p.stderr or "")).strip()
        except Exception as e:
            raise RuntimeError("卸载未完成；配置备份：%s\n%s" % (backup, e)) from e
    if p.returncode or any(n == name for n, _, _ in installed_plugins()):
        raise RuntimeError("卸载未完成，profile 可能已部分改变，请检查后再重试。配置备份：%s\n%s" %
                           (backup, out[-2000:] or "命令未移除该依赖"))
    log("✓ 已卸载 %s；重启 DSH 后生效。配置备份：%s" % (name, backup))
    return backup


def uninstall_confirm_text(row):
    return ("将从当前 profile 卸载：\n\n%s  %s\n来源：%s\n\n"
            "请先保存任务并退出 DSH，且不要同时运行其他插件管理器。\n"
            "这不是停用：其他依赖它的插件可能失效。\n"
            "通过官方 remove 卸载；仅清理它确认闲置的本插件补丁登记，保留补丁文件。\n"
            "不主动清理账号、聊天或插件数据；自定义配置引用不会自动修复。\n"
            "卸载前保存配置备份（不是完整回滚）；重新安装仍需原来源可用。\n\n确认卸载？" %
            (row["name"], row["installed"], row["declared"]))


STATE_TEXT = {"newer": "有更新", "same": "已是最新", "preview": "本地更新（预览版）",
              "error": "查询失败", "skip": "跳过（非 npm）",
              "incomplete": "版本已更新，安装未完成", "failed": "更新失败"}


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
    btn_remove = ttk.Button(bar, text="卸载选中插件", state="disabled")
    btn_remove.pack(side="left", padx=(8, 0))
    progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
    progress.pack(side="right")

    wrap = ttk.Frame(root, padding=(14, 0, 14, 8))
    wrap.pack(fill="both", expand=True)
    tree = ttk.Treeview(wrap, columns=("name", "installed", "latest", "state"),
                        show="headings", height=10, selectmode="browse")
    for c, t, w, a in (("name", "插件", 380, "w"), ("installed", "当前版本", 120, "center"),
                       ("latest", "最新版本", 120, "center"), ("state", "状态", 160, "w")):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor=a, stretch=(c == "name"))
    for tag, color in (("newer", "#0a7d32"), ("error", "#b3261e"), ("preview", "#8a6d00"),
                       ("skip", "#777777"), ("incomplete", "#8a6d00"), ("failed", "#b3261e")):
        tree.tag_configure(tag, foreground=color)
    ys = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    tree.pack(side="left", fill="both", expand=True)
    ys.pack(side="right", fill="y")

    logbox = tk.Text(root, height=8, wrap="word", state="disabled", font=("Consolas", 9),
                     background="#1e1e1e", foreground="#d4d4d4", relief="flat")
    logbox.pack(fill="x", padx=14, pady=(0, 12))

    state = {"rows": [], "busy": False}

    def selected_row():
        selected = tree.selection()
        if not selected:
            return None
        name = tree.item(selected[0], "values")[0]
        return next((r for r in state["rows"] if r["name"] == name), None)

    def sync_remove(event=None):
        row = selected_row()
        allowed = not state["busy"] and row and not row["name"].startswith(SKIP)
        btn_remove.state(["!disabled"] if allowed else ["disabled"])

    def close():
        if state["busy"]:
            messagebox.showinfo("正在处理", "请等待当前操作完成后再关闭窗口。", parent=root)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    tree.bind("<<TreeviewSelect>>", sync_remove)

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
        btn_remove.state(["disabled"])
        progress.start(12)
        set_status("正在检查…")
        state["rows"] = []
        paint([])

        def finish(rows, error):
            state["busy"] = False
            progress.stop()
            btn_check.state(["!disabled"])
            if error:
                set_status("检查失败")
                log("检查失败：%s" % error)
            else:
                state["rows"] = rows
                paint(rows)
                newer = [r for r in rows if r["state"] == "newer"]
                btn_all.state(["!disabled"] if newer else ["disabled"])
                set_status("共 %d 个插件，%d 个可更新 · %s"
                           % (len(rows), len(newer), time.strftime("%H:%M:%S")))
                log("检查完成：%d 个插件，%d 个可更新" % (len(rows), len(newer)))
            sync_remove()
            if after:
                after()

        def work():
            rows, error = [], None
            try:
                rows = check_all()
            except Exception as e:
                error = str(e)
            root.after(0, lambda: finish(rows, error))

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
        btn_remove.state(["disabled"])
        progress.stop()
        progress.configure(mode="determinate", value=0, maximum=len(todo))
        set_status("正在更新…")

        def finish(ok, incomplete, failed):
            state["busy"] = False
            progress.configure(mode="indeterminate", value=0)
            summary = "完成 %d 个，未完成 %d 个，失败 %d 个" % (
                len(ok), len(incomplete), len(failed))
            set_status(summary if incomplete or failed else "更新完成，重启 DeepSeek Harness 后生效")
            log("—— %s ——" % summary)
            try:
                current = {n: (d, v) for n, d, v in installed_plugins()}
                for row in state["rows"]:
                    name = row["name"]
                    if name in current:
                        row["declared"], row["installed"] = current[name]
                    if name in incomplete:
                        row["state"] = "incomplete"
                    elif name in failed:
                        row["state"] = "failed"
                    elif name in ok:
                        row["state"] = "same"
                paint(state["rows"])
            except Exception as e:
                log("刷新本地版本失败，请重新检查：%s" % e)
            btn_check.state(["!disabled"])
            sync_remove()
            if after:
                after()

        def work():
            ok, incomplete, failed = [], [], []
            try:
                ok, incomplete, failed = run_update(
                    todo, log, on_progress=lambda i: root.after(
                        0, lambda i=i: progress.configure(value=i)))
            except Exception as e:
                log("更新失败：%s" % e)
                failed = [n for n, _ in todo]
            root.after(0, lambda: finish(ok, incomplete, failed))

        threading.Thread(target=work, daemon=True).start()

    def do_remove():
        row = selected_row()
        if state["busy"] or not row:
            return
        if not messagebox.askyesno("确认卸载", uninstall_confirm_text(row),
                                   icon="warning", default="no", parent=root):
            return
        state["busy"] = True
        btn_check.state(["disabled"])
        btn_all.state(["disabled"])
        btn_remove.state(["disabled"])
        progress.start(12)
        set_status("正在卸载 %s…" % row["name"])

        def finish(error):
            state["busy"] = False
            progress.stop()
            btn_check.state(["!disabled"])
            if error:
                messagebox.showerror("卸载失败", error, parent=root)
            else:
                messagebox.showinfo("卸载完成", "已卸载 %s。\n请重启 DeepSeek Harness 后生效。" %
                                    row["name"], parent=root)
            do_check()

        def work():
            error = None
            try:
                run_uninstall(row["name"], row["declared"], log)
            except Exception as e:
                error = str(e)
                log("卸载失败：%s" % error)
            root.after(0, lambda: finish(error))

        threading.Thread(target=work, daemon=True).start()

    btn_remove.configure(command=do_remove)
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
        ok, incomplete, failed = run_update(todo, print)
        print("成功 %d，未完成 %d，失败 %d" % (len(ok), len(incomplete), len(failed)))
        return 1 if incomplete or failed else 0
    build_ui().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
