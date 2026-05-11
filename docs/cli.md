# MoviePilot CLI

`moviepilot` 是 MoviePilot 本地源码模式的一体化入口，负责本地安装、初始化、更新，以及前后端服务管理。

## 一键安装

```shell
curl -fsSL https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/scripts/bootstrap-local.sh | bash
```

脚本会自动：

- 检测操作系统
- 自动检查并尽量安装 `git`、`curl`、`Python 3.11+`
- 克隆 `MoviePilot`
- 安装后端依赖
- 按当前仓库 `version.py` 中的 `FRONTEND_VERSION` 下载对应前端 release 的 `dist.zip`
- 下载 `MoviePilot-Resources` 主分支资源
- 将 `resources.v2/*` 同步到后端 [app/helper](/Users/jxxghp/PycharmProjects/MoviePilot/app/helper)
- 下载本地 Node 运行时并安装前端运行依赖
- 执行初始化向导
- 创建全局 `moviepilot` 命令
- 默认启动前后端服务

说明：

- 如果系统里已经有可用的 `Python 3.11+`，脚本会优先直接复用本地解释器
- 如果系统里没有可用的 `Python 3.11+`，脚本会再尝试自动补齐运行环境
- Linux 下安装系统依赖时通常需要 `sudo`
- 复用已有仓库时，脚本现在只会因为已跟踪源码改动而阻止自动更新，不会再被 `.DS_Store` 之类未跟踪文件卡住

如果安装完成后当前终端仍提示找不到 `moviepilot`：

- 重新打开终端
- 如果脚本提示使用了 `~/.local/bin`，执行 `source ~/.zshrc` 或 `source ~/.bashrc`

## 配置目录

本地 CLI 默认将配置目录放在程序目录外，避免直接删除程序目录时把配置一并删掉。

- macOS：`~/Library/Application Support/MoviePilot`
- Linux：`${XDG_CONFIG_HOME:-~/.config}/moviepilot`

如果在交互式终端中执行一键安装脚本，或直接执行 `moviepilot setup` / `moviepilot init` 且未传入 `--config-dir`，程序会先询问配置目录，并把上面的默认路径作为默认值展示出来。

可以在安装或初始化时手动指定：

```shell
moviepilot setup --config-dir /path/to/moviepilot-config
moviepilot init --config-dir /path/to/moviepilot-config
```

查看当前实际配置目录：

```shell
moviepilot config path
```

## 目录说明

- 后端代码：仓库根目录
- 外置配置目录：`moviepilot config path` 输出的 `Config Dir`
- 前端静态文件：`public/`
- 前端本地 Node 运行时：`.runtime/node/`
- 后端日志：`<Config Dir>/logs/moviepilot.log`
- 后端启动日志：`<Config Dir>/logs/moviepilot.stdout.log`
  该文件同样受 `LOG_MAX_FILE_SIZE` 与 `LOG_BACKUP_COUNT` 控制
- 前端启动日志：`<Config Dir>/logs/moviepilot.frontend.stdout.log`

## 帮助与发现

根帮助：

```shell
moviepilot --help
moviepilot help
moviepilot commands
```

分级帮助：

```shell
moviepilot help install
moviepilot help init
moviepilot help setup
moviepilot help uninstall
moviepilot help update
moviepilot help agent
moviepilot help config
moviepilot help config set
moviepilot help tool
moviepilot help scheduler
```

配置项清单与说明：

```shell
moviepilot config keys
moviepilot config keys API
moviepilot config describe API_TOKEN
```

动态工具清单与参数说明：

```shell
moviepilot tool list
moviepilot tool show <tool_name>
```

## 完整命令清单

```text
moviepilot install deps
moviepilot install frontend
moviepilot install resources
moviepilot init
moviepilot setup
moviepilot uninstall
moviepilot update backend
moviepilot update frontend
moviepilot update all
moviepilot startup enable
moviepilot startup disable
moviepilot startup status
moviepilot agent
moviepilot start
moviepilot stop
moviepilot restart
moviepilot status
moviepilot logs
moviepilot version
moviepilot config path
moviepilot config list
moviepilot config get
moviepilot config set
moviepilot config keys
moviepilot config describe
moviepilot tool list
moviepilot tool show
moviepilot tool run
moviepilot scheduler list
moviepilot scheduler run
moviepilot help
moviepilot commands
```

## 安装命令

安装后端依赖：

```shell
moviepilot install deps
moviepilot install deps --python python3.11
moviepilot install deps --venv /path/to/venv
moviepilot install deps --recreate
moviepilot install deps --config-dir /path/to/moviepilot-config
```

