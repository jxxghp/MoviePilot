# 平台边界补齐与独立渠道验收

导航：[职责设计](platform-boundaries.md) · [计划](plan.md) · [任务](tasks.md) · [最新验收索引](validation.md)

本轮基于后端 `4e666434e`、前端 `af33d57d` 继续补齐。旧用例复用 Web 且只安装测试目录，不能证明新增平台与内置模块共存；旧一轮记录不得替代本轮证据。

## 四类身份与入口信任

| 概念 | 事实来源与职责 | 不承担的职责 |
| --- | --- | --- |
| provider | 既有模块清单 selector 的协议 owner，负责主体、地址、凭据与平台配置 | 不授予角色、审批状态或业务权限 |
| 传输类型 | `NotificationChannel` 保留内置名字和值；`platform:<标识>` 表达独立扩展类型，供消息和模块分派 | 类型可解析不等于已注册、已启用或已认证；不能把 Web/WebAgent 当成外部平台 |
| 连接身份 | 宿主创建的连接 ID 与不可变 identity_namespace，保存配置、授权、会话及投递归属 | 显示名称/source 可修改，不能作为入口认证凭据 |
| 入口信任来源 | SDK 先验证自身连接协议；宿主按连接找到 provider 和模块 ID，固定宿主 parser，创建私有 AdmissionProof | 报文中的 provider/channel/connection_id 或能力描述不能生成信任 |

扩展传输标识使用小写命名空间格式，标识本体 1–55 字符，总长不超过现有授权表 64 字符，不新增数据库迁移。OpenAPI 同时描述内置值和扩展格式。内置值、插件消息和既有持久化编码保持兼容。解析扩展类型不会把它加入内置成员迭代清单，也不会自动登记平台。

不同 provider 可以共用传输类型。连接中的 provider 确定身份解析 owner；授权、申请、审批、账号关联、历史读取、任务复核、私聊和分段均使用已有连接上下文。只有无连接上下文的旧兼容函数才按传输类型查找；遇到多个候选时拒绝猜测。设置中连接名称唯一仍是宿主统一配置政策，不是平台身份或信任来源。

## 本轮职责变化与源码入口

| 调整前 | 调整后 | 当前源码与验收 |
| --- | --- | --- |
| 固定枚举不能表达独立测试平台；测试替换成单平台目录 | 保留内置值，使用命名空间扩展，增量注册并保留全部内置声明 | [传输编码](../../app/schemas/types.py)、[注册适配](../../app/runtime/extensions/module/adapter.py)；`test_independent_subtype_is_valid_only_for_an_explicit_notification_owner` |
| 只凭 channel 选择唯一协议实现 | 已知连接按 provider 精确选择，无上下文的歧义查询拒绝 | [平台端口](../../app/application/messaging/channel/platform.py)、[授权应用服务](../../app/application/security/authorization.py)；`test_shared_transport_uses_connection_provider_without_global_uniqueness` |
| 已认证 SDK 仍可能进入全局 parser 和插件优先级分派 | 用连接 owner 的 module_id 固定既有宿主调度，不新增调度框架 | [消息入口](../../app/chain/message.py)、[模块分派](../../app/runtime/extensions/module/dispatcher.py)；`test_independent_platform_authenticated_lifecycle_with_all_builtins` |
| 公共基类包含个人地址格式和平台原图分支；启动层按传输猜 owner | 宿主执行私聊政策，模块声明地址约束和旧分段事实；启动层按保存的连接定位模块 | [模块模板](../../app/modules/_base/channel.py)、[装配入口](../../app/startup/initializers/modules.py)；个人投递、Slack 轮换及管理入口回归 |
| 架构约束主要覆盖直接比较和协议字符串 | 在保护范围内传播局部选择器、枚举/导入别名及字典查表来源 | [架构门禁](../../scripts/architecture/channel_platforms.py)、[反例测试](../../tests/test_channel_platform_architecture.py) |

上述扩展证明集中在[独立渠道回归](../../tests/test_channel_platform_extensions.py)。Agent 既有格式提示和 LLM 回复能力选择仍可为未声明渲染能力返回通用文本；它们不决定成员授权，不作为本次独立平台审批/规则验收的替代。历史内置枚举和值的兼容入口保留，固定离线升级不读取动态目录。

## 完整调用链验收

