# v3 架构重构进度

> 分支 `refactor/v3-pure`，基线为官方 `upstream/v3` @ `4dc713f02`。
> 目标：移除 v2 架构残留，让宿主只有一套 v3 架构，不做双架构缝合。
> 诊断依据见 [`architecture-resurvey-upstream-v2v3-2026-08-17.md`](architecture-resurvey-upstream-v2v3-2026-08-17.md)。
>
> *Last Updated: 2026-08-17*

---

## 一、已完成

### 1.1 包级单向依赖（唯一硬性要求）

基线上存在 **4 对包级双向循环**，现已清零：

| 循环 | 清偿方式 |
|---|---|
| `schemas ↔ runtime` | `schemas/i18n.py` 翻译挂钩，实现由组合根注入 |
| `application ↔ agent` | 技能管理器经 `application/agent.py` 门面 provider 惰性解析 |
| `db ↔ application` | 订阅候选的行→业务对象翻译上移应用层边界适配器 |
| `adapters ↔ runtime` | 宿主环境探针下沉 `foundation/hostenv.py`，`SystemUtils` 委托 |
| `chain ↔ workflow` | 工作流服务迁入 `workflow/service.py` 并脱离基类继承 |

反向边 `modules → application` **已清零**（35 条 → 0）。扩展不再 import 应用服务实现：

- `runtime/hostport.py` 提供端口槽位；`directories` / `storages` / `naming` /
  `siteresource` / `filterrules` / `ruleexpression` 六个端口各自只声明模块
  实际调用到的方法，由组合根 `startup/hostport_initializer.py` 惰性注入实现。
- 媒体根路径推导下沉 `domain/mediapath.py`：返回问题描述而不打日志，
  日志级别交回各层调用侧决定（domain 不依赖 runtime）。
- URL 与路径安全原语迁入 `adapters/network/urlsafety.py`，
  原位置再导出以承接兼容层映射与 SDK 导出。
- 规则解析器与内置规则集下沉 `domain/filterrule.py`：领域层声明加速后端协议、
  未注入时回落纯 Python 解析，rust 实现由组合根注入（与识别加速同一装配点）。
  下沉后模块可直接依赖领域层，为绕开该阻碍而建的端口脚手架随之删除。

### 1.2 分发内核：v2 聚合 → v3 能力索引三级分发

v2 内核的问题是调用方无法表达意图：`run_module` 用一套歧义协议
（插件先行 → 首个非空 → 签名匹配则管道传递 → 列表则合并 → 否则中止）
兼顾通知、收集与仲裁三种需求，模块选择是 O(n) 全量扫描。

现状：

- `ModuleManager` 建立方法名能力索引 `providers_for`，按代际失效，
  能力从**运行期实例反射**推导（不信任枚举标签）。
- `ModuleInvocationDispatcher` 提供三级语义（含异步）：
  - **广播** 通知全体、不收答案，遍历是其固有代价，刻意不索引化；
  - **多播** 走索引收集族类内全部非空答案；
  - **单播** 与多播同一候选集，叠加短路取首个非空，无人认领返回 `None`。
- 全树 **250 处聚合调用完成迁移，仅存 2 处**（`ChainBase` 与两个 Mixin 的 64 处、
  具体链的 186 处）。分类以逐端口取证为准：单播占绝大多数（族类内单一答案）、
  多播用于原本依赖列表合并的场景（仪表板统计、媒体库壁纸、跨提供者艺人专辑）、
  广播用于只通知不取答案的场景（命令注册、定时钩子、缓存清理）。
- 累积管道（`obtain_images` 族：Fanart、TheMovieDb、Douban 按优先级依次在同一个
  产出上继续富化）升为第四个显式原语，宿主对聚合分发的调用归零（见 1.14）。
- `run_module` 保留在处理链基类上，只服务插件生态。
- 迁移中确认并规避的三类陷阱：
  - **提供者以空列表让出**：`[]` 不是"未认领"，单播会短路。艺人专辑与
    媒体库壁纸因此改用多播。
  - **提供者以 `False` 让出**：同样会被单播当成认领，逐个核对了返回值。
  - **缓存清理类端口**：改广播后，插件返回真值不再短路掉宿主模块的实际清理。
