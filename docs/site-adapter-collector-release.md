# 站点适配采集器下载说明

普通用户优先使用 MoviePilot 正式 Release 提供的单文件采集器。单文件已经包含 Python 和采集器依赖，不需要安装 Python、pip、Git、MoviePilot 后端，也不需要下载源码。电脑只需已安装 Chrome、Edge 或 Chromium 浏览器。

## 选择下载文件

请只从 MoviePilot 官方 GitHub Release 下载与系统匹配的文件：

| 系统 | 下载文件 | 用户侧运行环境 |
|---|---|---|
| Windows | `moviepilot-site-collector-windows.exe` | Chrome、Edge 或 Chromium |
| macOS | `MoviePilot-Site-Collector-macOS.zip` | Chrome、Edge 或 Chromium |
| Linux | `moviepilot-site-collector-linux` | Chrome、Edge 或 Chromium |

每个程序旁边还有同名的 `.sha256` 文件，可用于核对下载文件是否完整。GitHub Actions 的手动构建产物主要用于维护者测试；普通用户应使用正式 Release 资产。

## 运行采集器

Windows 用户下载后双击 `.exe`，按窗口提示操作即可。macOS 用户解压 ZIP 后双击 `start-site-adapter-collector.command`，不要打开构建目录中的 `.pkg` 文件。Linux 用户在下载目录打开终端，只需首次赋予执行权限后运行：

```bash
chmod +x moviepilot-site-collector-linux
./moviepilot-site-collector-linux
```

运行后只需输入站点首页地址，随后在弹出的临时浏览器中登录并搜索，最后回到采集器按回车。采集器会在当前目录生成 `moviepilot-site-capture-*.zip`，用户只需把这个 ZIP 附加到站点适配 Feature Request，不需要提交任何源码、Cookie 或 HTML。

## 系统安全提示

当前自动构建产物尚未接入 Windows 或 Apple 代码签名。Windows SmartScreen 或 macOS Gatekeeper 可能因此显示安全提示。仅在文件来自 MoviePilot 官方 GitHub Release，且校验摘要一致时运行；不要从聊天、网盘或第三方站点接收采集器。

如果系统阻止运行，可改用随 MoviePilot 源码提供的本地采集脚本；该方式需要 Python 3.11 及完整后端依赖，不适合作为普通用户的首选路径。

## 维护者发布流程

`.github/workflows/site-adapter-collector.yml` 支持手动触发，也会在 Release 发布后自动构建 Windows、macOS 和 Linux 单文件程序。每个平台先执行 `--help` 启动检查，再上传程序及 SHA-256 摘要为 Workflow Artifact。Release 事件会在三个平台全部成功后，把文件附加到触发本次任务的 Release；手动触发时填写已有的 `release_tag` 也会上传到该 Release，留空则只生成 3 天的测试 Artifact。