`BoundaryFixtureModule.receive` 使用合成密钥验证 HMAC → 正式 `submit_platform_payload` → `MessageChain.process_platform_payload` 按连接 ID、来源和传输类型找配置 → 由 provider 的 module_id 通过既有模块调度器固定宿主 parser，跳过插件和其他平台 parser → 平台产生 IncomingMessage 主体/群组/线程事实 → 宿主创建带 provider、连接 namespace 和 authenticated_sdk 来源的私有证明 → 消息入口及公共授权门禁 → 平台主体规范化 → 真实授权 UoW/Oper → 申请 pending → 正式管理员 HTTP 审批接口 → 原连接私聊回执 Outbox → 下一条已认证消息 → 公共执行边界 `resolve_operation` 再查角色、范围和业务归属 → 外部业务边界。

规则链：相同认证入口 → 当前连接和规则 revision 复核 → 平台规范个人与群组 → 公共规则匹配 → `resolve_operation` 建立受限归属 → 外部媒体搜索边界。规则只给予明确的 search 范围，不批准成员、不授予管理员。已拒绝/停用、其他群组、连接或 namespace 的证明不能执行。旧验签回调继续使用模块生成的私有证明；新增来源字段为兼容旧回调可省略，公开 JSON 仍无法设置证明。

用例保留全部 12 个内置平台声明和配置，增量加入独立 `platform:boundary-fixture`，不复用 Web/WebAgent，不缩减原目录。验收实际执行 SDK 认证、宿主分派、申请、管理员接口、规则和拒绝路径；数据库、审批和权限判断不使用替身。外部平台传输和媒体搜索终点使用合成实现，不访问真实租户。

反向用例覆盖无效签名、报文伪造 provider/连接、群组冒充个人、审批前执行、跨连接、停用、拒绝、跨群组、重放、origin provider/namespace 不一致。另加入共享传输的独立 provider，验证不同规范主体与授权不混用；未绑定连接的歧义查询必须拒绝。Web/WebAgent 外部交互声明明确被目录拒绝。

## 统一政策与平台约束

| 宿主统一政策 | 所属平台的协议约束 |
| --- | --- |
| 只有明确连接和个人主体才能强制私聊，清除群组/线程/编辑上下文，禁止失败后回退广播，必须确认结果 | 私聊地址正则、个人/群组前后缀、单个用户编码 |
| 审批、角色、业务范围、归属、撤权与 namespace 复核 | 从已认证协议产生个人、会话和线程事实 |
| 通用令牌编辑接口的 32–256 字符输入要求和敏感值不回填；旧配置导入不会据此被重新归类为可信 | 专属配置键、平台密钥格式、凭据核验接口与更严格约束 |
| 通知受众、时间、Outbox 冻结清单和重试规则 | 地址编码、原生附件与历史分段的格式要求 |

通用 DTO 不再用 VoceChat、Telegram 或飞书专属说明解释通用入站令牌及消息格式提示。公开兼容字段仍保留，实际格式校验由模块执行；历史通知开关 DTO 和固定迁移的旧平台字段属于显式兼容边界，不用来描述新平台能力。

## 架构门禁

`channel_platforms.py` 跟踪函数内的 provider/channel 局部别名、枚举导入/赋值别名、字符串比较、match 和字面量平台字典的下标/get 查表；具体模块的静态导入、import_module 及其别名也被拒绝。反例用例包含新平台名字，不能只依赖现有平台关键词。

动态注册目录及能力事实映射、两个动态 provider 的兼容性比较、注释与文档均允许。分析是限定源码边界内的语法及局部数据流约束，不宣称证明任意反射或跨进程代码安全；实际入站和执行边界仍必须验证权限。门禁无写基线或自动豁免模式。

## 源码与证据入口

本地 `current-source.json` 指向这轮前后端实际工作树，验收时以 `review_target=index_tree` 和 tree hash 固定源码；经用户要求提交后，索引记录对应 commit 和已验收 tree；`platform-boundaries-current.json` 给出最新验收 manifest。验收引用必须核对 path、commit 和 tree，不能以旧 PR 链接或旧一轮 review/manifest 代替。

运行环境使用独立 `current-runtime.json`；源码指针同步不会自动切换手测环境、推送或发布。真实企业微信测试继续按用户要求跳过。最新检查结果见[验收记录](validation.md)。
