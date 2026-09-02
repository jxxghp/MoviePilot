# Docker 容器启动处理流程

本文档说明 MoviePilot V3 镜像从 Docker 创建容器到后端进入 `ready` 状态的完整处理链，覆盖
`docker/` 下的控制脚本、更新事务、依赖自愈、浏览器和证书准备、Nginx、Python lifespan、
异常保活与退出清理。

本文基于 `v3` 分支 2026-09-02 的实现整理。实际行为以当前源码为准。

## 1. 文件职责

| 文件 | 运行位置与职责 |
| --- | --- |
| `docker/Dockerfile` | 构建 Python 环境、后端、前端、插件、站点资源和容器控制面；声明 `tini` 入口与 readiness 健康检查。 |
| `docker/launcher.sh` | 构建时复制为镜像根目录 `/entrypoint.sh`；以 root 校验、选择并固化本轮启动使用的控制脚本。 |
| `docker/entrypoint.sh` | 真正的容器启动编排器；加载配置，驱动更新、权限、浏览器、证书、Nginx、后端和退出处理。 |
| `docker/update.sh` | 被 `entrypoint.sh` source；处理未完成更新恢复、已准备 Release 安装、Dev 更新、依赖同步和载荷事务。 |
| `docker/browser.sh` | 被 `entrypoint.sh` source；选择持久化 CloakBrowser 缓存、校正权限并按需安装浏览器内核。 |
| `docker/cert.sh` | 被 `entrypoint.sh` source；校验证书、按需安装 acme.sh、签发证书并配置续期任务。 |
| `docker/nginx.template.conf` | 由环境变量渲染为 `/etc/nginx/nginx.conf`，提供前端静态文件、API 和 SSE 反向代理。 |
| `docker/nginx.common.conf` | HTTP/HTTPS server 共用的前端、API、SSE 和静态资源规则。 |
| `docker/docker_http_proxy.conf` | 挂载 Docker Socket 时启动的本地只监听代理，后端通过 `127.0.0.1:38379` 访问 Docker API。 |

## 2. 总体调用链

```text
Docker
  -> tini -g
    -> /entrypoint.sh                         # 镜像内置 launcher.sh
      -> 校验并选择控制脚本代际
      -> /run/moviepilot/control/<sha256>/entrypoint.sh
        -> 加载 /config/app.env
        -> 渲染 Nginx 配置
        -> source update.sh
          -> 恢复未完成更新
          -> 安装已准备的 Release/资源包，或执行 Dev 更新
        -> 必要时用更新后的控制脚本重新 exec 一次
        -> source browser.sh
        -> 映射 PUID/PGID
        -> 检查并按需恢复 Python 运行依赖
        -> 校正目录和缓存权限
        -> 准备 CloakBrowser 内核
        -> source cert.sh
        -> 启动前端 Nginx
        -> 可选启动 Docker Socket 代理
        -> gosu moviepilot python3 app/main.py
          -> Uvicorn/FastAPI lifespan
          -> 数据库迁移和全部生命周期组件
          -> /health/ready 返回 200
```

镜像的 `ENTRYPOINT` 为：

```text
/usr/bin/tini -g -- /entrypoint.sh
```

`tini -g` 负责回收子进程并把停止信号发送给整个进程组。根目录 `/entrypoint.sh` 不是业务
`entrypoint.sh`，而是构建时从 `launcher.sh` 复制出的最小可信启动器。

## 3. 控制脚本可信选择

### 3.1 为什么不直接执行 `/app/docker/entrypoint.sh`

Docker 的 Dev 更新可以在启动过程中整体替换 `/app`。如果当前 Shell 正在从 `/app/docker` 继续
source 其他脚本，可能出现同一次启动混用新旧脚本的情况。因此 launcher 会先选择完整的一代控制脚本，
再复制到只属于本轮启动的运行时快照目录。

候选目录：

| 优先级 | 目录 | 用途 |
| --- | --- | --- |
| 1 | `/app/docker` | 当前源码携带的控制脚本。 |
| 特殊恢复 | `/app.__update_previous__/docker` | 更新停留在 `prepared` 且旧代完整可信时，用旧控制脚本恢复事务。 |
| 兜底 | `/usr/local/lib/moviepilot/control` | 镜像构建时固化的控制脚本，不随 `/app` 更新。 |

### 3.2 信任检查

`source_bundle_is_trusted()` 对源码控制目录执行以下检查：