- 跨层穿透一并消除：推荐链不再直接驱动其它链的分发原语，改调其公开方法；
  TMDB 发现与趋势端口补齐 `raise_exception` 透传，避免异常语义在改调后丢失。

### 1.3 上帝基类拆解

取证：34 个 `ChainBase` 子类中 **23 个不使用任何能力端口**，
无一使用超过三个业务域——继承下发纯属负担。

- 52 个端口按七域外迁 `app/application/orchestration/ports/`：元数据、搜索、下载器、
  整理分类、媒体库、报文解析、系统钩子；实现只保留一份。
- `ChainBase` 保留全部同名端口作一行转发（插件契约面零变动），
  端口方法内直接调用分发原语的地方由 64 处降为 0。
- 客户端持有分发宿主而非自建调度器，服务可只组合所需域。
- `WebhookChain` 作为示范不再继承基类，只组合报文解析域。

### 1.4 编排面收敛

`SchedulerChain` / `CommandChain` 不再继承 `ChainBase`，
改为持有并按需委托消息与分发设施——调度器与命令注册表回归进程级服务。

### 1.5 宿主脱离 v2 兼容路径

宿主代码对 `app.core` / `app.helper` / `app.utils` / `app.log` 的引用**归零**
（插件基类原先就走这些虚拟路径，等于每个插件天然锚定 v2 旧世界）。
兼容层本体完整保留，`_PluginBase` 公开 API 一字未变。

### 1.6 扩展点去硬编码

| 扩展点 | 原状 | 现状 |
|---|---|---|
| Indexer Spider | 硬 import + 静态字典 + 同步/异步两条 if/elif 链，同一知识四处表达 | 单一注册表，构造参数按爬虫自身签名推导 |
| Agent 内建工具 | 82 项静态元组 + 90 行硬 import | 目录扫描发现，强断言锁定工具清单与顺序 |
| 模块 subtype | 必须是 6 个内核枚举成员，否则启动失败 | 降为可选元数据，仅通知渠道与存储后端声明；取值不再由内核枚举把守（见 1.12） |
| 存储后端 | 嵌在文件管理模块内的第二套机制：扫死一个包路径、身份由 `StorageSchema` 把守、模块内按标识 if/elif 路由 | 七个后端各自成为一级模块（`app/modules/{alipan,alist,alistgo,localstorage,rclone,smb,u115}`），能力方法按存储标识自筛、单播首个认领者短路；标识仍是自由字符串 |

存储后端上浮时顺带消除一处隐蔽缺陷：原发现路径每次初始化都重载后端模块，
使插件导入到的类与模块选中的不是同一个类对象。未登记的路径前缀此前被当作
本地存储并把路径糊成拼接结果，现按通用 URI 文法正确拆分。

### 1.6.1 文件管理模块拆散

原 `app/modules/filemanager/`（7600 行 / 12 文件）把三件不同性质的事捆在一起，现已拆净：

| 成分 | 去处 |
|---|---|
| `transhandler.py`（1344 行整理编排） | `app/application/transferhandler.py`（服务层）；存储操作以 12 方法窄协议解耦 |
| `storages/` 七个后端（约 5000 行） | 七个一级模块，见上表 |
| 12 个存储路由方法 | 随后端迁出，改为按存储标识自筛 |
| 剩余的媒体库职责 | `app/modules/medialibrary/`（2 文件）|

剩余模块拥有"媒体库目录"这一概念：跨存储的整理落地（需成对的源与目标操作对象，
任何单个存储后端都拿不到）、磁盘侧的存在性与文件查询（`media_exists` 与四个
媒体服务器族并列应答：电视剧收齐各来源答案后按季集取并集，电影与音乐取首个
非空答案，指名服务器时让出）、命名推荐与目录配置自检。
它与媒体服务器"自己库里有什么"是并列的两种视角，`filemanager` 之名已名不副实，
故按实际职责更名，`app/modules/filemanager/` 目录随之消失。

