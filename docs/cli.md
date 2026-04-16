# MoviePilot CLI

`moviepilot` 是 MoviePilot 本地源码模式的一体化入口，用于安装后端、安装前端 release、同步资源文件、初始化配置，以及统一管理前后端服务。

## 一键安装

直接从仓库读取脚本并执行：

```shell
curl -fsSL https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/scripts/bootstrap-local.sh | bash
```

脚本会自动：

- 检测操作系统
- 检查 `git`、`curl`、`Python 3.12+`
- 克隆 `MoviePilot`
- 安装后端依赖
- 下载 `MoviePilot-Frontend` 最新 release 的 `dist.zip`
- 下载 `MoviePilot-Resources` 主分支资源
- 将 `resources.v2/*` 同步到后端 [app/helper](/Users/jxxghp/PycharmProjects/MoviePilot/app/helper)
- 下载本地 Node 运行时并安装前端运行依赖
- 创建全局 `moviepilot` 命令
- 默认启动前后端服务

## 目录说明

本地安装完成后，主要运行目录如下：

- 后端代码：仓库根目录
- 前端静态文件：`public/`
- 前端本地 Node 运行时：`.runtime/node/`
- 后端日志：`config/logs/moviepilot.log`
- 后端启动日志：`config/logs/moviepilot.stdout.log`
- 前端启动日志：`config/logs/moviepilot.frontend.stdout.log`

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
moviepilot install deps --python python3.12
moviepilot install deps --venv /path/to/venv
moviepilot install deps --recreate
```

安装前端 release：

```shell
moviepilot install frontend
moviepilot install frontend --version latest
moviepilot install frontend --version v2.9.31
moviepilot install frontend --node-version 20.12.1
```

说明：

- 默认下载 `MoviePilot-Frontend` 最新 release 的 `dist.zip`
- 会自动安装本地 Node 运行时
- 会自动安装 `service.js` 所需的运行依赖

安装资源文件：

```shell
moviepilot install resources
moviepilot install resources --resources-repo /path/to/MoviePilot-Resources
moviepilot install resources --resource-dir /path/to/resources.v2
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
```

一体化安装：

```shell
moviepilot setup
moviepilot setup --wizard
moviepilot setup --frontend-version latest
moviepilot setup --node-version 20.12.1
moviepilot setup --skip-resources
moviepilot setup --recreate
```

`moviepilot setup` 会串行执行：

1. 安装后端依赖
2. 下载并安装前端 release
3. 下载并同步资源文件
4. 初始化本地配置

`--wizard` 会进入交互式初始化向导，支持配置：

- `API_TOKEN`
- 默认下载目录与媒体库目录
- 下载器
- 媒体服务器
- 消息通知渠道

## 服务管理命令

`moviepilot start/stop/restart/status` 现在统一管理前后端。

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
- 前端默认监听 `NGINX_PORT`，默认值 `3000`
- 后端默认监听 `PORT`，默认值 `3001`
- 前端通过 `service.js` 代理 `/api` 与 `/cookiecloud` 到后端

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
- `config describe` 显示单个配置项的类型、默认值、当前值与配置文件位置
- 如果前后端正在运行，更新配置后需要 `moviepilot restart`

## 工具命令

工具命令依赖后端已启动，并且本地配置中存在有效的 `API_TOKEN`。

列出工具：

```shell
moviepilot tool list
```

查看工具参数：

```shell
moviepilot tool show search_media
```

调用工具：

```shell
moviepilot tool run search_media title="Inception" media_type=movie
moviepilot tool run query_schedulers
```

`tool list` 和 `tool show` 是查看“当前后端实际暴露的全部工具与参数”的推荐方式。

## 调度命令

查看调度任务：

```shell
moviepilot scheduler list
```

立即执行调度任务：

```shell
moviepilot scheduler run subscribe_search
```

## 推荐流程

首次安装：

```shell
moviepilot setup --wizard
moviepilot start
moviepilot status
```

日常维护：

```shell
moviepilot status
moviepilot logs --frontend
moviepilot logs --stdio
moviepilot config keys
moviepilot tool list
```