1. 目录及其上级路径不能是符号链接。
2. 目录及其上级路径必须归 root 所有，且 group/other 不可写。
3. 目录下所有 `.sh` 都必须是普通文件，不能是符号链接。
4. 必须至少包含 `entrypoint.sh`、`update.sh`、`browser.sh`、`cert.sh`。
5. 所有脚本必须通过 `bash -n`。
6. 按文件名排序后计算每个脚本的 SHA-256，再计算整组 manifest 的 SHA-256 作为代际编号。

通过检查的脚本会复制到：

```text
/run/moviepilot/control/<generation>/
```

快照目录权限为 `0700`，脚本权限为 `0500`。launcher 导出 `MP_CONTROL_DIR` 和
`MP_CONTROL_GENERATION` 后，`exec` 快照中的 `entrypoint.sh`。

如果源码脚本不可信或快照失败，会回退镜像内置脚本；镜像内置脚本自身也必须通过完整性和语法检查，
否则容器拒绝启动。

### 3.3 更新恢复时的代际选择

`/config/temp/__update_pending__` 的值会影响 launcher：

| 状态 | 控制脚本选择原则 |
| --- | --- |
| `prepared` | 优先使用可信的旧代 `/app.__update_previous__/docker`；旧代不存在时继续检查当前源码。 |
| `dependencies` / `blocked` | 不使用旧代控制脚本，避免数据库或当前载荷已经前进后执行缺少新迁移的旧逻辑。 |
| `committed` 或无标记 | 使用当前可信源码，失败时回退镜像内置版本。 |

## 4. 配置加载和 Nginx 渲染

### 4.1 基础环境

业务 `entrypoint.sh` 首先设置：

- `VENV_PATH` 默认 `/opt/venv`，并前置到 `PATH`；
- `UV_BIN` 默认 `/usr/local/bin/uv`；
- `CONFIG_DIR` 默认 `/config`；
- `PACKAGE_CACHE_ROOT` 默认 `/config/.cache`；
- `UV_CACHE_DIR` 默认 `/config/.cache/uv`。

### 4.2 `app.env` 优先级

脚本解析 `${CONFIG_DIR}/app.env`，配置优先级为：

```text
容器进程环境变量（包括显式空字符串）
  > /config/app.env
  > 脚本内置默认值
```

普通变量只加载脚本白名单中的启动配置；`ACME_ENV_*` 变量额外透传给证书流程。`app.env` 使用 Python
端 `set_key(..., quote_mode="always")` 对应的单引号格式，未按规范包裹的值只按字面量解析并输出告警。

### 4.3 Nginx 配置

`render_nginx_config()` 使用一次 `envsubst` 生成 `/etc/nginx/nginx.conf`：

- HTTP 默认监听 `NGINX_PORT=3000`；
- 后端 upstream 默认指向 `127.0.0.1:3001`；
- `ENABLE_SSL=true` 时追加 HTTPS server，证书固定读取 `/config/certs/latest/`；
- `/` 服务 `/public` 下的前端文件；
- `/api`、SSE 和 CookieCloud 路径转发到后端；
- Service Worker、Manifest、普通静态资源分别使用适合更新语义的缓存策略。

## 5. 启动前更新处理

`entrypoint.sh` 在 `/` 目录 source 当前控制快照中的 `update.sh`。脚本只定义函数和初始化更新状态，
随后由 entrypoint 按顺序调用恢复与更新入口。

### 5.1 一次性 Dev 更新

如果存在：

```text
/config/temp/moviepilot.pending_dev_update
```

entrypoint 会删除该标记，并只在本次启动中把 `MOVIEPILOT_AUTO_UPDATE` 临时设为 `dev`。更新阶段结束后
恢复原值，避免把一次性操作变成永久自动更新。

### 5.2 未完成更新恢复

更新事务使用以下目录：

```text
/app.__update_previous__
/public.__update_previous__
/config/temp/__update_pending__
```

状态含义与启动处理：

| 状态 | 含义 | 下次启动处理 |
| --- | --- | --- |
| `prepared` | 新载荷已准备，事务开始，但尚未确认依赖阶段或提交。 | 恢复旧载荷并清理事务；没有备份时按当前完整载荷结束未完成事务。 |
| `dependencies` | 正在同步新清单对应的共享虚拟环境。 | 当前 `/app`、`/public` 完整时保留当前载荷，改记 `blocked`，交给启动前依赖自愈；载荷不完整时恢复旧备份。 |
| `blocked` | 已决定保留当前载荷，等待依赖自愈。 | 继续保留当前载荷；依赖自愈成功后清理旧备份和事务标记。 |
| `committed` | 新载荷已切换完成，只剩旧备份清理。 | 清理旧备份并删除事务标记。 |