说明：

- 默认会自动选择本地已安装的 `Python 3.11+` 解释器

安装前端 release：

```shell
moviepilot install frontend
moviepilot install frontend --version latest
moviepilot install frontend --version v2.9.31
moviepilot install frontend --node-version 20.12.1
moviepilot install frontend --config-dir /path/to/moviepilot-config
```

说明：

- 默认按当前仓库 `version.py` 中的 `FRONTEND_VERSION` 下载对应前端 release 的 `dist.zip`
- 如需覆盖默认行为，仍可显式传入 `--version latest` 或指定具体 tag
- 会自动安装本地 Node 运行时
- 会自动安装 `service.js` 所需的运行依赖

安装资源文件：

```shell
moviepilot install resources
moviepilot install resources --resources-repo /path/to/MoviePilot-Resources
moviepilot install resources --resource-dir /path/to/resources.v2
moviepilot install resources --config-dir /path/to/moviepilot-config
```

说明：

- 默认直接从 GitHub 下载 `MoviePilot-Resources` 主分支压缩包
- 会将 `resources.v2/*` 整体复制到 [app/helper](/Users/jxxghp/PycharmProjects/MoviePilot/app/helper)
- 这一步和 Docker 构建流程保持一致

## 初始化命令

初始化本地配置：

```shell
moviepilot init
moviepilot init --wizard
moviepilot init --skip-resources
moviepilot init --force-token
moviepilot init --superuser admin --superuser-password 'ChangeMe123!'
moviepilot init --config-dir /path/to/moviepilot-config
```

一体化安装：

```shell
moviepilot setup
moviepilot setup --wizard
moviepilot setup --frontend-version latest
moviepilot setup --node-version 20.12.1
moviepilot setup --skip-resources
moviepilot setup --recreate
moviepilot setup --superuser admin --superuser-password 'ChangeMe123!'
moviepilot setup --config-dir /path/to/moviepilot-config
```

`moviepilot setup` 会串行执行：

1. 安装后端依赖
2. 下载并安装前端 release
3. 下载并同步资源文件
4. 初始化本地配置

`--wizard` 会进入交互式初始化向导，支持配置：

- `API_TOKEN`
- 超级管理员用户名与密码
- 数据库类型
  默认 `SQLite`
  可切换为 `PostgreSQL`，并填写主机、端口、数据库名、用户名、密码
- 默认下载目录与媒体库目录
- AI Agent
  可按需启用，并配置 `LLM_PROVIDER`、`LLM_MODEL`、`LLM_API_KEY`、`LLM_BASE_URL`
- 用户站点认证
  可按需选择认证站点，并按站点要求填写用户名、UID、Passkey 等参数
- 开机自启
  可按需启用，MoviePilot 会根据当前操作系统注册登录自启动
- 下载器
- 媒体服务器
- 消息通知渠道

如果希望在自动化安装时直接预设超级管理员，也可以在一键安装脚本中透传：

```shell
curl -fsSL https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/scripts/bootstrap-local.sh | \
  bash -s -- --superuser admin --superuser-password 'ChangeMe123!'
```

说明：

- `--superuser-password` 更适合自动化场景，命令可能会出现在 shell 历史中
- 交互式 `--wizard` 会在初始化过程中提示输入超级管理员用户名和密码

## 开机自启命令

管理当前本地安装的开机自启：

```shell
moviepilot startup status
moviepilot startup enable
moviepilot startup disable
moviepilot startup enable --venv /path/to/venv
moviepilot startup enable --config-dir /path/to/moviepilot-config
```

说明：

- macOS 使用 `LaunchAgent`
- Linux 优先使用 `systemd --user`，当前环境不可用时自动回退到 `XDG autostart`
- Windows 使用当前用户的 Startup 启动目录
- 注册的启动项会调用本地 CLI 的统一启动入口，因此会同时拉起后端与前端

## 卸载命令

卸载本地安装产物：

```shell
moviepilot uninstall
moviepilot uninstall --venv /path/to/venv
moviepilot uninstall --config-dir /path/to/moviepilot-config
```

说明：

- 卸载时会先停止当前 CLI 管理的前后端服务
- 会删除本地虚拟环境、前端运行时、本地 Node 运行时、全局 `moviepilot` 软链接，以及同步到 `app/helper` 的资源文件
- 如果之前注册过开机自启，卸载时也会一并取消
- 会询问是否同时删除配置目录，默认不删除
- 如果当前使用的是仓库内 legacy `config/` 目录，确认删除后其中的 `category.yaml` 等配置文件也会一起删除
- 整个卸载流程包含两次确认
- 源码目录会保留，如需彻底移除仓库请在确认后手动删除项目目录

