# 渠道接入、SDK 与兼容边界

导航：[设计](design.md) · [计划](plan.md) · [任务](tasks.md) · [验收](validation.md) · [平台职责](platform-boundaries.md)

## 四种不同的契约

| 对象 | 实际职责 | 不提供的能力 |
| --- | --- | --- |
| 平台客户端 SDK | Telegram Bot API、Discord/Slack SDK、飞书长连接、企业微信/iLink 客户端拥有平台连接、认证、协议应答和重试 | 不批准用户、不选择业务账号、不决定允许操作 |
| 宿主平台扩展 | 既有 `CapabilityRegistry` + `host_module` 清单 + `channel_extension` 指定的平台实现产生个人、群组、线程、地址和凭据事实 | 能力与版本声明不能授予权限，也不能让插件变成宿主 owner |
| 公共插件 SDK | `app.sdk.channel` 第一版只重用规范类型和 `ChannelPlatform` 协议；已有 `app.sdk.feishu` 继续提供传输客户端 | 没有平台注册器、运行目录、配置装配服务、可信接收器注入或私有 `AdmissionProof` |
| 历史兼容接口 | 旧导入名、模块方法、HTTP 回调和旧配置转换，进入当前 canonical 实现 | 不恢复旧授权引擎，不因使用旧名字而获得新信任 |

Python 插件本身运行于受信任的宿主进程中，本契约不是任意 Python 代码的安全沙箱。这里约束受支持 API、宿主实际调用链及入口可接受的认证事实；公开 JSON 和插件普通扩展接口不能提交私有证明。

## 源码来源与归因

本轮实现提交为 `42d905f03`，基于 `0ccb50397`；审阅的官方目标源码为 `86635b5ae`。远端 PR #7567 的内容另存审阅索引，本轮没有推送；不能用该远端 head 代替本地实现。前端仅文档提交 `148c6e7d`，生产与测试代码仍与 `81b53931` 一致。

平台客户端、自研传输、平台确认顺序和公开 HTTP 是既有实现，本轮核对其实际使用方式，没有把依赖某个第三方包当作完成宿主 SDK 接入的证据。上一轮平台边界调整引入了认证载荷入口，但缺少装配时仍返回空值并隐式走旧 HTTP；企业微信另有私有文本接收器。这些是本次 PR 的过渡路径，本轮统一退出。公开插件导入和历史配置/升级接口则继续适配，未以本仓零引用作为删除依据。

## 内置渠道接入清单

以下共 12 个 provider；企业微信两种接入模式仍属于同一 provider。表中的确认表示平台协议应答或任务提交，不等同于业务审批成功。