依赖阶段不能盲目回退旧 `/app`：数据库可能已经由当前程序迁移到新 revision，旧程序未必包含对应
Alembic migration。保留当前载荷并恢复其依赖，可以避免形成“新数据库 + 旧源码”的不可启动组合。

### 5.3 已准备的 Release/资源更新

稳定版更新不在容器启动时联网检查 GitHub Release，也不在 Shell 中比较版本号。后台更新服务会提前
下载并校验制品，用户确认重启后生成：

```text
/config/temp/moviepilot-update/prepared.json
/config/temp/moviepilot-update/install.json
/config/temp/moviepilot-update/state.json
```

启动时 `install.json` 优先于 `MOVIEPILOT_AUTO_UPDATE`。处理顺序：

1. 识别 `application`、`resources` 目标。
2. 校验后端、前端和资源文件存在且 SHA-256 一致。
3. 应用更新时解压后端和前端到临时目录，保留当前插件运行目录和 V3 站点资源。
4. 同时包含资源目标时，把已准备资源写入新后端的 `app/application/site/`。
5. 只有资源目标时，使用临时目录和备份目录原子替换当前资源文件。
6. 安装成功后逐项消费 `prepared.json`，删除 `install.json`；失败时写入 `state.json` 并保留可重试状态。

### 5.4 Dev 自动更新

仅当 `MOVIEPILOT_AUTO_UPDATE=dev` 时，启动脚本会联网获取 `v3` 分支源码和最新 V3 前端 Release。
GitHub 访问按 `GITHUB_PROXY`、`PROXY_HOST`、直连顺序选择；包索引按 `PIP_PROXY`、`PROXY_HOST`、
直连顺序选择。

后端或前端下载失败时不替换当前载荷。资源下载失败时优先复用当前资源；没有可用旧资源才终止更新。

### 5.5 载荷切换事务

应用更新的提交顺序为：

1. 下载、解压并验证后端和前端。
2. 暂存插件和站点资源。
3. 写入 `prepared`。
4. 依赖清单变化时写入 `dependencies`，再同步临时后端声明的依赖。
5. 把当前 `/app`、`/public` 移到旧代备份。
6. 把临时 `App`、`dist` 移到 `/app`、`/public`。
7. 写入 `committed`。
8. 删除旧代备份和事务标记。

依赖同步固定使用当前虚拟环境解释器，并执行等价于：

```text
uv sync --project <project> --locked --inexact --no-dev --no-install-project \
  --python /opt/venv/bin/python3 --no-default-groups --group <runtime-profile>
```

`runtime-profile` 由 `app.runtime.dependencies.profile` 根据标准 CPython 或 free-threaded CPython 选择。
回滚历史载荷时仍保留旧 `app/runtime/dependencies.py` 选择器兼容，防止旧快照无法恢复依赖。

### 5.6 控制脚本更新后重入

应用更新成功后，entrypoint 通过根目录 launcher 的 `--source-generation` 重新计算 `/app/docker` 代际。
如果新代际与当前 `MP_CONTROL_GENERATION` 不同，会：

```text
exec /entrypoint.sh --post-update-reexec
```

launcher 设置“更新已完成”和“已经重入”标志，新 entrypoint 不会重复安装更新。单次启动只允许重入一次；
如果控制脚本再次变化，会终止启动以避免无限循环。

## 6. 用户、依赖、浏览器和文件权限

### 6.1 运行用户映射

entrypoint 使用 `PUID`、`PGID` 修改镜像内 `moviepilot` 用户和组。后端、浏览器安装及 doctor 默认通过
`gosu moviepilot:moviepilot` 执行；`START_NOGOSU=true` 仅用于不降权的特殊运行场景。

### 6.2 后端依赖自愈

启动后端前先执行轻量探针：

```text
/opt/venv/bin/python3 -m app.doctor.dependencies
```

探针检查 Web 栈、中文分词和转换等启动关键能力。成功时不会执行 `uv sync`；失败时才选择包源并对
当前 `/app` 执行锁定依赖同步，随后再次探测。

包源不可用、同步失败或二次探测失败时，不再拉起后端，而是进入 Docker 诊断保活流程。

### 6.3 CloakBrowser 缓存

缓存目录选择顺序：

