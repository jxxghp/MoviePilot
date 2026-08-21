# v3 重新变基计划（2026-08-14）

## 背景与前提

官方 `jxxghp/MoviePilot` 的 `v3` 分支自行完成了大规模后端分层重构（`7b3444c36` 716 文件 +10391/−7722、`369d7d644` 584 文件），**`app/core/` 与 `app/utils/` 已整体删除**，并对插件生态做了破坏性迭代、自带 `app/runtime/compat/` 全局 legacy import 兼容层。我们的 PR #6307（打断 import 循环）已被 **CLOSED 未合并**，正是被这次重构取代。

**新分层**：`app/foundation/`（纯工具）、`app/domain/`（领域模型）、`app/application/`（应用服务 28 个）、`app/adapters/`（外部适配）、`app/runtime/`（运行时基础设施 + `extensions/` + `compat/`）、`app/sdk/`（插件 SDK）。

### 三条纪律

1. **不再写任何自有垫片 / re-export 桥 / PEP 562 shim**——官方 compat 层已覆盖插件侧兼容。
2. **纯结构性改动一律作废**（迁出 core / 拆包 / 加 Protocol / 打断 import 循环）。只抢救行为语义、缺陷修复、官方没有的独有功能。
3. **不得逐提交 cherry-pick**。这 17 个提交历史被 rebase 打散过，互相不自洽：
   - `fbdbd882d` import 了 `06f1efc14` 才创建的 `app.core.module.loader`
   - `06f1efc14` import 了 `19ee03301` 才创建的 `app.startup.service_registry`
   - `c2c45d026` import 了 `96a35777c` 才创建的 `app.helper.plugin_manager`
   - `c9bfe310e` 有三处独立 import 断裂（`MediaSourceQuery` NameError、`app.chain.music` 不存在、`compare_secret` 未定义）
   - `feb80c77f` 标题写的"渠道隔离"生产码其实在兄弟提交 `c2c45d026` 里，本提交只有测试

   **事实来源只能是「合并基 `d61189129` → 终态 `fe311b966` 的整体 diff」**（333 文件 +29594/−8592），commit message 只当索引。抢救内容一律取分支 tip `v3-python` 的版本，不取原提交。

### 坐标

> **2026-08-15 追新**：基线由 `e28de9cfe` 前进到 **`5a1808592`**（6 个新提交）。其中 `8a11214a4 refactor(db) (#6320)` 252 文件 +11413，**使 B1、B2 两个工单作废**（官方自己做完了 SQLA 2.0 与 journal_mode 修复），并把 `app/db/` 重组为 `base/decorators/diagnostics/engine/session/models/oper`。**15 个 P0 缺陷经逐条复核全部仍在**，一个都没被顺带修掉。⚠️ 本文档中 `#6320` 之前采集的行号已大面积失效，落点请按**符号名**定位，不要信行号。

### 官方架构门禁（移植必须遵守）

官方新增了 `docs/rules/`（12 份规则文档）与两个门禁测试 `tests/test_architecture_dependencies.py`、`tests/test_chain_layering.py`——**构建完整 Python 模块图并拒绝任何 import 环**，即使环穿过未搬迁的既有包也拒绝。

`docs/rules/05-architecture.md` 明文规定：`app/core`、`app/helper`、`app/utils` 是**虚拟兼容包，禁止在其下重建物理源码**；`app/sdk/` 与兼容层是边界而非规范实现模块的依赖。官方还在 `a2117bafc` 里新增了 `app/sdk/_legacy/{subscribe,transfer,user}.py` 进一步兜住插件旧行为——**再次印证：兼容由官方承担，我们一个垫片都不写**。

