# DSH 插件自动更新

一个 300 多行、零依赖的 Windows 小工具：列出 DeepSeek Harness 桌面 profile 里装的所有插件，对比 npm 上的最新版本，点一下「一键更新」就按官方方式装好。

![截图](screenshot.png)

> 非官方工具，与 DeepSeek 官方无关。

## 为什么用它

`dsh plugin` 只负责装，不负责查。插件多了以后想知道哪个该升级，就得逐个 `npm view`。这个工具把「查」和「升」放一个窗口里，并且**和 dshmarket 商店走同一条安装链路**（`dsh plugin --profile <profile> add`），不会把 profile 弄脏。

## 系统要求

- Windows 10/11
- Python 3.8+（用系统自带的 tkinter，无第三方依赖）
- 已安装 DeepSeek Harness 桌面版

## 用法

双击 `dsh-plugin-updater.pyw` 即可（`.pyw` 不会弹黑框）。也可以：

```bat
pythonw dsh-plugin-updater.pyw          :: 打开界面
python  dsh-plugin-updater.pyw --check   :: 只检查并打印，适合计划任务
python  dsh-plugin-updater.pyw --update  :: 直接更新所有可更新项
```

装完需要重启 DeepSeek Harness 才生效。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DSH_UPDATER_PROFILE` | `desktop` | 要检查的 profile 名 |
| `DSH_UPDATER_REGISTRY` | `https://registry.npmjs.org` | npm registry，换镜像时用 |
| `DSH_HOME` | 自动探测 | DSH 数据目录；探测顺序见 `dsh_home()` |

## 工作原理

1. 读 `<DSH_HOME>/profiles/<profile>/package.json` 的 `dependencies`。
2. 并发查 `registry/<pkg>/latest`，按 semver 比较（含预发布版）。
3. 更新时串行执行 `dsh plugin --profile <profile> add [-w] <pkg>@<ver>`；profile 里有 `pnpm-workspace.yaml` 时才加 `-w`。

## 已知限制

- 只支持 Windows（界面用 tkinter，安装命令走 `cmd /c`）。
- 只更新来自 npm 的插件。写成 `github:` / `file:` / `link:` 的依赖会显示「跳过」，因为查 registry 的 latest 对它们没意义。
- 本地版本高于 registry 时显示「本地更新（预览版）」，不会被降级。
- 更新的是 **latest 正式版**，不跟进预发布通道。
- 界面只有中文。

## 测试

```bat
python -m unittest -v test_updater.py
```

## 许可证

[MIT](LICENSE)