1. 显式绝对路径 `CLOAKBROWSER_CACHE_DIR`；
2. 已可用的 `/config/.browser/cloakbrowser`；
3. 已可用或单独挂载的历史 `${HOME}/.cloakbrowser`；
4. 新安装默认 `/config/.browser/cloakbrowser`。

显式目录不能是 `/`、`CONFIG_DIR` 或 `HOME` 本身。脚本先确认目录可由 `moviepilot` 读写，再用
`cloakbrowser.binary_info()` 判断内核是否可执行；缺失时以 `moviepilot` 用户安装，失败只告警，首次实际
使用浏览器时仍可重试。

### 6.4 权限校正

默认启动避免递归扫描大体积浏览器缓存，只处理缓存根和直接子项。主要处理：

- V3 站点资源目录；
- 插件运行目录根；
- `HOME` 和 `CONFIG_DIR` 下除浏览器缓存外的内容；
- 配置目录外的自定义 uv 缓存；
- Nginx 运行和日志目录；
- `/etc/hosts` 与 `/tmp`。

`MOVIEPILOT_FORCE_CHOWN=true` 才会递归修复 `/app`、`/public` 和浏览器缓存。修复 `/app` 时会保留
`/app/docker` 为 root 所有且不可由 group/other 写入，保证下次 launcher 仍可信任源码控制脚本。

## 7. 证书与前置服务

### 7.1 证书处理

`cert.sh` 临时启用 `errexit` 和 `pipefail`，结束后恢复调用方原有 Shell 选项。

| 配置 | 行为 |
| --- | --- |
| `ENABLE_SSL=false` | 跳过证书处理。 |
| `ENABLE_SSL=true`、`AUTO_ISSUE_CERT=false` | 要求 `/config/certs/latest/fullchain.pem` 和 `privkey.pem` 已存在。 |
| `ENABLE_SSL=true`、`AUTO_ISSUE_CERT=true` | 要求 `SSL_DOMAIN` 和 `DNS_PROVIDER`；按需安装 acme.sh、加载 `ACME_ENV_*`、签发并安装证书。 |

自动签发证书保存到 `/config/certs/<domain>/`，并维护 `/config/certs/latest` 符号链接。存在 `cron` 时，
脚本写入 `/etc/cron.d/acme`，每天 03:00 执行续期检查。续期任务配置失败不会阻断已有证书启动。

### 7.2 Nginx 和 Docker Proxy

证书检查完成后启动主 Nginx。若 `/var/run/docker.sock` 是 Unix Socket，再使用独立配置启动 root Nginx，
监听 `127.0.0.1:38379` 代理 Docker API，之后重新修正 Nginx 目录权限。

## 8. Python 后端启动

entrypoint 设置 `UMASK` 后，以后台进程启动：

```text
gosu moviepilot:moviepilot /opt/venv/bin/python3 app/main.py
```

`app/main.py` 的主要步骤：

1. 修正直接执行脚本时的 `sys.path`，只让项目根目录作为应用导入入口。
2. 设置进程标题并读取运行配置。
3. 校验进程拓扑；全功能模式要求单 worker，安全模式允许降级拓扑。
4. 注册 SIGINT/SIGTERM 处理器。
5. 生产单进程模式创建 `MoviePilotServer` 并运行 Uvicorn。
6. FastAPI 应用通过 `app.factory.create_app()` 安装公开健康探针、中间件、异常处理和 lifespan。

### 8.1 FastAPI lifespan 启动顺序

生命周期组件按 `start_order` 串行启动：