## 更新命令

更新后端：

```shell
moviepilot update backend
moviepilot update backend --ref latest
moviepilot update backend --ref v2
moviepilot update backend --ref v2.9.31
```

更新前端：

```shell
moviepilot update frontend
moviepilot update frontend --frontend-version latest
moviepilot update frontend --frontend-version v2.9.31
```

整体更新：

```shell
moviepilot update all
moviepilot update all --ref latest --frontend-version latest
moviepilot update all --skip-resources
```

说明：

- `update backend` 会更新 Git 仓库并重新安装后端依赖
- `update frontend` 会按当前仓库 `version.py` 中的 `FRONTEND_VERSION` 下载并替换前端 release
- `update all` 会先更新后端，再按更新后代码中的 `FRONTEND_VERSION` 更新前端，默认也会同步资源文件
- 更新前请先执行 `moviepilot stop`

## Agent 命令

直接给智能体发送一次请求：

```shell
moviepilot agent 帮我分析最近一次搜索失败的原因
moviepilot agent --user-id admin 帮我检查当前下载器配置
moviepilot agent --session cli-debug-1 帮我看看为什么没有自动整理
moviepilot agent --new-session 帮我总结当前系统配置有什么明显问题
```

说明：

- `moviepilot agent` 直接在本地环境里发起一次智能体请求
- 默认每次可自动创建新会话，也可以通过 `--session` 指定会话 ID
- 使用前需要先正确配置 LLM 相关参数，并打开 `AI_AGENT_ENABLE`

## 服务管理命令

`moviepilot start/stop/restart/status` 统一管理前后端。

启动、停止、重启与状态：

```shell
moviepilot start
moviepilot start --timeout 60
moviepilot stop
moviepilot stop --timeout 30 --force
moviepilot restart
moviepilot restart --start-timeout 60 --stop-timeout 30
moviepilot status
moviepilot version
```

说明：

- `start` 会先启动后端，再启动前端
- 如果开启了 `MOVIEPILOT_AUTO_UPDATE=release|true|dev`，`start/restart` 会在启动前尽力执行一次本地自动更新；更新失败只告警，不阻断当前启动
- 通过系统内置的重启入口触发重启时，本地 CLI 安装模式也会复用同一套前后端进程管理完成重启
- 前端默认监听 `NGINX_PORT`，默认值 `3000`
- 后端默认监听 `PORT`，默认值 `3001`
- 前端通过 `service.js` 代理 `/api` 与 `/cookiecloud` 到后端
- 本地前端代理在启动时会先确认后端可用；如果后端长时间不可用，前端也会自动退出，避免只剩半套服务

日志：

```shell
moviepilot logs
moviepilot logs --lines 100
moviepilot logs --stdio
moviepilot logs --frontend
moviepilot logs --follow
moviepilot logs --frontend --follow
moviepilot logs --stdio --follow
```

说明：

- 默认 `logs` 查看后端应用日志
- `--stdio` 查看后端启动标准输出
- `--frontend` 查看前端启动标准输出

## 配置命令

查看配置路径：

```shell
moviepilot config path
```

查看当前配置：

```shell
moviepilot config list
moviepilot config list --show-secrets
```

读取和写入单个配置：

```shell
moviepilot config get PORT
moviepilot config set PORT 3001
moviepilot config set NGINX_PORT 3000
moviepilot config set API_TOKEN your-token-here
```

查看所有可配置项：

```shell
moviepilot config keys
moviepilot config keys DB_
moviepilot config keys --show-current
moviepilot config keys --show-current --show-secrets
moviepilot config describe PORT
moviepilot config describe API_TOKEN --show-secrets
```

说明：

- `config list` 显示当前配置值
- `config keys` 显示配置项名称、类型和默认值
- `config describe` 显示单个配置项的类型、默认值和当前值

## Tool 命令

列出所有 MCP 工具：

```shell
moviepilot tool list
```

查看单个工具的参数说明：

```shell
moviepilot tool show query_schedulers
moviepilot tool show search_torrents
```

运行工具：

```shell
moviepilot tool run query_schedulers
moviepilot tool run search_torrents media_type=movie tmdb_id=12345
```

说明：

- `tool list` 用于动态发现当前服务可调用的工具
- `tool show` 会输出参数名、类型和描述
- `tool run` 参数格式固定为 `key=value`

## Scheduler 命令

列出调度任务：

```shell
moviepilot scheduler list
```

立即执行调度任务：

```shell
moviepilot scheduler run subscribe_refresh
```