| 项 | 值 |
|---|---|
| 新基线 | `upstream/v3` == **`5a1808592`**（2026-08-15 追新） |
| 工作区 | `.claude/worktrees/v3-next`（分支 `v3-python-next`） |
| 旧终态 | `fe311b966`（工作区 `.claude/worktrees/v3-rebase`） |
| 合并基 | `d61189129` |
| **基线测试** | **4026 passed / 3 skipped / 0 failed**（145s）。移植期任何失败都是我们引入的 |
| alembic 单 head | `f4c8d2a7b1e6`（46 个迁移），新迁移接这里 |
| 测试解释器 | `/home/vscode/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12`（CI 用 3.12） |
| 环境坑 | 官方 `requirements.txt` 漏声明 `mutagen`，不装则 155 个 collection error；`venv/` 缺 moviepilot-rust/pytest-asyncio，用它跑会假失败 |

---

## 分诊总表

| 原提交 | 规模 | 判定 | 结论 |
|---|---|---|---|
| `fbdbd882d` refactor(core) 迁出 Redis/Thread/ModuleHelper | 32f +1542 | 捞行为 | 结构全废（Redis/Thread 迁移前后**字节完全一致**）；仅捞限流原语 |
| `56af247b9` refactor(event) 拆包 + 锁内快照 | 6f +325 | 捞行为 | 拆包废；**并发修复必捞（P0）** |
| `06f1efc14` refactor(module) 拆包 + Protocol | 11f +1507 | 捞行为 | 拆包/Protocol 废；插件模块注册待决策 |
| `c2c45d026` refactor(dispatch) 统一分发内核 | 49f +5147 | 捞行为 | 内核+门面全废（44 文件）；捞 3 项 + 渠道隔离生产码 |
| `feb80c77f` refactor(message) 拆分 + 渠道隔离 | 17f +2011 | 捞行为 | 拆分纯搬移（**1537 行逐字节相同**）；捞 1 安全修复 + 隔离测试族 |
| `96a35777c` refactor(plugin) SPI 扩展点 | 27f +4038 | 捞行为 | 迁移/拆包废；捞声明式 SPI（待决策） |
| `78160c536` refactor(db) SQLA 2.0 + 插件自管理库 | 28f +1099 | **整体移植** | 官方全缺，价值最高 |
| `eb2a9a215` feat(auth) 可插拔认证框架 | 60f +7393 | 捞行为 | 桥接层废；核心引擎整体移植，拆 13 单 |
| `0cd6b34e6` refactor(transfer) 拆三服务 | 17f +2852 | **整体作废** | 6 项"行为增量"**全是上游自己的代码**，我们只是搬进了自己抽的类 |
| `c9bfe310e` refactor(api) 端点下沉 app/service | 49f +2403 | 捞行为 | **服务层整体作废**；捞 2 项 |
| `19ee03301` refactor(di) ServiceRegistry | 15f +499 | 捞行为 | DI 骨架废；捞 2 个缺陷修复 |
| `5d37dd82d` fix(security) P0/P1/P2 | 14f +216 | 捞行为 | 4 项仍存在；release-zip 穿越官方已自修 |
| `6d43ec5fb` fix(security) 图片穿越+凭据泄露 | 5f +218 | **整体移植** | 两项**全部仍存在**，含一个 CRITICAL |
| `a78645156` fix(reliability) think DoS+死锁+Alist | 6f +237 | 捞行为 | think 死循环官方已自修；死锁与 Alist 仍在 |
| `5dcaad830` / `bdf6013d1` / `fe311b966` | — | 丢弃 | 注释文案 + 上一轮变基产物 |

---

## 工单清单

优先级：**P0 安全/可用性缺陷** → **P1 独有能力** → **P2 待决策**。

### P0 — 独立可落地的缺陷修复（先做）