| 顺序 | 组件 | 关键处理 |
| ---: | --- | --- |
| 1 | 文件日志 | 创建本次 lifespan 独占的文件日志 writer。 |
| 5 | 后台任务登记器 | 建立统一的后台任务所有权和关停入口。 |
| 10 | 数据库准备 | 校验迁移 lineage，按需备份，执行建表/Alembic upgrade，再确认唯一 head。 |
| 20 | HTTP 基础能力 | 配置默认 User-Agent 和共享异步传输。 |
| 25 | 站点访问端口 | 注入站点访问所需的网络、浏览器和 OCR 适配。 |
| 26 | Chain 外部端口 | 注入 Chain 使用的外部系统能力。 |
| 27 | Chain 网络端口 | 注入同步网络和系统适配。 |
| 30 | 领域依赖装配 | 注入识别规则、DNS、系统和 Rust 能力。 |
| 40 | 数据库引擎预热 | 物化同步与异步数据库引擎。 |
| 50 | 数据库连接预算 | 校验当前部署的连接池预算。 |
| 60 | 路由 | 注册主程序 API 路由。 |
| 65 | 插件运行时装配 | 构造插件市场和插件运行时依赖。 |
| 70 | 模块服务 | 构造唯一 HostRuntime、数据库 worker、配置、Agent/Chain/资源/事件等核心服务。 |
| 72 | 插件服务装配 | 发布插件安装、目录和动态路由服务。 |
| 75 | 消息队列 | 创建共享消息队列。 |
| 80 | 插件备份恢复 | 普通模式下恢复启动前插件备份。 |
| 90 | 插件 | 普通模式下加载插件。 |
| 100 | 定时器 | 普通模式下启动 Scheduler 和 Agent 定时任务。 |
| 110 | 监控器 | 普通模式下启动目录监控。 |
| 120 | 待处理整理回放 | 普通模式下恢复未完成整理任务。 |
| 130 | 命令服务 | 普通模式下注册命令入口。 |
| 140 | 工作流 | 普通模式下启动工作流后台服务。 |
| 150 | 插件同步与启动收尾 | 登记插件同步收尾任务，由 TaskRegistry 统一管理。 |

其中“模块服务”内部还会组装当前 lifespan 唯一的配置、数据库、认证、运行时、Agent、Chain、Outbox、
Transfer、Workflow 和 MoviePilot Server 服务，注册站点资源版本读取器、事件处理器和模块管理器。

`MOVIEPILOT_SAFE_MODE=true` 时跳过标记为 `NORMAL_ONLY` 的插件、调度器、监控、命令和工作流等组件，
但数据库、路由、核心模块服务和后台诊断入口仍会启动。

### 8.2 数据库就绪边界

数据库准备先读取当前 revision 和代码唯一 head：

- revision 不可识别、存在多个 current/head、或 current 不是目标 head 的祖先时直接失败；
- 已标记的旧数据库需要迁移时，先 Alembic upgrade，再按当前元数据补表；
- 新库或未标记旧库先建表，再执行 Alembic；
- 配置允许时，迁移前创建数据库备份；
- 迁移结束后再次验证数据库已经到达当前唯一 head。

只有该阶段完成，应用健康状态才标记 `database_ready`。

### 8.3 readiness

所有启用的 fail-fast 启动组件成功后，lifespan 才把应用标记为 `ready`。

entrypoint 同时在后台每秒请求：

```text
http://127.0.0.1:${PORT}/health/ready
```

成功后输出容器总启动耗时和后端就绪耗时。默认等待 300 秒，可通过
`MOVIEPILOT_BACKEND_READY_TIMEOUT` 调整。该等待任务只负责日志，不替代 Docker 健康状态。

Dockerfile 的 `HEALTHCHECK` 每 30 秒请求同一地址：数据库迁移和完整 lifespan 成功后返回 200；启动、
失败或关停阶段返回 503。`/health/live` 只表示进程和事件循环仍可响应。

## 9. 异常、重启和退出

### 9.1 信号退出

entrypoint 捕获 SIGINT/SIGTERM 后按顺序：

1. 停止前端 Nginx；
2. 等待 Python 完成 lifespan 逆序关停；
3. 停止 Docker Proxy Nginx；
4. 使用原退出码退出容器。

Python lifespan 会先撤销 readiness，再按组件声明的 `stop_order` 停止工作流、命令、插件、事件、Agent、
整理任务、模块服务、数据库和日志等资源。启动中途失败时，只清理已经启动或正在启动的组件。

### 9.2 应用内重启

应用请求重启时写入：

```text
/config/temp/moviepilot.intentional_restart
```

Python 退出后，entrypoint 删除标记，并确保容器以非零状态退出，把真正的重新创建/重启交给 Docker
restart policy。entrypoint 本身不在容器内递归拉起第二个 Python 主进程。

### 9.3 异常诊断保活

镜像默认：

```text
MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE=true
```

后端非预期退出、依赖恢复失败或更新回滚无法完成时，entrypoint 会运行一次 `moviepilot doctor`，然后
通过长时间 sleep 保持容器存活，便于执行：

```shell
docker exec -it <container> moviepilot doctor
```

设置 `MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE=false` 可恢复异常后直接退出容器的行为。保活只保留诊断
入口，不代表服务健康；Docker `HEALTHCHECK` 仍会保持失败。

## 10. 关键运行文件