能力清单的优先级与子类型未动，仲裁顺序不变；旧包路径与旧类名经兼容层
解析到同一对象，插件反射与 Pickle 往返已实测。

存储模块与下载器、媒体服务器同构，由能力清单发现与启停；
`runtime/extensions/storage_registry.py` 只保留「按标识直取后端」这一项职责，
登记由各存储模块的生命周期驱动，供整理编排取用成对的源、目标操作对象。

`StorageSchema` 保留为已知值目录（存量配置、前端展示、本地存储引用），
仅解除其"发现门槛"职责。

### 1.6.2 过滤契约化

过滤此前把契约与实现焊在一起：规则表达式组名直接出现在能力签名里，
等于宣告"过滤就是跑规则表达式"，换一种分析方式连签名都对不上。

- `schemas/filter.py` 定义判定契约：分析器标识、是否通过、判定依据、排序权重；
  判定列表与候选列表按下标一一对应。标识与依据均必填，否决可追溯到具体分析器。
- 分析能力经既有能力分发以**多播**接入，**不新建注册表**——插件在 `get_module()`
  里提供同名方法即被看见，与内置判定按**合取**组合（任一否决即否决）。
- 内置规则引擎成为该契约的第一个实现，过滤流程收敛为多播分析、合取组合、
  应用判定三步；排序权重取首个给出者。
- `filter_torrents` 的签名与单播分发未动，插件沿用它仍是整体接管。
  两条插件路径的语义分别写入 docstring。

至此 `app/modules/` 的判据自洽：每个模块要么适配一个外部系统，
要么提供一族可互换或可组合的实现。

### 1.7 治理换轨

`tests/test_architecture_dependencies.py` 新增**包级允许依赖矩阵**断言，
取代结构上不完备的禁止前缀黑名单。负债清单 `DEPENDENCY_DEBT` **已清空**，
矩阵成为无例外的硬约束：

- Agent 工具经 `runtime/diagnostics.py`、`runtime/workflows.py` 端口取用
  自检诊断与工作流执行，扩展之间不再互相 import。
- 认证依赖下沉 `application/security/dependencies.py`：函数名、签名、
  `Depends` 链、状态码与文案逐字保留；`api/deps.py` 再导出使端点侧零改动，
  插件兼容门面改从应用层取用，SDK 不再反向依赖入口层。

### 1.8 服务层合并

`app/application/` 成为唯一的服务层包，跨入口复用的用例编排收敛为其
`orchestration/` 子包（44 个文件），两个顶级包并列且边界含糊的状态结束。

- 896 处导入路径由脚本统一重写，覆盖 `app/` 与 `tests/` 共 207 个文件。
- `app.chain.*` 登记为兼容层旧导入根（45 条别名 + 虚拟包），
  存量插件直接 import 具体链类的写法不受影响，且旧路径与新路径解析为同一对象。
- 未选用 `app/services/` 作为包名：它是上游退役并设有防复活断言的名字。
- 门禁的编排层专属断言（不得穿透模块内部、不得依赖 Agent 实现、
  不得引入下载器 SDK）意图保留，仅同步路径。

### 1.9 单一 Extension 契约

`runtime/extensions/contract.py` 声明扩展的共同面：发行方式（预装 / 市场）与
失败归属、身份、生命周期、能力与钩子探测，以及分发用的提供者视图与来源协议。

- **两个基类源码零改动**，经适配器投影成契约视图：模块侧映射
  `init_module`/`stop`/`test`，插件侧映射 `init_plugin`/`stop_service`/`get_state`。
  协议声明语义、名称映射留在适配层——两者的打包模型本就不同
  （包级单例 + 配置开关 vs 多实例 + 安装清单），合并基类会破坏插件生态。
- 分发内核只消费提供者视图，不再假设扩展的具体形状：错误按失败归属分流、
  签名接力与逐调用日志按视图声明。六个成对方法合并为四个通用实现。
- 能力索引、插件投影与插件生命周期改经契约探测；重复的钩子与能力探测合并为一份。
- **验收自评**：新增一种发行方式无需改动分发内核、能力索引与投影，
  已由一个只实现协议的第三方来源接入四级分发验证；组合根仍需一行装配。