| provider | 平台接入与认证 owner | 当前宿主入口 | 公开兼容、确认和重试 |
| --- | --- | --- | --- |
| telegram | `telegram/telegram.py` 调用自研 `TelegramBotApi`，官方 Bot API 凭据轮询；模块解析个人、群组与话题 | `_forward_to_message_chain` → 连接绑定提交 → 固定 `TelegramModule` parser | 显式旧 HTTP helper 保留。按钮平台确认仍在业务提交之前；SDK 轮询和平台异常重试归客户端 |
| slack | `slack/slack.py` 的 Bolt App + Socket Mode，应用/OAuth 凭据 | 绑定提交 → `SlackModule` parser，用户/频道/线程由模块解释 | 原协议 `ack()` 顺序保留。`timeout` 关键字在私有适配方法中保留，但进程内提交不进行 HTTP 等待 |
| discord | `discord/discord.py` 的官方客户端/Gateway、机器人 Token | `_post_to_ds` → 绑定提交 → `DiscordModule` parser | Discord 的 defer/平台响应仍归客户端；旧异步 HTTP helper 仅作为显式插件兼容入口 |
| feishu | `feishu/feishu.py` 的 `FeishuOpenApi`、`FeishuLongConnection`，应用认证和网关帧归传输实现 | 绑定提交 → `FeishuModule` parser | 帧应答语义保留，卡片 toast 按队列是否接受显示结果；旧 helper 保留。宿主长连接采用自研传输，已存在的 lark-oapi 插件依赖不在本轮删除 |
| qqbot | `qqbot/qqbot.py` + `gateway.py`，应用凭据、Gateway 事件与平台 ID | 绑定提交 → `QQBotModule` parser | SDK 事件缓存与平台重连保持原语义；当前缓存登记先于提交，不宣称失败事件必被重投 |
| wechat（智能机器人） | `wechat/wechatbot.py`，当前官方长连接订阅认证、本机器人 ID、当前 socket 及停机检查 | 文本和私聊附件均进入绑定提交 → `WechatModule` parser；群组文本需要宿主 SDK 解析上下文 | 不再单独构造/注入已验证文本接收器，也不回环 HTTP。HTTP 兼容 JSON 不获得 SDK 标记；群组附件能力未扩大 |
| wechat（自建应用） | `WechatModule.message_parser` 拥有 XML 验签解密、应用 ID/时间/消息 ID 核验 | 公开 HTTP → 固定宿主 parser → 相同授权处理链 | 验证 URL 和现有回调保留；有效短时证明包含 provider 与身份空间。无有效平台证明的 API_TOKEN 请求不能使用规则豁免 |
| wechatclawbot | `wechatclawbot/wechatclawbot.py` 的 iLink 登录与官方服务轮询 | 绑定提交 → `WechatClawBotModule` parser | 原先先保存轮询游标的语义保留，不增加失败重投保证；模块消息缓存继续保留。登出/重新登录复用实例时保留绑定，停止事件阻止入站 |
| vocechat | 客户端拥有 REST 发送；`VoceChatModule.message_parser` 拥有回调令牌及 UID/GID 解释 | 公开 HTTP → 固定宿主 parser | 专用令牌才能形成规则所需证明。无专用令牌的旧回调仍受一般审批/权限约束；不把 API_TOKEN 当作平台证明 |
| synologychat | 客户端拥有发送与令牌核验；模块解释表单用户 ID 和消息标识 | 公开 HTTP → 固定 `SynologyChatModule` parser | 原表单回调保留；不能证明私聊分类时不开放私聊账号关联 |
| dingtalk | `DingtalkModule` 及客户端拥有机器人 webhook 发送与签名 | 仅通知发送，无机器人入站声明 | 不创建可信 SDK 入站，不因此新增授权或规则条件 |
| webpush | 浏览器设备登记与 Web Push 发送；登记按当前登录账号认证 | 设备登记 HTTP 和出站投递，无外部机器人入站 | 保留设备归属、切号和迟到操作隔离，不冒用保留的 Web/WebAgent 消息身份 |
| apppush | 设备配对/推送客户端与现有应用服务 | 仅设备配对和出站投递，无外部机器人入站声明 | 保留设备与账号归属；不将平台推送能力当成机器人授权能力 |

自定义 Telegram/iLink/企业微信网关原本不具备官方 SDK 的可信证明资格。本轮移除其缺少完整绑定后到旧 HTTP 的隐式降级；SDK 入口给出失败及诊断。已有显式 HTTP/API 接入继续按原认证、配置和授权边界处理，不通过自报连接 ID 提升信任。出站配置和发送能力保持原实现。

## 实际调用链

1. 宿主读取正式资源模型，依既有模块清单创建客户端。`_MessageBase._service_kwargs` 在平台配置展开后写入绑定，冻结连接 ID、provider、identity_namespace、模块解析 owner 和交互 revision；配置字典中的同名项不能覆盖绑定。仅投递、停用或交互策略停用的连接不创建此绑定。
2. 平台 SDK 完成自身协议认证和事实提取，调用客户端的绑定提交方法。入口未装配、授权服务未就绪、缺少身份、Web/WebAgent 保留身份、执行器拒绝或异常均返回 False 并记录稳定原因，不能再选择 HTTP。
3. 共享 `ThreadHelper` 接受任务只表示已入队；Future 取消和处理异常另行记录。执行前复核停止状态及接收器是否仍为原代际，生命周期重装后的旧任务不转给新接收器。
4. `MessageChain.process_platform_payload` 重新读取当前连接，核对 provider、身份空间、解析 owner、策略 revision 和停机状态。只通过既有调度器的 `_host_module` 选中该宿主 parser，跳过插件和其它渠道 parser。
5. 平台产生规范个人/群组/线程事实，领域拒绝群组冒充个人；宿主产生私有来源证明，按原规则认领事件。通用授权服务/UoW/仓储独立处理申请、审批、规则、业务账号及权限。新 SDK、版本或能力声明本身不会批准成员。
6. 申请保持 pending，管理员通过正式 HTTP API 审批并产生回执 Outbox；后续事件执行业务时再次检查审批状态、scope、归属及撤权。通知受众、发送时间、冻结分段、已确认进度及重试实现不变。

