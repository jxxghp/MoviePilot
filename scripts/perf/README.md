# MoviePilot Docker A/B Harness

该工具用同一个冻结 Docker substrate 对两个 Git commit 做源码级 A/B。派生镜像会先清空
`/app`，复制目标 commit 的完整 `git archive`，再从 substrate 注入镜像构建阶段生成的插件目录、
`sites.*.so` 和 `user.sites.v3.bin`。如果依赖或 Docker substrate 输入发生变化，工具会拒绝继续，
避免把外部构建输入漂移误算成性能收益。

工具不会读取工作区或容器内的 `app.env`，不挂载真实配置、媒体目录或 Docker socket。固定配置只使用
内置实验室占位凭据，样本不发布主机端口，并运行在无外网的 internal network。

## 一次性预热浏览器

默认从固定命名 volume `mp-perf-v3-browser-seed` 复制 CloakBrowser 缓存。该 volume 只需联网预热
一次：

```bash
docker volume create mp-perf-v3-browser-seed
docker run --rm \
  --mount source=mp-perf-v3-browser-seed,target=/moviepilot/.cloakbrowser \
  --entrypoint python3 \
  jxxghp/moviepilot-v3@sha256:925de1fdf1bb0312144bc818bc8ebaa999a9a159c6d14f1b48b0ff05edb7f720 \
  -m cloakbrowser install
```

也可以在 `seed` 或 `run` 时显式传入 `--allow-browser-download`，但该选项会让 seed 阶段联网；
正式 Before/After 样本仍然只克隆预热结果并使用 internal network。

## 分阶段执行

所有全局参数必须放在子命令前。结果默认写到系统临时目录下的
`moviepilot-perf-results/<campaign>/`。

```bash
PYTHON=../.venv/bin/python
CAMPAIGN=v3-perf-001

${PYTHON} scripts/perf/moviepilot_docker_ab.py \
  --campaign "${CAMPAIGN}" \
  build --before-ref upstream/v3 --after-ref HEAD

${PYTHON} scripts/perf/moviepilot_docker_ab.py \
  --campaign "${CAMPAIGN}" \
  seed

${PYTHON} scripts/perf/moviepilot_docker_ab.py \
  --campaign "${CAMPAIGN}" \
  sample --variant before --index 1 --points 1,5,10,30
```

开发 harness 时可以用小数分钟做短冒烟，例如 `--points 0,0.02`。正式数据必须保持
`1,5,10,30`。

未指定 `--scenario` 时仍使用 `idle-default`，样本目录和 Docker 资源名称与既有命令保持一致。

## 浏览器激活场景

浏览器场景使用 campaign browser seed 的独立克隆卷，不直接挂载或写入固定来源卷，也不会在样本
阶段下载浏览器。容器保持 internal network；探针只发送信号，`app.sdk.browser` 的导入、浏览器上下文
创建和本地 `data:` 页面校验都发生在主 MoviePilot Python 进程中。

非默认浏览器场景用于候选实现的 After 激活门禁；Before 不具备新 SDK，且旧实现启动时已经常驻
Xvfb，因此不能用同一个 `0 → 0` / `0 → 1` 不变量衡量。三轮 Before/After 空载收益仍由默认
`run` 的 `idle-default` 场景完成，浏览器场景用三个隔离的 After sample 记录冷激活成本。

```bash
../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-002-headless \
  sample --variant after --index 1 --scenario browser-headless --points 1,5,10,30

../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-002-headed \
  sample --variant after --index 1 --scenario browser-headed --points 1,5,10,30
```

- `browser-headless`：一次真实 headless context 激活，要求 Xvfb `0 → 0`；
- `browser-headed`：主进程内两个线程通过屏障并发调用
  `launch_browser_context(headless=False)`；要求两个真实 SDK 冷启动调用成功、额外上下文关闭后只保留一个、
  Capability observation 只有一个 `headed_browser_launch` generation/start，且 Xvfb `0 → 1`；
- 激活完成后再开始 `1/5/10/30m` 计时，JSON 保留激活前后 Engine 网络、working set、进程
  PSS/USS/RSS/线程、Xvfb 数量/PSS、`sys.modules` 和进程内 marker；
- 非默认场景结果保存在 `samples/<scenario>/<variant>-<index>/`，可与同 campaign 的 idle 样本并存，
  Markdown 中位数会按场景分组，不会混算。

## Agent 惰性物化场景