已知限制：插件生态没有连通性自检契约，故插件视图的自检恒为空
（把它映射到启用态会把"已启用"误报成"可连通"）；能力索引仍返回模块实例
而非视图，因为编排层与既有测试依赖实例身份。

### 1.10 SDK 成为兼容清单的受校验投影

兼容清单里 58 条弃用建议的 `replacement` 指向 `app.sdk.*`，而运行期
`target` 只有 9 条经过 SDK——即绝大多数"请改用 SDK"的建议从未被校验过。
SDK 在宿主生产代码中零消费者（这是对的，SDK 面向插件），叠加的后果是
插件作者照建议迁移后拿到什么，没有任何测试保证。

现在建立的不变式：**每条指向 SDK 的弃用建议，都必须在该位置兑现同一个对象**。

- 实测缺口 **66 个符号**（承诺了但未导出），身份漂移 0 个；已按只增不改名补齐。
- 15 个符号刻意不进 SDK 并记录理由：装配用的 `configure_*` 提供者注册口
  （插件调用会改写全局宿主行为）、连接池关停与调用栈回溯等宿主生命周期内部。
- `scripts/sdk/exports.py --write/--check` 生成"清单要求 SDK 提供什么"的数据表。
  **刻意不生成 SDK 模块本身**：SDK 的公开导出一旦加入即不可撤销，
  把永久承诺变成生成器副产品是危险的；清单新增条目时 `--check` 变红，
  是否加进 SDK 仍需人工决定。含行为适配的模块（旧关键字参数转发、签名伪装、
  子类覆写）只校验不生成。

### 1.11 模块类型枚举退役，服务实例按配置归属定位

`ModuleType` 到最后只剩一个真实职责：按族找出模块、再问模块要其内部的服务实例。
其余四十余个模块的类型取值无任何代码读取，属于被迫声明。

- 能力清单新增可选的 `metadata.service_config`，20 个多实例服务模块声明自己消费哪个
  配置键；清单校验要求该字段必须同时出现在配置变更监听里，确保配置改动能重建实例。
- 服务注册表按配置归属定位模块实例，取代按类型枚举查找；模块内两百余处实例取用零改动。
- 类型枚举、41 个模块的 `get_type()`、按类型与按子类型的查找一并移除，
  其中按子类型查找早已无调用者。

同期落地扩展实例的运行期标识（`app/runtime/extensions/contract/instance.py`）：默认实例的实例键
退化为裸扩展标识，因此单实例扩展的取值与不区分实例时一致。实例键构造只约束不含分隔符，
路径安全校验移到真正构造路径处——插件数据目录此前完全没有校验。

### 1.12 渠道标识开放：最后一处闭集焊死

`NotificationChannel` 此前是内核里的闭集：新增一个消息渠道必须改 `app/schemas/types.py`
（加枚举成员）和 `app/schemas/notification.py`（加静态能力表条目），插件无从参与。
判定它是焊缝而非类型系统硬约束的依据：

- 落库的 `channel` 全部是 `String` 列，枚举只活在内存；
- 12 处 `NotificationChannel(...)` 转换点**全部**有 try/except 守卫，未知取值降级不崩溃；
- 姊妹族早已开放——`FileItem.storage`、`DownloaderConf.type`、`MediaServerConf.type`
  都是自由字符串，`storage_backend_identity` 用 `getattr(schema, "value", schema)`
  同时接受枚举与裸字符串。通知渠道是唯一把标识泄漏进 Pydantic 字段类型的一族。

改动：

- 标识归一收敛为一份实现。渠道在接口、配置与插件之间以枚举对象、枚举取值、枚举成员名
  三种形式流通，此前 `runtime/channels.py`、`db/oper/agentchat.py`、`api/endpoints/agent.py`、
  `agent/llm/capability.py` 各写各的转换，`wechatclawbot` 甚至在模块内手写三态兼容。
  现由 `resolve_channel` / `channel_identity` 统一收口。
