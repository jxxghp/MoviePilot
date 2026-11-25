# 消息渠道验证

导航：[设计](design.md) · [计划](plan.md) · [任务](tasks.md)

## 验证合同

后端用例通过临时 `CONFIG_DIR`、临时数据库和模拟外部协议隔离真实账号及服务。单元测试覆盖成功、拒绝、撤权、并发版本冲突、重放及失败恢复；不使用真实渠道批量发送作为验收要求。

| 场景 | 主要回归文件 |
| --- | --- |
| 迁移与配置导入 | `tests/test_channel_resources_migration.py`、`tests/test_channel_actor_migration.py` |
| 准入与操作范围 | `tests/test_channel_admission.py`、`tests/test_channel_authorization.py` |
| 会话与个人隔离 | `tests/test_channel_actor_boundaries.py`、`tests/test_interaction_scope.py` |
| 资源与版本、完整删除 | `tests/test_channel_settings.py`、`tests/test_notification_channel_manage.py` |
| 通知受众、时间及分段 | `tests/test_notification_rules.py`、`tests/test_notification_routing_parity.py`、`tests/test_outbox.py` |
| 不可变业务归属 | `tests/test_subscription_ownership.py`、`tests/test_download_task_service.py` |
| 待确认下载归属及防重 | `tests/test_download_pending_confirmation.py` |
| 已认证入站提交与共享认领失败 | `tests/test_message_ingress.py` |
| 渠道身份核验与缓存对账 | `tests/test_channel_runtime_initializers.py` |
| 设备登记与迟到清理 | `tests/test_webpush_ownership.py`、`tests/test_webpush_subscription.py` |

## 提交检查

执行锁定环境的完整 `uv run --locked --no-sync python tests/run.py`，改动文件及全应用 pylint、mypy、架构依赖、复杂度、并发、异步阻塞、任务归属、service locator、Ruff/mypy ratchet 和三轮启动性能检查。复杂度只允许固化下降，不提升预算；冷导入预算保持 3000ms。

第一轮验收已形成上述本地提交，未推送或发布；后续 CI 结果须按实际提交核对。真实企业微信测试跳过；其他真实平台和生产部署尚需对应环境验收。数据库回退要求恢复完整备份，详见[升级计划](plan.md)。

## 通用核心与平台实现分离

职责来源、最终 owner、完整调用链、兼容与架构边界以[平台边界文档](platform-boundaries.md)为准。

## 第一轮平台职责分离验收（4e666434e / af33d57d）

| 检查 | 最终结果 |
| --- | --- |
| 完整后端用例 | 锁定环境按 `tests/run.py --shard N/8` 覆盖全部测试文件，22,889 通过、22 跳过，8 个分片均成功 |
| 覆盖率 | 同一最终应用源码的全部分片原始数据合并，`coverage_ratchet.py --report ...` 通过；全 app 排除 plugins 为 82.50%，1,229 文件；新平台文件满足 80% 门槛 |
| 身份/平台专项 | 最小测试渠道、已认证别名、跨连接/群组、历史冲突、固定升级等价、凭据保护及 Slack 分段恢复通过 |
| 架构与 API | 全量中依赖、API/OpenAPI、包根及 SDK/Compat 治理通过；文档/快照/平台约束另复验 72 项通过 |
| pylint / mypy | PR base 到最终树的全部改动 Python 文件及全 app pylint 10.00/10；全宿主 mypy 无错误 |
| 静态 ratchet | 类型例外、Ruff/mypy、复杂度、并发、async、任务归属及 service locator 全部通过 |
| 启动性能 | 三轮中位数：factory 1855.832ms、main 1818.798ms、lifecycle 1832.358ms；原 3000ms 预算和冷导入模块数均保持 |
| 前端配套 | 全量 5,083 通过、1 跳过；format、lint、typecheck、build 通过 |
| 真实浏览器 | Chrome 桌面 1440×1000、移动 390×844，真实规则组件按能力显示非数字线程、编辑和保存；无控制台错误，无横向溢出。使用合成能力/数据，不代表真实租户端到端验收 |
| 真实企业微信 | 按用户要求继续跳过；企业微信凭据策略仅执行模拟外部 API 的离线测试 |