| # | 标题 | 落点 | 难度 | 来源 |
|---|---|---|---|---|
| **A1** | 事件总线订阅者并发安全：广播/链式同步/链式异步三处分发改**锁内快照订阅者**；广播消费者循环补 `except Exception` 兜底（当前一次 `RuntimeError` = 广播总线**永久停摆**） | `app/runtime/events.py` L413/L442/L471/L681；`tests/test_event_broadcast_concurrency.py` | 低 | `56af247b9` |
| **A2** | 通知渠道隔离 fail-closed：token 规范化（`"user, admin"`/`"ADMIN"` 当前可绕过）、未知 token 改 fail-closed（当前 `else: send_orignal=True` **广播全渠道**）、空 targets 守卫；vocechat/qqbot/feishu/webpush 四渠道补守卫（telegram/wechat/slack/discord/synologychat/wechatclawbot 已合规） | `app/chain/__init__.py:1772/1888`、`app/modules/{vocechat,qqbot,feishu,webpush}/__init__.py`；`tests/test_post_message_isolation_leak.py`、`test_channel_targets_isolation.py` | 中 | `c2c45d026`(码)+`feb80c77f`(测) |
| **A3** | Agent `extra_context_files` 路径穿越（CWE-22 → 任意文件经 LLM 上下文外泄）。当前 `_resolve_relative_path` 绝对路径直通、`..` 不拦 | `app/agent/runtime.py:750-753`；`tests/test_path_trust_boundary.py::TestRuntimeResolveRelativePath` | 低 | `feb80c77f` |
| **A4** | API_TOKEN/APIKEY 改**常量时间比较**（CWE-208）。最高价值点是 `__verify_key`（旧提交未覆盖，属新增面） | 新增 `compare_secret` 于 `app/application/security/access.py` + 改 `:315`；`app/api/endpoints/{openai.py:253,anthropic.py:53,subscribe.py:554}` | 低 | `c9bfe310e`+`19ee03301` |
| **A5** | CORS 凭证与通配源 `*` 互斥（`ALLOWED_HOSTS` 默认 `["*"]` + `allow_credentials=True`，**默认配置即命中**） | `app/factory.py:302-306` | 低 | `19ee03301` |
| **A6** | scheduler 协程任务 Future 收口（`.result()`，修「提前复位 `running` 标志致重入并发」+「协程异常静默丢失」）**+ 关停不阻塞主循环配套**（`await run_shutdown_step("定时器", lambda: asyncio.to_thread(stop_scheduler))`）。两半必须同 commit。**注意**：官方当前 `__start_coro` 是 fire-and-forget，旧的无界死锁路径已不复现；配套项按「防止 `Scheduler.stop()` 的 APScheduler `shutdown(wait=True)` 阻塞主循环」的一般性加固定性 | `app/scheduler.py:988-1007`、`app/startup/lifecycle.py:136`；两个测试文件 | 中 | `19ee03301`+`a78645156` |
| **A11** | **图片代理缓存键路径穿越 → 任意文件读/写（CRITICAL）**。域名白名单只查 host 不查 path，携带 `../../` 的合法域名图片 URL 直接成为缓存 key；**WRITE 侧完全开放**，可在缓存目录外写入/覆盖任意可写路径 | 源头 `app/application/security/url.py:829-852 sanitize_url_path` 过滤 `''/'.'/'..'` segment；纵深 `app/adapters/cache/backends.py:157-322` FileBackend/AsyncFileBackend 加 `relative_to` fail-closed 守卫；`tests/test_image_cache_path_traversal.py`(10 例) | 低 | `6d43ec5fb` |
| **A12** | **存储配置公开接口越权泄露凭据（HIGH）**。`_PUBLIC_SYSTEM_CONFIG_KEYS` 含 `SystemConfigKey.Storages` 且原样透传，`get_current_active_user_async` 只校验 `is_active` → **非超管普通用户可读云盘 OAuth token / alist 账密 / SMB 密码** | `app/api/endpoints/system.py:864-880 get_public_setting` 补脱敏（仅回传 type/name）；`tests/test_public_setting_storage_redaction.py` | 低 | `6d43ec5fb` |
| **A13** | **Alist 截断/取消下载当成功返回（HIGH，可致不可恢复数据丢失）**。取消时不删已写入的部分文件即 `return None`；异常兜底 `if local_path.exists(): return local_path` 把残缺文件当成功；transhandler 不校验大小即 move 入库，move 模式随后删源 | `app/modules/filemanager/storages/alist.py:620-711 Alist.download`；`tests/test_alist_download_partial.py` | 低（diff 可近乎原样复用） | `a78645156` |
| **A14** | 超管 TokenPayload 缓存 TTL 600s 过宽 + **未校验 `is_active`**（超管被禁用后 10 分钟内仍可持令牌通行） | TTL → `app/application/security/access.py:86`（600→60）；is_active → `app/application/security/auth.py:121-135 build_superuser_token_payload` | 低 | `5d37dd82d` |
| **A15** | 插件「分身」`clone_plugin` 的 `suffix` **无任何长度/字符集校验**，直接拼进目录名并写入生成的 `__init__.py` 类名 → 目录逃逸 + 生成代码注入（需超管认证，属纵深防御缺口） | `app/runtime/extensions/plugin_manager.py:2001-2040`、`app/api/endpoints/plugin.py:971-993`；补 `1<=len<=20 and isascii and isalnum` 白名单 + `is_relative_to(plugins_root)` 校验 | 低 | `5d37dd82d` |
| **A7** | `/access-token` 补 (ip:username) 滑窗限流（CWE-307，官方**完全无限流**）；顺带给 `WindowRateLimiter` 加原子 `try_record` 消 TOCTOU + 新增 `KeyedWindowRateLimiter` | `app/runtime/rate.py`、新增 `app/application/security/rate_limit.py`、`app/api/endpoints/login.py` | 低 | `eb2a9a215`+`fbdbd882d` |
| **A8** | `torrent_files()` 归一化 `DownloaderFile`，堵 **rTorrent 种子永不清理的静默失效**（`List[Dict]` 上 `file.name` 抛 AttributeError 被宽 except 吞掉 → `_check_torrent_deletable` 对 rTorrent 恒 False） | `app/schemas/transfer.py`、qb/tr/rtorrent 三个 `__init__.py`、`app/chain/__init__.py:1736`、`app/chain/transfer.py:4762` | 中 | `c2c45d026` |
| **A9** | rTorrent 补齐 `get_torrent_trackers`（qb/tr 都有，rtorrent 缺，广播时被 `hasattr` 过滤跳过） | `app/modules/rtorrent/{rtorrent.py,__init__.py}` | 低 | `c2c45d026` |
| **A10** | `_ModuleBase.get_priority()` 默认返回 `DEFAULT_MODULE_PRIORITY=9999`（当前 37/37 内建模块都声明了故不触发，属预防性；一旦有插件注册模块立刻变真崩） | `app/modules/__init__.py:83-87` | 低 | `06f1efc14` |