| 路径 | 作用 |
| --- | --- |
| `/entrypoint.sh` | 镜像内置 launcher。 |
| `/usr/local/lib/moviepilot/control/` | 镜像构建时固化的控制脚本。 |
| `/run/moviepilot/control/<generation>/` | 本轮启动执行的只读控制快照。 |
| `/app` | 当前后端源码和插件运行目录。 |
| `/public` | 当前前端静态文件。 |
| `/config/app.env` | Docker 启动脚本和应用配置。 |
| `/config/.cache/uv` | 默认 uv 持久缓存。 |
| `/config/.browser/cloakbrowser` | 默认 CloakBrowser 持久缓存。 |
| `/config/temp/__update_pending__` | 载荷切换事务状态。 |
| `/app.__update_previous__` | 更新前后端备份。 |
| `/public.__update_previous__` | 更新前前端备份。 |
| `/config/temp/moviepilot-update/` | 后台下载的 Release/资源包及安装状态。 |
| `/config/temp/moviepilot.pending_dev_update` | 单次 Dev 更新请求。 |
| `/config/temp/moviepilot.intentional_restart` | 应用内重启请求。 |
| `/config/certs/latest/` | Nginx 使用的稳定证书路径。 |

## 11. 关键环境变量

| 变量 | 默认值 | 影响 |
| --- | --- | --- |
| `CONFIG_DIR` | `/config` | 配置、缓存、更新状态和证书根目录。 |
| `PUID` / `PGID` | `0` / `0` | 映射 `moviepilot` 运行用户。 |
| `UMASK` | `000` | 后端进程文件权限掩码。 |
| `PORT` | `3001` | 后端监听和 readiness 端口。 |
| `NGINX_PORT` | `3000` | HTTP 前端入口。 |
| `MOVIEPILOT_AUTO_UPDATE` | `false` | 只有 `dev` 会触发启动时分支更新；稳定版使用准备清单。 |
| `MOVIEPILOT_SAFE_MODE` | `false` | 跳过普通模式专属的插件及后台控制面。 |
| `MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE` | `true` | 后端异常后是否保留容器供 doctor 诊断。 |
| `MOVIEPILOT_FORCE_CHOWN` | `false` | 是否执行大范围递归权限修复。 |
| `MOVIEPILOT_BACKEND_READY_TIMEOUT` | `300` | entrypoint readiness 日志等待秒数。 |
| `PACKAGE_CACHE_ROOT` | `/config/.cache` | 包管理缓存根目录。 |
| `UV_CACHE_DIR` | `/config/.cache/uv` | uv 缓存目录。 |
| `PIP_PROXY` | 空 | Python 包索引镜像。 |
| `GITHUB_PROXY` | 空 | GitHub 下载 URL 前缀。 |
| `PROXY_HOST` | 空 | GitHub/包命令使用的 HTTP(S) 代理。 |
| `GITHUB_TOKEN` | 空 | Dev 更新访问 GitHub 时的令牌。 |
| `CLOAKBROWSER_CACHE_DIR` | 自动选择 | 浏览器内核缓存位置。 |
| `ENABLE_SSL` | `false` | 是否渲染并启用 HTTPS server。 |
| `AUTO_ISSUE_CERT` | `false` | 是否自动签发证书。 |
| `SSL_DOMAIN` / `SSL_EMAIL` / `DNS_PROVIDER` | 空 | acme.sh 签发参数。 |

## 12. 维护约束

后续修改 Docker 启动流程时应保持以下边界：

1. 控制脚本必须按完整代际执行，不能在同一次启动中直接混用更新前后的 `/app/docker/*.sh`。
2. Release 更新由后台下载和用户确认驱动；启动脚本只消费已校验清单，不恢复启动时 GitHub Release 查询和 Shell 版本比较。
3. 更新载荷与共享虚拟环境必须作为一个可恢复事务处理，不能留下新源码配旧依赖或旧源码配新数据库的混合状态。
4. 标准 V3 与 V3t 依赖恢复必须复用 `app.runtime.dependencies.profile`，不能使用默认组覆盖当前 ABI profile。
5. 站点资源只安装到 `app/application/site/`；历史目录仅用于更新旧载荷时读取兼容资源。
6. readiness 只能在数据库 head 校验和完整 lifespan 成功后发布，不能退化为进程存在或普通接口可访问。
7. 后端异常保活是诊断机制，不是成功状态；外部编排必须继续以 `/health/ready` 判断可接流量。