- 能力表双轨：内建 10 渠道的静态表内容一字未动，新增按登记方整体替换的扩展登记表，
  查表内建优先、未命中查扩展。插件经 `_PluginBase.get_channel_capabilities()` 声明，
  由插件投影按严格契约校验后在加载、配置变更、停止三个生命周期点同步登记；
  停止走无条件撤销而非重算，因为终止不翻转插件自身的启用态声明。
- 传输模型放开：`Message` / `IncomingMessage` / `MessageResponse` /
  `ResourceDownloadEventData` 的 `channel` 改为入模型前归一，内建渠道仍得到
  同一枚举对象，既有 `==` 比较与 `to_dict()` 行为不变。
- 修掉 25 处 `channel.value`：扩展渠道是字符串，取 `.value` 会抛 `AttributeError`。

顺带暴露并修掉的缺陷：`app/agent/tools/base.py` 的管理员判定对任何非内建渠道
恒返回 `False`——`matches_channel_admin` 本身早已接受字符串，是这层多余的枚举
构造把扩展渠道的管理员挡在门外。

同时退役无人读取的 `subtype`：`metadata.subtype` 的唯一读取点是它自己的校验逻辑，
`/modulelist` 不返回该字段；`get_subtype()` 的真实读取点只有通知渠道的管理员回退、
`wechatclawbot` 自过滤与存储基类三处。据此删除 27 个模块的 `get_subtype()` 与
对应的清单声明、以及 `_ModuleBase` 上的全域契约声明；`subtype` 降为可选元数据，
仅通知渠道（标识必需）与存储后端（由后端 schema 推导）保留，共 17 个。
清单校验中"通知渠道 subtype 必须是内核枚举已登记成员名"这条准入被删除。
`DownloaderType` / `MediaServerType` / `MediaRecognizeType` / `OtherModulesType`
四个枚举保留在 `app/schemas/types.py`——宿主不再使用，但它们是插件可直接 import
的公共词表，删除是无谓的生态破坏。

### 1.13 分发面源前缀方法名归一

分发面按「源 × 实体 × 类型」笛卡尔展开：`tmdb_person_detail` / `douban_person_detail` /
`bangumi_person_detail` 是同一能力的三个方法名，`movie_credits` 与 `tv_credits`、
`movie_similar` 与 `tv_similar` 还把 `mtype` 编码进方法名。新增一个数据源要在方法名族上
新增一整族，插件无法以对等身份提供同一能力——插件想接管"人物详情"，必须猜中并覆盖
调用方实际询问的那一个源前缀方法名。

六个多来源能力契约把源与类型降为参数，登记于
`app/runtime/extensions/contract/module_method.py` 的 `_MULTI_SOURCE_CONTRACTS`：

| 契约 | 覆盖来源 |
|---|---|
| `match_media` | TMDB、豆瓣、插件 |
| `person_detail` / `person_credits` | TMDB、豆瓣、Bangumi、AniList、插件 |
| `media_credits` | TMDB、豆瓣、Bangumi、AniList、插件 |
| `media_recommend` | TMDB、豆瓣、Bangumi、AniList、插件 |
| `media_similar` | TMDB、插件（豆瓣、Bangumi、AniList 均未实现，不进能力索引） |
| `discover` / `discover_board` | TMDB、豆瓣、Bangumi、AniList、插件 |
| `media_detail` | TMDB、豆瓣、Bangumi、AniList、TVDB、插件 |

判据：

- 数据源与媒体类型降为参数（`source`、`mtype`），不再编进方法名；
- 非本来源返回 `None` 让出，调度据此询问下一来源；返回空列表会被单播当成已认领
  而短路，因此这一让出协议对所有来源硬性统一；
- 契约方法只委托原方法，不重新实现——缓存与限流仍挂在被委托的原方法路径上；
- 本来源不支持的参数（如 Bangumi、AniList 的 `media_detail` 不支持 `mtype`、`season`）
  就地丢弃，在参数说明里注明，不静默改写调用方语义。

刻意不做：不为榜单引入枚举契约与专属传输模型——`discover_board` 的 `board` 标识本就
按来源各自的白名单校验，没有消费方读取一份统一枚举，硬造出来是死代码；相似推荐
（`media_similar`）单独成契约而不与推荐（`media_recommend`）合并，因为它只有 TMDB
一个内建实现，豆瓣、Bangumi、AniList 均未实现，未实现的来源不进能力索引，
因而不需要为它们编写让出逻辑。