### P1 — 独有能力（官方完全没有）

| # | 标题 | 落点 | 难度 | 依赖 |
|---|---|---|---|---|
| ~~B1~~ | ~~SQLite `journal_mode` 改 connect 事件~~ | **已上游化** —— 由**我方 PR #6320**（作者 Aqr-K，2026-08-14 merged）带入官方，且比 fork 原版更彻底：异步侧**完全移除** `asyncio.run()` 设 WAL，引擎改惰性创建，`app/db/engine.py` 有详细论证 | — | — |
| ~~B2~~ | ~~全量 ORM 模型升级 SQLAlchemy 2.0~~ | **已上游化** —— 同属**我方 PR #6320**：`mapped_column` 命中 22 文件，legacy `Column(` 零命中。**不是被官方取代，是我们推上去的** | — | — |
| **B3** | **插件自管理数据库框架**（SQLite 独立 Engine / PG `schema_translate_map` 路由 / `create_tables` / `drop_plugin` / `dispose_all`）。`#6320` 后复核：`schema_translate_map`/`PluginDatabase`/`build_plugin_base` **仍全部零命中**，依旧是我们独有 | **落点变更**：`app/db/` 已重组为 `base/decorators/diagnostics/engine/session/models/oper`，新增文件须按此结构选址（引擎相关靠 `engine.py`、会话相关靠 `session.py`）；`app/sdk/database.py`；`tests/test_plugin_db_manager.py` | 中 | 无（B2 已由官方完成，前置解除） |
| **B4** | `provides_models()` / `get_plugin_db()` 钩子 + PluginManager 建表/删库生命周期挂载 | `app/plugins/__init__.py`、`app/runtime/extensions/plugin_manager.py` | 低 | B3 |
| **B5** | per-plugin Alembic 迁移链 + `provides_migration_location()` | 新增 `app/db/plugin_migration.py` | 中 | B3、B4 |
| **B6** | 认证标识符集中校验（`IDENTIFIER_RE` 禁下划线/路径分隔符，杜绝跨提供方撞名与路径注入） | `app/foundation/identity.py` | 低 | 无 |
| **B7** | 通用挑战短时存储 `ChallengeStore`（TTL + 取即销毁 + 注入 now） | 新增 `app/application/security/challenge_store.py` | 低 | 无 |
| **B8** | db-free 认证契约核心：`IAuthStep`/`AuthStepResult`/`AuthContext`/组合策略代数（AllOf/AnyOf/NOf）/`Challenge` ADT/类型化 `AuthResult`（删死代码 `ResolvedIdentity`） | 新增 `app/application/security/authflow/{flow,challenge,outcome,types}.py` | 中 | B6 |
| **B9** | owner-scoped 注册表 + 步骤注册表 + 流程规格注册表（含 `_is_empty_true` **空真绕过拒绝**：`AllOf([])`/`NOf(0)` 会 vacuous 绕过 MFA） | `authflow/{registry,steps,flow_registry}.py` | 中 | B8 |
| **B10** | 流程引擎 `AuthFlow.advance` + **CAS `FlowStore`**（owner 分流护栏：仅受信内建步可直落 `user_id`；`max_attempts`；防御式包裹） | `authflow/engine.py` | 中 | B8、B9 |
| **B11** | `ExternalIdentity` 模型 + 守护式 `resolve_or_create`（防外部用户名接管本地管理员、防绕过封禁、并发首登回退）+ **新建表**迁移（非 rename，`ssoidentity` 在官方从未存在） | `app/db/models/externalidentity.py`、`app/application/security/provisioning.py`、新 alembic 接 `f4c8d2a7b1e6` | 中 | B6 |
| **B12** | 步骤适配器（Factor/Password/CredentialProvider）+ 内建 `OtpFactor`（须保 `is_otp=False→免MFA`、`is_otp=True且无码→401+X-MFA-Required` 两条不变） | `authflow/steps_impl.py`、`authflow/builtin_factors.py` | 中 | B8、B10、B11 |
| **B13** | `AuxiliaryCredentialStep`（`AUXILIARY_AUTH_ENABLE` 等价性：旧「密码失败→试辅助」vs 新 `AnyOf` 回落，**需专门等价性测试**） | `authflow/steps_impl.py` | 中 | B12 |
| **B14** | `FlowService` 两阶段编排（凭证→条件 MFA）+ `redact_reason` 白名单脱敏 + `_validate_requirement` 运行期降级 | `authflow/{service,assembler}.py` | 中 | B10、B12 |
| **B15** | 认证观测事件（`AuthSucceeded`/`AuthFailed`/`AuthLogout`/`MfaChallengeRequired`） | `app/schemas/{types,event}.py`、`authflow/observation.py` | 低 | B14 |
| **B16** | `/auth/flow/{begin,advance}` 端点 + **类型化 `response_model`**（不得用裸 `dict`，官方全线 `ResponseAPIRouter`） | `app/api/endpoints/auth.py`、`app/schemas/auth.py` | 高 | A7、B14、B15 |
| **B17** | `/access-token` 切引擎驱动，**保留官方 401 `Response[MfaChallenge]` 契约**（仅扩 `factors_available`），**不移植** `Token.status`/`Token.factors_available`（往成功模型塞失败态，与官方分离契约冲突） | `app/api/endpoints/login.py` | 高 | B16 |
| **B18** | 认证安全回归套件（空真绕过/冒充内建 id/CAS 并发/脱敏/限流/护栏拒绝） | `tests/test_auth_security_regression.py`(664 行) | 中 | B16、B17 |
| **B19** | 认证/限流文案补译 5 条（源串目前官方全无，先补等于死键） | `app/locales/{en-US,zh-TW}.json` | 低 | B17 |
| **B20** | `recognize_media(raise_exception=)` 严格模式（默认路径逐字节不变）。**风险**：该参数会原样透传给后端，签名严格（无 `**kwargs`）的后端会 `TypeError`，移植前须确认所有识别后端接受 `**kwargs` | `app/chain/__init__.py:608` | 低 | 无 |

