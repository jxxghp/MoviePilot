# MoviePilot Doctor 诊断与自救

`moviepilot doctor` 是离线诊断入口，适合 WebUI、后端 API、Agent 或插件都不可用时使用。它不调用 MoviePilot 后端 API，而是直接检查本地配置、运行时文件、进程、端口、日志、依赖、数据库、前端资源和 Docker 环境。

## 快速使用

本地源码安装：

```shell
moviepilot doctor
moviepilot doctor --json
moviepilot doctor --fix
```

安全模式启动：

```shell
moviepilot start --safe
```

Docker 容器仍在运行或处于诊断保活状态：

```shell
docker exec -it <container> moviepilot doctor
docker exec -it <container> moviepilot doctor --json
```

容器已经退出时，可用同一镜像挂载配置目录运行：

```shell
docker run --rm --entrypoint python -v <config-dir>:/config <image> -m app.cli doctor
```

## 诊断内容

Doctor 默认执行只读检查：

- 运行路径：程序目录、配置目录、日志目录、Python 解释器
- 关键配置：`API_TOKEN`、`PORT`、`NGINX_PORT`、代理格式、安全模式
- 进程与端口：后端、前端端口监听状态，runtime 文件是否过期
- 日志线索：后端日志、启动日志、前端日志和插件日志中的近期错误
- 核心依赖：FastAPI、Pydantic、SQLAlchemy、Uvicorn、CloakBrowser 等是否可导入
- 数据库：SQLite 只读打开和完整性检查；PostgreSQL 默认做配置检查
- 前端资源：`version.txt`、`service.js` 或核心静态文件是否存在
- Docker：`/config`、虚拟环境和容器内 `moviepilot` 命令是否可用

`--deep` 会启用可能较慢或更依赖环境的检查，例如 PostgreSQL TCP 连通性。

整体状态只聚合会影响 MoviePilot 核心运行的诊断项。插件独立日志以及主日志中可明确识别的插件子系统异常仍会作为 `warn/degraded` 诊断项保留，但其 `affects_report_status` 为 `false`，不会单独把整体状态从 `healthy` 降为 `degraded`；同一日志中若还存在核心错误，核心错误仍会参与状态聚合。

## 自救能力

`moviepilot doctor --fix` 只做白名单安全修复：

- 清理指向已退出进程的 runtime 文件
- 在未被系统环境变量锁定时，为缺失或过短的 `API_TOKEN` 生成合规值

Doctor 不会自动删除数据库、修改 Docker Compose、回滚迁移、禁用多个插件或删除用户数据。

## 安全模式

`moviepilot start --safe` 或 `MOVIEPILOT_SAFE_MODE=true` 会在本次启动中跳过：

- 第三方插件加载与插件同步
- 调度器和 Agent 定时任务
- 目录监控
- 命令注册
- 工作流后台服务

安全模式不修改用户配置，适合插件、调度任务或 Agent 导致后端无法启动时先恢复后台入口。修复问题后移除环境变量或使用普通 `moviepilot start` 重启即可恢复完整能力。

## Docker 诊断保活

Docker 镜像默认设置 `MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE=true`。当后端主进程非正常退出时，entrypoint 不会立刻退出容器，而是打印一次 doctor 报告并保持容器运行，方便执行：

```shell
docker exec -it <container> moviepilot doctor
```

如果需要恢复旧行为，可设置：

```env
MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE=false
```

Dockerfile 同时提供 `HEALTHCHECK`，用于标记容器健康状态。是否自动重启仍由 Docker Compose、NAS 平台或 Docker restart policy 决定。

## Issue 反馈集成

`feedback-issue` skill 的诊断收集脚本会自动调用 `moviepilot doctor --json`，并把 doctor 摘要写入预览和最终 Issue 正文。完整 doctor JSON 存在运行时 diagnostics 文件中，默认不会直接贴入 Issue，避免泄露本机路径和过长输出。