架构依赖与运行合同快照仅记录已审阅的 owner/调用变化；新增 1 个 `MessageAttachmentRead` schema 导出，没有删除或改名公开导出。原 13 种下载模块 ABI 保留，调用由平台读取计划描述。复杂度基线只移除三项旧豁免：附件图像 18→9、文件 19→5、音频 19→8；不提高复杂度、覆盖率或启动门槛，覆盖率基线未改。

源码与测试均位于独立工作树，原 PR 源码、当前手测源指针和实例未切换。精确暂存树、diff、日志与浏览器截图随本地 `review/manifest.json` 留档，后续提交可按“协议/用例实现”“回归与架构约束”“文档”划分职责；本轮不自动创建提交或操作远端。

## 独立平台边界补齐

以[独立平台验收与身份职责](platform-boundaries-completion.md)为本轮补齐设计和调用链；旧 Web 复用测试不再作为扩展完成的证据。

本轮新增验收以独立 `review-completion/manifest.json` 的最终源码树和日志为准；第一轮测试数与截图只代表其对应提交，不能冒充当前源码验收。


## 本轮补齐验收结果

| 检查 | 结果与证据范围 |
| --- | --- |
| 后端全量 | `CONFIG_DIR=<临时目录> uv run --locked --no-sync python -m coverage run --parallel-mode tests/run.py --shard N/8`，8 个分片全部成功：22,915 通过、22 跳过 |
| 最终新增 Schema 回归 | 同一应用源码另执行 `coverage run --parallel-mode -m pytest -q tests/test_channel_platform_extensions.py tests/test_channel_platform_architecture.py`：71 通过；其中公开 OpenAPI/JSON 往返是全量后新增的 1 项测试，不将重复执行计入全量总数 |
| 覆盖率 | 保留全量和补充测试原始数据，以 Coverage API `combine(..., keep=True)` 合并，`coverage_ratchet.py --report <coverage.json>` 通过；全 app 排除 plugins：82.51%，1,229 文件；canonical 覆盖率基线未修改 |
| 独立平台链路 | 12 个内置平台共存 + 独立传输；实际认证、宿主分派、申请、管理员 HTTP 审批、规则执行和拒绝；共享传输不同 provider 仍隔离 |
| 架构约束 | `channel_platforms.py`、`baseline.py --check-host` 和全量依赖测试通过；局部变量、枚举/导入别名和字典查表反例通过 |
| pylint / 类型 | PR base、HEAD 和本轮暂存变更的 Python 文件并集及全 app 均 10.00/10；新增 Schema 测试另复验 10.00/10；`mypy_policy.py`、`mypy_ratchet.py` 通过，0 类型错误 |
| 其他门禁 | `complexity.py`、`concurrency.py`、`async_blocking.py`、`task_ownership.py`、`service_locator.py`、`ruff_ratchet.py` 通过；原门槛保持 |
| 启动性能 | `performance.py --check --repeat 3` 通过；中位数 factory 1881.687ms、main 1825.886ms、lifecycle 1829.128ms；模块数与 3000ms 预算保持 |
| 前端本轮 | `yarn test:run`：5,084 通过、1 跳过；`yarn format:check`、`yarn lint`、`yarn typecheck` 通过；新增目录测试同时展示全部 12 个内置平台和独立平台，组件专项 12 项通过 |
| 前端构建/浏览器 | 本轮未改生产前端源码，复用 af33d57d 的构建及桌面/移动浏览器证据；本轮新增后端不宣称已做新的真实浏览器或租户验收 |
| 环境与交付 | 使用隔离测试配置，真实企业微信继续跳过；未推送、强推、合并、发布或切换手测实例。按用户要求将已验收改动提交本地，提交阶段不额外运行测试；最新 commit 和 tree 由审阅索引记录 |

完整日志、原始覆盖率数据位置与源码树哈希由本地 `review-completion/manifest.json` 索引；该 manifest 和 `current-source.json` 均指向实际前后端工作树。旧 PR 内容尚未同步，不能用远端链接代替本轮源码证据。最终应用源码与全量测试时一致；之后只增加 Schema 测试和更新文档，补充测试、覆盖率和静态检查已重跑。