### P2 — 待决策（产品决策，非移植决策）

| # | 标题 | 说明 |
|---|---|---|
| **C1** | 插件模块二级注册（`register_module(owner=)`/`unregister_modules`/契约校验/字符串子类型） | 官方**完全没有**「插件可注册运行态模块」这个概念。整块移植 = fork 再次背上与上游发散的核心 API。**必须与 C2 同进同退** |
| **C2** | 声明式 SPI `provides_*()`（取代 `get_module()` monkey-patch 胁持） | 硬依赖 C1。官方 `get_auth_providers()` 是**纯展示 dict**，与我们的后端契约 SPI 语义完全不同，属叠加非替换 |
| **C3** | 存储器开放注册 `storage_registry` + 渠道能力外部注册轨 | 依赖 C2，否则注册表无调用方即死码。注意渠道能力只加**外部注册轨**，不动内建静态表 |
| **C4** | discover/recommend 数据源去重 + 畸形项守卫 | 仅在存在第二条来源车道（C2）时才有意义，单事件车道下是空转 |
| **C5** | SSO 重定向车道（`RedirectStep` + `/auth/flow/callback`） | **与官方 ticket/exchange 模型正面冲突**。官方：插件前端自行认证→`create_plugin_auth_ticket`→`/auth/exchange` 换 Token；我们：拉回服务端流程引擎。且我们的流程末端仍退回 ticket，两套模型半嵌套。**架构路线定案前不动** |
| **C6** | uvicorn `proxy_headers` / `FORWARDED_ALLOW_IPS` | 官方全仓 `request.client.host` 零消费点，当前无攻击面。等 A7 认证限流落地后再做 |
| **C7** | `atomic_session` 托管事务 | 全分支零调用方，属未兑现能力，等有真实调用方再落 |