公开 HTTP 的 `MessageChain.process` 单独检查授权服务和停止状态。已配置来源按宿主清单固定 parser；旧回调省略来源时，只接受唯一的规范解析结果。没有宿主 owner 的旧插件解析 Hook 继续保留基础适配，但其返回的私有证明被清除；保留的 Web/WebAgent 身份必须走登录用户入口。HTTP 参数中的 `_authenticated_sdk` 会被移除，JSON 无法传入私有证明。

## 第一版公共 SDK

面向扩展作者的入口说明见[渠道 SDK v1](../channel-sdk.md)。

`CHANNEL_EXTENSION_API_VERSION = 1` 定义于现有 schema owner，`app.sdk.channel` 精确重导出同一对象和既有类型：`ChannelPlatform`、`ChannelActor`、`ChannelConnection/Info`、`ChannelPlatformCapabilities`、`IncomingMessage`、`Message`、`MessageAttachmentRead`、`InteractionPolicy`、`NotificationRoute`、`NotificationRule`、`NotificationChannel`。不复制 DTO，不导出模块模板、应用服务或发现/装配接口。

宿主清单使用 `metadata.channel_api_version = 1`。未标版本的旧宿主扩展按 v1 读取；未知版本、布尔或字符串版本被拒绝。模块清单还须符合既有 host_module、物理包和 entrypoint 约束。module_id、provider、显示名称及版本由宿主注册投影产生，扩展类的自报值不能覆盖这些装配事实。

v1 保留当前方法、参数语义和类型对象；增加必需方法或改变既有语义须经兼容审查并发布新版本，不能静默修改版本号或删除旧公开入口。数据模型可按已有规则增加有默认值的兼容字段。协议客户端的私有方法、私有证明、可信提交及装配函数不属于此承诺。

这个门面可供宿主渠道作者使用，也可供普通插件使用规范类型，但没有可安装插件的渠道注册 API。支持可安装插件渠道还需要定义独立的入口信任、认证委派、生命周期和权限边界，单独列入后续范围；本轮不以插件 Hook 冒充宿主 owner。

## 兼容台账与退出条件

本仓真实调用者按源码核对；无法据本仓零引用断言所有已部署插件均无依赖，因此未虚构弃用期限。