旧的源特定方法名（`match_tmdbinfo`、`match_doubaninfo`、`tmdb_info`、`douban_info`、
`bangumi_info`、`async_bangumi_info` 等）全部保留为公开访问器，只是退出分发面——
内部改为委托统一契约。**对外部插件市场的影响**：若某插件在 `get_module()` 里挂的方法名
是旧的源前缀写法（如 `tmdb_person_detail`），归一后分发面已不再以该名义提问，该方法
不会再被分发到，是一条静默失效路径，插件作者需要把方法名改成 `person_detail` 并以
`source` 参数收窄。

`contracts.py` 的 `_PREFIX_CONTRACTS` 随之删除 `tmdb_` / `douban_` / `bangumi_` /
`anilist_` / `tvdb_` 五条前缀规则——那是「源编进方法名」问题的成文化。删除后这些方法名
退回默认的 `legacy` 契约，字段值与原先的专属 family 完全一致（聚合方式、同步/异步支持、
插件短路开关三个字段皆为默认值），故行为不变，只是不再拥有专属 family 标签。`music_`
与 `torrent_` 两条非源前缀规则保留。

`tmdb_collection`、`tmdb_seasons`、`tmdb_group_seasons`、`tmdb_episodes`、
`tmdb_cache_items`、`tmdb_cache_delete`、`tmdb_cache_clear`、`tvdb_slug` 八个分发方法名
仍带源前缀，但它们是 TMDB、TVDB 各自 API 原生的结构（合集、季、剧集组、识别缓存管理、
TVDB 详情页别名），没有跨源等价物，从未落入「源 × 实体 × 类型」笛卡尔展开问题，
不在本轮六契约归一范围内；`tests/test_dispatch_surface.py` 的门禁对它们显式登记豁免。

`tests/test_dispatch_surface.py` 新增 AST 静态门禁：扫描 `app/application/` 下全部
`unicast` / `async_unicast` / `multicast` / `async_multicast` / `broadcast` /
`async_broadcast` / `run_module` / `async_run_module` 调用，首个位置参数为字符串字面量时
不得以 `tmdb_` / `douban_` / `bangumi_` / `anilist_` / `tvdb_`（含 `async_` 前缀变体）开头，
防止「源编进方法名」问题回潮；豁免清单另有测试守着不得腐化为无引用条目。

### 1.14 第四分发原语与三处机制收敛

**累积管道升为显式原语**。图片获取是三个来源按优先级依次在同一个产出上继续富化，
广播不收答案、多播各答各的不接力、单播首个非空即停，都表达不了它，此前只能骑在
聚合分发上并继承那套歧义协议。管道原语的候选集与多播单播同源，提供者返回空结果时
保留上一轮产出继续传递，无提供者时原样返回初始值，单个提供者出错不中断接力。
宿主对聚合分发的调用由此归零，该方法保留在处理链基类上只服务插件生态，
并有门禁断言其调用清单为空。

**站点解析器收敛为注册表**。此前同一份知识表达三处：扫包发现、枚举把守身份、
线性比对取用。现由解析器自行声明所承载的站点标识，定义即登记，取用按标识直取；
标识不再由枚举把守准入，站点枚举保留为已知值目录。

**包内文件级环拆除**。大模型提供商注册表与模型构建门面此前互相以函数内延迟导入
规避导入期失败——环并未消失，只是从静态引用里藏了起来。现由注册表声明所需能力的
窄协议，组合根注入实现。注入点取无条件执行的模块初始化而非受开关门控的智能体
初始化：测试模型连接必须在用户开启智能助手之前就能用。全树文件级强连通分量归零。

**插件契约补两处**。渠道能力声明使插件可作为消息渠道被正确渲染（此前能力表是内核
闭集）；连通性自检使插件视图据实给出三种状态——未给出结论、可连通、不可连通，
此前恒为未给出结论，而把它映射到启用态会把「已启用」误报成「可连通」。