---

## 明确整体作废（不再移植）

- **`app/core/dispatch.py` 统一分发内核 + `app/managers/` 五门面 + `base.py`（44 个文件）**——纯去重零行为收益；且门面改写会**静默摘除 v2 插件经 `get_module` 劫持 `media_statistic`/`downloader_info`/`mediaserver_*` 的能力**（是回归）
- ChainBase 六 Mixin 拆分 + 13 个 keyword-only DI 参数
- **`app/chain/media_interaction.py` 拆分（1567 行）**——与原 `message.py` 内容**逐字节相同**，纯搬移；官方坚持单文件且该区已被身份重构大改，重做只制造永久冲突
- **`app/service/*` 全部 11 个模块 + 16 个端点薄壳化**——提交本身三处 import 断裂不可用；官方 `app/application/` 是 **helper 搬家而非编排层**（端点行数零变化：transfer 618→618、system 1496→1496、search 809→809）；且 `app/service/{history,storage}.py` 与官方 `app/application/{history,storage}.py` **同名不同物**
- **`TransferService`/`ScrapeBatchCoordinator`/`TransferResultProcessor` 三服务（2105 行）**——6 项"行为增量"经核验**全是上游自己的代码**（`get_job_id`/`pending_total`/`clear_transfer_failures`/`__is_overwrite_declined`/`file_contexts`/`overwrite_skipped` 在父提交中已全部存在）
- `history.py`/`llm.py` 的 `response_model` 泛型退化——**方向与官方相反**，官方有 `tests/test_explicit_response_models.py` 守门
- `app/startup/service_registry.py` DI 组合根——与官方 `app/runtime/extensions/service_registry.py` **命名撞车但语义无关**（官方那个是「已配置服务查找表」）
- `app/core/auth_bridge.py`——官方 `app/application/security/auth.py:20-166` 已逐字提供同等实现
- `app/core/auth_level.py` + `security.py` 的 `get_auth_level()` seam——官方新分层已无 core→helper 循环
- `app/service/mfa.py`、`passkey_login.py::PasskeyChallengeStore`（官方版更强，带 `purpose`+`user_id` 绑定）、`app/schemas/mfa.py` 的 8 个 Request 模型（官方刻意内联）
- `IDownloader`/`IMediaServer`/`INotification`/`IMediaRecognize` 四个 Protocol（~250 行）——运行期不被契约校验使用（校验走 `getattr`+`inspect.signature`），纯装饰
- `systemconfig_oper` falsy 守卫——**官方已自行修复且更彻底**（无条件 update，我们的是子集）
- `tests/test_github_sso_plugin.py`——引用 `app.plugins.githubsso`，该插件在本仓从未存在，恒 ImportError
- 全部结构断言型测试（`test_plugin_manager_relocation.py`、`test_*_facade.py`、`test_dispatch_kernel_unify.py`、9 个 `test_*_service.py`、5 个 `test_s6_di_*.py`、6 个 transfer 拆分测试——其中 `test_transfer_handle_carve.py:49` 甚至用 `inspect.getsource` 断言方法体字符串）
- `REBASE_NOTES_20260813.md`、`TRANSFER_UPSTREAM_DIFFS.txt`——变基过程文档，绝不入库