PERF-003 在既有 `AI_AGENT_ENABLE=false` 固定配置下增加两个 After-only 场景。探针只向主 MoviePilot
Python 进程发送信号；OpenAPI 生成和工具目录构造均发生在该解释器内，不通过 `docker exec` 启动
第二个 Python，也不调用真实 Agent、LLM provider 或外部 MCP。

先以 `f2e548e1` 冻结 Before，候选提交完成后把 `AFTER_COMMIT` 替换为其精确 commit：

```bash
../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-003 \
  build --before-ref f2e548e1 --after-ref AFTER_COMMIT

../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-003 \
  seed --browser-source-volume mp-perf-v3-browser-seed --replace
```

正式 idle-default 三组 A/B 仍使用原 `run` 合同；下面两个动作场景在同一 build/seed 后单独采 After，
不会覆盖 idle 结果：

```bash
../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-003 \
  run --before-ref f2e548e1 --after-ref AFTER_COMMIT \
  --browser-source-volume mp-perf-v3-browser-seed \
  --points 1,5,10,30 --replace --keep-resources

../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-003 \
  sample --variant after --index 1 --scenario agent-disabled-router --points 1,5,10,30

../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-003 \
  sample --variant after --index 2 --scenario agent-tool-catalog --points 1,5,10,30
```

- `agent-disabled-router`：直接从主进程 FastAPI app 生成完整 OpenAPI，确认 Agent、LLM、MCP、OpenAI、
  Anthropic 路由在禁用态仍存在，同时 callback、LLM helper、工具域、orchestrator、LangGraph 和 provider SDK
  前后保持 0，工具工厂不物化；
- `agent-tool-catalog`：通过主进程已有的 `moviepilot_tool_manager.list_tools()` 首次构建现有工具目录和 JSON Schema，
  要求动作前工具域未物化，动作后仅工具 base/catalog/factory/impl 物化；目录还必须无身份碰撞、Schema
  digest 完整，重复读取复用同一 snapshot/revision。结果记录工具数、Schema 摘要、plugin revision 与
  factory revision；
- 固定哨兵覆盖 `app.agent.orchestrator`、`app.agent.callback`、`app.agent.llm.helper`、工具
  `base/catalog/factory/impl`、`langgraph`、`langchain`、`langchain_core`、`openai`、`anthropic`、
  `google.genai`、`boto3`、`botocore`。其中 `langchain/langchain_core` 可能由完整 Schema 聚合形成既有
  基线，只记录数量与变化，不作为禁用态归零门禁；
- JSON 保留动作前后 Engine、PSS/USS、线程、完整 `sys.modules`、materialization observation、revision、
  网络累计值和浏览器卷指纹；动作前后容器网络收发必须为 0，Markdown 另汇总 Agent 场景与各定时点的
  模块哨兵峰值；
- 启用态 Agent 生命周期不会在该无凭据场景中伪造。现有 `get_running_agent_manager()` 是严格只读、
  non-materializing 的运行态 getter，`begin_agent_shutdown()` 也只是关闭轴；二者都不是安全启用入口。
  启用态必须由正式 startup/service lifecycle 驱动，只有宿主形成明确不创建 provider/client、不会外联的
  公共初始化合同后，才适合加入同一测量门禁。

## 完整三组 A/B

```bash
../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-001 \
  run \
  --before-ref upstream/v3 \
  --after-ref HEAD \
  --points 1,5,10,30
```

执行顺序固定为：

```text
Before-1 → After-1 → After-2 → Before-2 → Before-3 → After-3
```

每个样本都从 SQLite 和浏览器 seed 克隆新的命名 volume。样本结束后立即移除容器和样本卷；
完整 `run` 结束后还会移除 campaign seed 与 internal network。派生镜像和本地结果保留，便于复核。

## 输出

```text
<output>/<campaign>/
├── build.json
├── seed.json
├── results.json
├── report.md
└── samples/
    ├── before-1/
    │   ├── result.json
    │   ├── container.log
    │   └── modules/
    └── ...
```

- `results.json`：Engine stats、进程 PSS/USS/RSS/线程、网络累计值和模块前缀计数；
- `report.md`：三次原值与中位数 Before/After 汇总；
- `modules/`：由目标 MoviePilot Python 进程自身写出的完整 `sys.modules` 名称清单；
- `container.log`：已移除实验室凭据值和本地实例 UUID。

容器 working set 统一按 Docker Engine API 的
`memory_stats.usage - memory_stats.stats.inactive_file` 计算。进程 PSS 仅用于归因，不能替代容器指标。

## 清理

清理严格限定到 campaign 标签；不会执行 Docker prune：

```bash
../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-001 cleanup --images
```