## 扩展传输标识缓存修复

本次基于后端 `a2e36930a` 修复 `NotificationChannel._missing_`：外部合法标识只生成无缓存的枚举值对象，解析不写 `_value2member_map_` 或成员目录，不产生平台注册和授权。同类枚举保持按值相等及原名称哈希，内置成员仍是原单例；字符串不获得枚举身份。平台协议、连接 provider/namespace 和 Web/WebAgent 保留限制保持原设计。

- 新增批量回归先在旧实现复现：4,096 个合法未注册标识导致全局缓存增加 4,096 项；修复后直接解析及 Pydantic JSON 解析均不改变缓存、平台目录或授权记录。
- 404 项专项通过，覆盖同值比较、集合/字典、复制和 pickle/JSON 往返，及实际认证入站、申请、HTTP 审批、规则执行、连接隔离和保留身份限制。
- 改动文件及全应用 pylint 均 10.00/10，mypy 0 错误；Ruff、复杂度和渠道平台架构门禁通过，未改门槛或基线。
- 后端全量以隔离 `CONFIG_DIR` 执行 `uv run --locked --no-sync python tests/run.py --shard N/8`，8 个分片均成功：22,918 通过、22 跳过。前端生产及测试源码未变，本次未重跑前端；真实企业微信继续跳过。

本次精确源码与检查日志由本地 `review-enum-cache/manifest.json` 索引，`current-source.json` 和 `platform-boundaries-current.json` 的最新入口同时更新。此前全量覆盖率报告仍属于其原源码树，本次不将旧覆盖率数字标作新源码结果。运行实例与远端 PR 未随源码索引自动更新。


## 上游冲突同步

后端以保留现有提交的合并方式同步官方 `86635b5ae`，保留 `8a4f5c70b` 的未注册传输无缓存修复。前端 PR 无冲突，源码保持 `81b53931`。

冲突仅涉及架构文档和依赖快照的计数/摘要：按合并源码核对全部依赖，保留上游新增的 16 条边和本次渠道重构的全部依赖，最终为 1,229 模块、10,996 条边。直接 Adapter、direct egress 和 SCC containment 与同步前一致；没有扩大权限、复杂度、覆盖率或性能门槛。自动合并保留上游消息历史/兼容别名、分页校验、站点 User-Agent 和下载失败重试选项，同时保留本次操作用户归属与投递上下文。

上游将 `moviepilot-rust` 升级到 0.3.16，本轮创建匹配合并锁文件的独立测试环境并重新核验依赖，原共享环境和手测实例未修改。架构依赖专项 109 项、消息/授权/下载整合专项 323 项通过，依赖锁定核对与漏洞审计通过；类型、静态 ratchet、改动文件和全应用 pylint 及启动预算检查通过。最终 8 个分片均通过（包含第 4/6 分片完整复验）：23,040 通过、22 跳过。覆盖率门禁通过，全 app 排除 plugins 为 82.56%，1,229 文件；未改覆盖率基线。启动三轮中位数 factory 1955.181ms、main 1899.722ms、lifecycle 1853.792ms，原 3000ms 预算保持。精确源码树和日志以本地 `review-conflicts-86635b5ae/manifest.json` 为准。源码索引区分当前本地修订与远端 PR 修订，普通推送后另行核对 PR head 和冲突状态。


本轮全量还复现了两个既有的时序问题。媒体分类性能测试首轮在并发覆盖率采集中超出 5ms P95，源码及预算未改，同覆盖率单例和完整分片复验通过。终端等待测试曾将分块返回的正文误判为缺少末尾换行，现复用原有真实字节观测握手，保留完整输出、进程状态、游标与超时断言；修正后的完整分片通过。首轮失败和最终复验日志分别留档，不以调整基线或放宽门槛解决。

## SDK 接入收口与兼容治理验收

实现及回归提交 `42d905f03` 的完整树为 `cfa4fa533a29ed049dca8bb5e3b3b316996a078e`，其中 app 树为 `e853fc9169641d58ba94ae841e7a13a584394e78`。本节之后的提交仅补充文档，最终本地修订与远端 PR 修订分别记录于 `current-source.json`、`platform-boundaries-current.json` 和 `review-sdk/manifest.json`。前端文档提交为 `148c6e7d`，生产和测试源码仍与 `81b53931` 相同。

