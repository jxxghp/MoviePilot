# 站点适配采集器下载说明

普通用户优先使用 [站点采集器独立 Release](https://github.com/jxxghp/MoviePilot/releases?q=site-adapter-collector-v&expanded=true) 提供的单文件采集器，选择列表中最新版本。采集器使用 `site-adapter-collector-v1.0.x` 独立标签，与 MoviePilot 后端版本分开发布。单文件已经包含 Python 和采集器依赖，不需要安装 Python、pip、Git、MoviePilot 后端，也不需要下载源码。电脑只需已安装 Chrome、Edge 或 Chromium 浏览器。

## 选择下载文件

请只从官方 [站点采集器独立 Release](https://github.com/jxxghp/MoviePilot/releases?q=site-adapter-collector-v&expanded=true) 下载与系统匹配的文件；MoviePilot 主程序 Release 不再提供采集器附件：

| 系统 | 下载文件 | 用户侧运行环境 |
|---|---|---|
| Windows | `moviepilot-site-collector-windows.exe` | Chrome、Edge 或 Chromium |
| macOS | `MoviePilot-Site-Collector-macOS.zip` | Chrome、Edge 或 Chromium |
| Linux | `moviepilot-site-collector-linux` | Chrome、Edge 或 Chromium |

每个程序旁边还有同名的 `.sha256` 文件，可用于核对下载文件是否完整。程序启动时显示版本，也可执行 `--version` 查看。GitHub Actions 的手动构建产物主要用于维护者测试；普通用户应使用采集器独立 Release 资产。

## 运行采集器

Windows 用户下载后双击 `.exe`，按窗口提示操作即可。macOS 用户解压 ZIP 后双击 `start-site-adapter-collector.command`，不要打开构建目录中的 `.pkg` 文件。Linux 用户在下载目录打开终端，只需首次赋予执行权限后运行：

```bash
chmod +x moviepilot-site-collector-linux
./moviepilot-site-collector-linux
```

运行后只需输入站点首页地址，随后在弹出的临时浏览器中登录并搜索，最后回到采集器按回车。采集器会在当前目录生成 `moviepilot-site-capture-*.zip`，用户只需把这个 ZIP 附加到站点适配 Feature Request，不需要提交任何源码、Cookie 或 HTML。

## 系统安全提示

当前自动构建产物尚未接入 Windows 或 Apple 代码签名。Windows SmartScreen 或 macOS Gatekeeper 可能因此显示安全提示。仅在文件来自官方 [站点采集器独立 Release](https://github.com/jxxghp/MoviePilot/releases?q=site-adapter-collector-v&expanded=true)，且校验摘要一致时运行；不要从聊天、网盘或第三方站点接收采集器。

如果系统阻止运行，可改用随 MoviePilot 源码提供的本地采集脚本；该方式需要 Python 3.14+ 及完整后端依赖，不适合作为普通用户的首选路径。

## 维护者发布流程

`.github/workflows/site-adapter-collector.yml` 在 `v3` 分支的采集器源码、依赖、启动脚本或打包工作流变化时触发，支持手动运行。后端 Release 发布及普通后端代码、文档、测试改动不会触发采集器构建。

构建前会与上一个已正式发布的采集器标签比较输入文件。没有变化就跳过构建和发版；首次发布沿用源码中的 `COLLECTOR_VERSION`（当前为 `1.0.1`），后续有变化时自动递增补丁版本。工作流只在 Runner 构建副本中写入版本，使程序、`--version` 和脱敏包中的 `collector_version` 一致，不向 `v3` 分支回写版本提交。采集包的 `format_version` 协议版本保持独立。

每个平台先执行 `--help` 和 `--version` 启动检查，再上传程序及 SHA-256 摘要。三个平台全部成功后，工作流创建独立版本 Release，自动生成仅包含采集器相关提交的变更记录，并设置 `latest=false`，使主程序的最新 Release 入口继续指向后端版本。

手动触发时 `publish` 默认为 `true`，仍遵守“输入变化才发版”。设为 `false` 时可重复构建测试，只生成保留 3 天的 Artifact，不创建 Release、不消耗版本号。历史主程序 Release 中的采集器附件会迁移移除，下载请始终使用上面的独立 Release 入口。