本地 JSON、Markdown 和日志不会被 `cleanup` 删除。

## Python 3.14 free-threaded 镜像 A/B

`free_threaded_ab.py` 用于正式发布前在同一 Docker daemon、相同 CPU/内存限制下比较
`moviepilot-v3` 与 `moviepilot-v3t`。它不构建镜像，只接受两份
`repository@sha256:<digest>` 不可变引用，并要求镜像标签证明两者来自相同源码 revision 和版本。
未使用 `--pull` 时，digest 也可以是本机 Docker image ID，供依赖尚未发布前验收本地候选。

preflight 会验证 Python 3.14、GIL 状态、`thread_inherit_context`、MoviePilot-Rust 0.3 的
`jieba_cut`/中文转换入口，以及标准与 free-threaded 镜像互斥的原生依赖 profile。正式样本使用
固定 seed 和 fixture hash，按 `v3-1 → v3t-1 → v3t-2 → v3-2 → v3-3 → v3t-3`
交替执行真实 readiness 启动，并把应用识别热点明确分成 `V3 + Python`、`V3 + Rust`、
`V3t + Rust` 三组。两种镜像的纯 Python 并发与直接 Rust 并发是解释器/ABI 探针，不代表产品 Rust
开关的第三组结果；PostgreSQL 驱动选择使用不连接数据库的命令单独验证。

```bash
../.venv/bin/python scripts/perf/free_threaded_ab.py \
  --campaign v3-ft-001 \
  --standard-image 'jxxghp/moviepilot-v3@sha256:<64-hex-digest>' \
  --free-threaded-image 'jxxghp/moviepilot-v3t@sha256:<64-hex-digest>' \
  --pull
```

结果默认写入系统临时目录的 `moviepilot-free-threaded-ab/<campaign>/`：

- `results.json` 使用 `schema_version` 保存镜像身份、preflight、阈值、原始样本与中位数；
- `report.md` 提供维护者可读摘要；
- `samples/` 保存六个交替样本，便于排查离群值。

退出码 `0` 表示合同与性能阈值通过，`1` 表示样本有效但出现性能回退，`2` 表示 digest、ABI、
依赖、语义、驱动、启动或样本完整性不成立。该工具只用于隔离的本地长 A/B，不接真实凭据、用户数据库、
媒体目录或外网，也不加入常规 CI。

## TaskRegistry 跨线程提交 A/B

`task_registry_ab.py` 验证目标事件循环尚未分发 callback 时执行 shutdown，pending completion 与原始
coroutine 是否取得明确终态，同时采集跨线程提交最小协程的提交和完成耗时。分别在 Before/After revision
运行相同参数并保留两份 JSON，即可比较正确性与固定负载开销：

```bash
../.venv-test/bin/python scripts/perf/task_registry_ab.py \
  --iterations 2000 \
  --samples 7
```

该探针不访问数据库、配置或网络。吞吐结果用于识别可重复回退，不作为跨机器性能阈值；pending completion
取消且 coroutine 关闭属于正确性门禁。

### PostgreSQL 同步驱动三方案

`postgresql_driver_ab.py` 在同一 PostgreSQL 容器中比较标准 V3/psycopg2、标准
V3/psycopg3 binary 和 V3t/psycopg3 C。三个输入镜像必须来自相同源码 revision 和产品版本；
标准 V3/psycopg3 镜像是只增加该驱动的本地验证衍生镜像，不是发布制品。

```bash
../.venv/bin/python scripts/perf/postgresql_driver_ab.py \
  --campaign v3-ft-pg-001 \
  --postgres-container moviepilot-pg-ab \
  --dsn 'postgresql://moviepilot:<benchmark-password>@127.0.0.1:5432/moviepilot' \
  --standard-image 'moviepilot-v3@sha256:<64-hex-digest>' \
  --standard-psycopg3-image 'moviepilot-v3-pg3@sha256:<64-hex-digest>' \
  --free-threaded-image 'moviepilot-v3t@sha256:<64-hex-digest>'
```

脚本按三方案的六个全排列执行固定 SQL，默认把每个采样容器限制为 2 CPU/1 GiB，保存单连接查询、
16 线程查询、批量事务、长事务行锁并行、驱动/libpq/SOABI 和 GIL 状态。每个 campaign 使用独立
测试表并在成功或失败后清理；DSN 只传入隔离容器，不写入结果。性能数据用于解释驱动选择，不作为
跨机器发布阈值；驱动实现、GIL、查询结果、长事务并行和样本完整性属于硬门禁。