| 检查 | 最终结果 |
| --- | --- |
| 完整后端 | `COVERAGE_FILE=<隔离文件> uv run --locked --no-sync python -m coverage run --parallel-mode tests/run.py --shard N/8`；8 个分片全部退出 0，23,116 通过、22 跳过 |
| 覆盖率 | 保留各分片原始数据，合并后执行 `coverage_ratchet.py --report <coverage.json>` 通过；全 app 排除 plugins 的行覆盖率 82.57%，1,231 文件；两个新增 SDK 门面均为 100%，原覆盖率基线未改 |
| SDK 与入站 | 独立平台与全部 12 个内置平台共存；公共类型契约、版本/owner 校验、认证、申请、HTTP 审批、规则、拒绝和撤权全链路通过；未就绪、身份缺失、停止、执行器拒绝/异常及接收器重装均无隐式回环 |
| 防重与兼容 | 企业微信私聊规则文本在提交成功或失败时都仅提交一次；保留旧插件导入、显式 HTTP helper、公开方法和固定离线升级；旧解析 Hook 不能注入私有证明或 Web/WebAgent 身份 |
| 架构 | `test_architecture_dependencies.py` 109 项先通过，再核对快照；全量和 `channel_platforms.py` 通过。反例覆盖模块、局部与带类型变量别名，以及公共 SDK 整体导入可信模块 |
| 快照审阅 | 仅新增两个 SDK 模块、净增 19 条依赖和 13 个规范类型/版本导出，增加已保留旧名的精确映射；现有导出没有删除。两个新 owner 分派调用纳入运行快照；SCC、直接 Adapter、网络出口和越层边均未变 |
| pylint 与类型 | PR base、暂存及本轮文件并集共 243 个 Python 文件和全 app pylint 均 10.00/10；全宿主 mypy 与 mypy ratchet 0 错误 |
| 其余适用门禁 | 类型例外、复杂度、并发、异步阻塞、任务归属、service locator、Ruff/mypy ratchet 通过；未增加豁免或提高基线 |
| 启动 | `performance.py --check --repeat 3` 通过，中位数 factory 1834.428ms、main 1890.701ms、lifecycle 1769.386ms；3000ms 预算保持 |
| 前端文档 | 四个改动 Markdown 的 Prettier、相对链接与 diff 检查通过；未重跑前端产品测试、构建或浏览器，没有宣称真实租户联调 |

首轮全量发现旧测试仍替换 `message_parser`，新入口已改用固定宿主 `run_module` 分派；相关群组归属用例已接到当前分派并保留真实解析、授权及原回复断言。消息上下文单测显式装配就绪状态，停用 ClawBot 断言增加 `message_ingress=None`。新增防重夹具的缺失必填字段也已修正。失败记录保留在 `review-sdk/full-suite.log` 和初轮专项日志；最终结果来自随后完整的 8 个隔离分片，未删除安全断言或放宽校验。

真实企业微信按用户要求继续跳过，外部协议均使用隔离配置与替身。本轮仅完成本地提交；未推送、强推、合并、发布或切换手测实例。完整内置接入清单、调用链及兼容退出条件见[SDK 台账](sdk-boundaries.md)，可安装插件渠道的信任模型仍为后续独立范围。

## SDK 收口后的上游冲突同步（bf097becf）

从 `4ab084dd9` 以合并方式同步官方 `bf097becf`，保留原提交历史。五处冲突涉及系统设置合同、下载提交、架构总览和两份事实快照；前端 `148c6e7d` 无冲突，源码未调整。