**分发面归一的插件迁移提示**。八十七个带数据源前缀的分发名退出分发面，插件经模块
方法表胁持系统实现时按方法名精确匹配，挂旧名的插件从此永不被触达。插件模块表投影时
按登记表给出迁移提示，指明插件、旧名与应改用的契约名；只记录不改写方法表。
挂新契约名的另给一条提示，说明该契约覆盖多个数据源、须按来源自认领——归一后覆盖
一个契约名等于一次性拦截其全部来源，而归一前只能拦截一个。

---

## 二、待办

清单已清空。后续如需继续，可考虑的方向：

1. **`ChainBase` 去继承**：宿主侧 **33 处继承**（31 直接 + 2 间接）。注意按
   `grep "(ChainBase)"` 只能数到 25——漏掉 `(ChainBase, metaclass=Singleton)`
   与多基类写法，而漏掉的 6 个里有 4 个是全树最大的编排类。判据须同时取
   「类体内 `self.X` 使用」与「类外消费者对该实例调用基类成员」两项，只做前者会
   把五个纯壳类误判为零使用。

   分批：**A 组 22 个**改持 `ModuleCapabilityDispatch`（`ports/dispatch.py` 已就绪，
   `WebhookChain` / `CommandChain` / `SchedulerChain` 是现役样例）；**B 组 6 个**
   需先把 `NotificationMixin` 提为组合式服务——它有 15 个消费者，是真正的关键路径；
   **C 组 4 个**跨 2–4 个端口域，组合写法不会更啰嗦；**`MediaChain` 单列**：它用 28 个
   基类成员跨 4 域，改组合转发比现状更啰嗦且换不来解耦，正确动作是把元数据域转发与
   `RecognitionMixin` 整体搬进它；**`PluginChian` 不动**，它是插件承诺的命名锚点。

   有利条件：`isinstance(x, ChainBase)` 全树 0 处，宿主侧以其作类型标注 0 处，
   基类无抽象方法。完成判据：宿主继承为 0，唯一子类是 `PluginChian`。

   顺带待修：`ChainBase.__init__` 每次都向消息队列传 `self.multicast` 作回调，
   但队列是单例，只有进程内第一个被构造的链生效，其余每次实例化都在白传参。
2. 能力索引的返回值从模块实例改为契约视图，受编排层与既有测试的实例身份依赖阻碍。
3. **插件多实例**：同一插件类按配置扇出多个独立实例（各自启用态、日志等级、
   业务参数、数据目录），当前只有物理复制文件、改类名生成新插件 ID 的克隆机制。
   前置依赖是插件自管理数据库（独立 MetaData 与库文件），当前全局单例 DB
   结构性阻断插件自有表。1.11 落地的扩展实例标识只服务内建宿主模块，
   插件层零引用，可作为接入点。
4. **插件模块注册 SPI**：插件目前只能经 `get_module()` 注入方法表参与分发，
   不能声明系统模块的入口与生命周期，也无法向能力内核做运行期注册。

---

## 三、验证基线

| 阶段 | 结果 |
|---|---|
| 官方 v3 基线 | 4904 passed / 1 failed |
| 当前 | **5555 passed / 0 failed**（另 3 skipped、14 subtests passed） |

官方基线上那条失败是
`test_legacy_plugin_resource_imports.py::test_scanner_invalidates_equal_size_source_with_preserved_mtime`：
容器文件系统对 inode 时间戳只提供粗粒度时钟，用例内紧邻的两次写入常落在同一刻度，
拿到完全相同的 `(mtime, ctime, size, dev, ino)`，"内容已变"在时间戳层面不可区分。
症结是同一用例内两次写入之间缺乏可观测的时间戳差异，与执行顺序相关而非缓存污染。
现补自旋等待辅助函数，在替换文件前触碰探测文件直到观测到新刻度（带超时兜底），
断言强度不变，只是让被测场景的时序前提在任意环境下都成立。

架构基线快照（`tests/fixtures/architecture/*.json`）与 `app/schemas/exports.py`
在每轮改动后由 `scripts/architecture/baseline.py --write` 与
`scripts/schema/exports.py --write` 统一刷新。