---

## 附：官方自身的问题（与我们无关，可单独提 issue/PR）

1. `app/foundation/reflection.py` 的 `ModuleHelper` 三处（L48/L82/L100）把 `logger.debug('加载模块 X 失败')` 换成裸 `except Exception: continue`，**模块/插件加载失败完全静默无日志**。
2. `requirements.txt` 漏声明 `mutagen`。
3. `/access-token` 无任何频次限制（CWE-307）。
4. `WindowRateLimiter.can_call`/`record_call` 分离两次取锁，存在 TOCTOU 突发绕过。
5. `ALLOWED_HOSTS` 默认 `["*"]` 且 `allow_credentials=True`，默认配置即 CORS 凭证泄露。
6. 辅助认证建号路径（`app/chain/user.py:234-256`）无绑定表，存在「外部用户名撞本地管理员用户名」的接管面。
7. 事件总线读侧不取锁而写侧原地 mutate 同一 dict（详见 A1），且广播消费者线程数下限为 1，一次异常即永久停摆。
8. 图片代理缓存键路径穿越（A11，CRITICAL）、存储凭据越权泄露（A12，HIGH）、Alist 截断下载数据丢失（A13，HIGH）、超管令牌缓存无 `is_active` 校验（A14）、`clone_plugin` suffix 无校验（A15）—— 这 5 项均为官方基线现存缺陷。

## 官方已自行修复的项（无需移植）

- **插件 Release zip 安装目录穿越** —— `app/adapters/external/market.py:2029-2033` 已用等价的 `Path.relative_to` 边界校验取代我们的 `SystemUtils.is_within`。
- **`_ThinkTagStripper.process` 死循环 DoS** —— 类已搬到 `app/agent/orchestrator.py:250-315`，现状已含对称 `break`。
- **`systemconfig_oper` 假值落库** —— 官方改为无条件 `update`，比我们的守卫更彻底。
- **关停死锁的原始触发链** —— 官方 `__start_coro` 现为 fire-and-forget，旧的无界互等路径已不复现（但 A6 的一般性加固仍应做）。

**结论：`SystemUtils.is_within` 无需移植**——官方仓库已普遍使用 `Path.is_relative_to()`/`relative_to()` 惯用法，A11/A15 直接复用即可，不要为此引入新工具函数。