- 系统设置同时保留上游媒体账号凭据的内部敏感合同、旧通知的专用操作入口和新渠道资源的内部状态合同；三者均禁止通用设置写入。
- 下载同时保留上游 `ResourceSubmit` 事件取消、已解析哈希复用和本次不可变归属、任务标识及撤权复核。事件前后和下载器副作用前分别核验权限；事件回调不继承调用者归属或授权上下文。
- 架构依赖精确等于原源码与上游新增边的合并：新增 24 条、无删除，最终 1,232 模块和 11,039 条边。SCC、直接 Adapter、网络出口和越层边不变；SDK v1 导出、兼容映射及固定 owner 的动态分派保持完整。
- 109 项架构依赖先通过，随后 44 项架构契约及事件事实、208 项下载/设置/SDK/独立平台/媒体播放专项通过。新增回归覆盖事件内撤权、上下文隔离及三类设置写入边界。
- 最终锁定环境以 `coverage run --parallel-mode tests/run.py --shard N/8` 执行全部测试，8 个分片均退出 0：**23,137 通过、22 跳过**。合并覆盖率数据后门禁通过，行覆盖率 **82.54%**，原覆盖率基线未改。
- 280 个改动范围 Python 文件及全 app pylint 均为 10.00/10；mypy 0 错误，类型例外、复杂度、并发、异步阻塞、任务归属、service locator 和 Ruff/mypy ratchet 通过。上游 Ruff 清理低水位保留，未增加诊断预算。
- 启动三轮中位数 factory 1863.854ms、main 1897.805ms、lifecycle 1826.080ms，原 3000ms 预算保持。依赖声明与锁文件没有变化，已核验原隔离环境与锁文件一致。

精确合并修订、源码树、检查日志与推送结果由最新源码索引及 `review-conflicts-bf097becf/manifest.json` 记录。真实企业微信继续跳过；本次不合并官方 PR、不发布，也不切换手测实例。

## CI 第六分片的消息历史隔离修复

运行 `45591708561` 的第 6/6 分片中，两条消息分页/清理用例读到了前序授权用例遗留的无标题收件记录。本地按“授权文件 → 消息查询文件”顺序稳定复现相同的两条失败；逐用例退出观测定位到管理员旧交互撤权回归的获准文本分支。该用例确实需要写入消息历史，原夹具却只登记了用户和回执 Outbox 的清理水位。

授权夹具现同时登记消息历史水位，按原测试载具机制清理新增行；保留真实写入，并增加“获准文本新增一条、按钮不新增、撤权后不再写入”的断言。未修改生产查询、未过滤原失败断言，也未调整分片、门槛或基线。

- 两文件同序复验：287 项通过；六种旧交互在夹具完全退出后均恢复原消息主键集合。
- 与 CI 相同的 `coverage run --parallel-mode tests/run.py --exclude-architecture-gate --shard 6/6`：4,075 项通过，原失败已消除。
- PR 改动范围 pylint 通过。生产 app 树仍为 `12e007edf0a159b86db9949514580523e7874bc8`，依赖和共享测试引导未改，其余分片与生产门禁复用前述源码的有效证据；远端检查按修复后精确提交重新核对。

失败日志、定位观测、复验与后续提交/CI 状态见最新源码索引和 `review-ci-45591708561/manifest.json`。前端源码、真实企业微信验收及手测实例均未调整。

### 覆盖率报告的后续回归

修复后的运行 `45606387541` 六个覆盖率分片全部通过，最终报告单独发现入站 92.94% 和插件投影 84.33% 低于各自原低水位。该运行实际验证的是 GitHub 合并修订 `7c3a77b38`，包含上游 `583d678e7` 的联邦产物摘要改动；源码与行号不能直接用本地分支的投影文件替代。

补充绑定入口异常只提交一次且不泄漏异常正文、隔离入口的跨连接防重/容量/过期，以及插件模块/API hook 异常不阻断健康插件的有效回归。当前分支相关 111 项通过；在独立工作树上保留该 CI 合并修订的全部上游测试、仅应用候选测试补丁后，112 项通过。下载的六份 CI 原始覆盖率数据在准确合并源码上重放，再合并同一源码的补充测试数据，入站达 97.06%、插件投影达 85.38%，覆盖率门禁通过；此合并报告仅作诊断，远端正式结果以修复提交的新 CI 为准。

本轮继续只改测试和验证说明，没有改生产实现、覆盖率基线、分片或阈值。其余生产门禁复用相同源码证据，新增测试按准确 PR 范围复验 pylint；原始 CI 数据、准确合并修订与重放结果一并保留在验收索引中。