| 入口/数据 | 调用者及 canonical 目标 | 当前状态与安全边界 | 退出条件 |
| --- | --- | --- | --- |
| 7 个客户端对旧 HTTP helper 的隐式调用 | 原 Telegram/Slack/Discord/飞书/QQ/企业微信机器人/ClawBot 客户端；现改绑定提交 | 已退出，失败只在当前路径结束；架构门禁禁止重新导入或经别名调用 | 已满足，宿主调用已移除 |
| `forward_message_to_host`、异步版本、`submit_message_to_host` | 旧插件显式调用；`app.sdk._legacy.ingress` 和 7 个旧模块导入名经 `SYMBOL_ALIASES` 指向唯一 `application.messaging.ingress` 实现 | 继续保留原参数；仅发送普通 HTTP 数据，不构造证明；停止、执行器拒绝和 HTTP 失败明确失败 | 核实支持范围内插件迁移，并经公开兼容决策后才能移除，无既定日期 |
| 部分客户端曾导入的 `NotificationChannel` 名称 | Telegram/QQ/ClawBot 旧模块导入名 → 同一 schema 枚举，推荐 `app.sdk.channel` | 通过精确惰性别名保留，不恢复物理旧名；内置值、扩展 JSON 及无缓存解析不变 | 同公开导入名的兼容决策 |
| `submit_verified_message` / `configure_verified_message_receiver` | 上一轮重构的私有文本过渡端口，原唯一宿主调用为企业微信机器人 | 已退出；文本与私聊附件改用同一个绑定、固定 owner 的 SDK 入口，不为该私有注入能力建立公共别名 | 无 stable SDK 承诺且已无宿主调用，满足移除条件 |
| `submit_platform_payload` 及接收器装配 | canonical 宿主模块装配与启动层、隔离测试 | 内部可信能力，不属于普通插件 SDK。旧的短参数调用不再因缺字段而回环；需完整绑定才能提交 | 保留内部实现，不能以兼容名公开信任能力 |
| `ChainBase.message_parser`、`post_message` 等模块方法 | 旧插件模块调用 → 既有方法契约与调度器 | 公开 ABI 保留；宿主消息入口改用显式 owner 分派。解析结果是事实，不是业务权限 | 插件合同有独立版本承诺，本轮不移除 |
| 客户端既有发送方法及构造参数 | 旧插件直接使用客户端发送，仍由原平台实现负责协议 | 发送参数继续接受；单独的 name/connection_id 不能获得 SDK 入站信任。Slack 旧 timeout 调用继续接受 | 无已满足移除条件，本轮保留 |
| 公开 `/message/` 和 Web 消息 HTTP | API_TOKEN/平台回调及已登录 Web 用户 → canonical MessageChain | 路径、认证和响应结构保留；队列确认不等于审批；Web 身份不从外部 SDK 或插件解析结果取得 | 公开 API 未满足移除条件，本轮保留 |
| 旧 `/notification/config`、Notifications 配置 | 超级管理员兼容写入口 → `ChannelSettingsService.replace_legacy` → 原子资源更新 | 可表达的旧编辑进入当前资源；保护 provider/凭据/连接身份，检查 revision，不能覆盖新版规则或拆分开关；通用系统设置拒绝直接写资源键 | 需要旧客户端迁移证据和公开兼容决策，无既定日期 |
| 旧用户名/白名单/平台 flat 字段 | 正式升级及运行时配置转换 → 当前资源、主体与规则；Discord 旧 command_guild_id 由 Discord 解释 | 不作为第二套运行授权引擎；未知归属、群地址和别名冲突继续隔离 | 必须满足已发布升级和数据兼容要求后另审，本轮保留 |
| `database/legacy`、正式迁移、持久化模型 | `62c91ad8b057` 等已发布 revision → 固定离线算法与快照 | 不读取 SDK 版本目录、不联网、不依赖可变平台注册；原数据和 Outbox 语义不变 | 受正式升级支持范围约束，本轮保留 |

`MessageChannel`、`NotificationType`、`Notification`、`CommingMessage`、`WebMessageItem` 等既有公开别名继续使用原兼容映射，不在 canonical 层复活物理旧名。通用 API_TOKEN 只认证宿主 HTTP 调用，不能成为规则所需的平台来源证明。

## 可执行约束与验收入口

`channel_platforms.py` 已在原架构 CI 中运行：在原平台协议门禁之外，扫描宿主对旧 helper 的真实导入来源及模块/局部别名，保留旧实现和 SDK 私有兼容适配的明确边界；公开 SDK 禁止可信装配、私有证明、整个 ingress/启动模块或平台实现泄漏。渠道 v1 门面的导出集合和类型 owner 精确校验，动态运行代码不进入类型门面。约束是有界语法分析，不宣称证明任意反射代码安全。

- `test_channel_sdk.py`：v1 公开类型/方法合同、未就绪/缺身份/保留 Web 身份、停止和接收器重装、Future 取消/失败、旧插件导入、旧解析器证明清除及反例门禁。
- 企业微信私聊规则文本区分“入口不适用”和“提交失败”；两种提交结果都只选择一次入口，不再落入普通私聊分支重复提交。
- `test_channel_platform_extensions.py`：独立 `platform:boundary-fixture` 与全部 12 个内置平台共存，实际注册、SDK 认证、固定宿主分派、申请、HTTP 审批、已批准旧 HTTP 调用、规则执行及伪造/拒绝/撤权路径；版本和插件 owner 拒绝。
- `test_message_ingress.py`、平台协议测试：各客户端零回环、队列确认、HTTP 兼容和原协议行为。
- 既有授权、作用域、凭据轮换、固定升级、Outbox、分段恢复及同名重建回归继续执行。真实企业微信按用户要求跳过，使用隔离配置与模拟协议，不把模拟成功当作真实租户验收。
