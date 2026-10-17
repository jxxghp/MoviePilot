# v3 宿主 Mypy 治理记录

前五个条目各有独立提交；本批按用户要求一次评估当时剩余的 240 个问题文件，统一验证和提交。生产文件错误清零后才加入 `mypy.ini` 严格清单，仍有错误的文件只记录真实下降。完整扫描采用 Linux/Python 3.14 合同，按文件及错误码禁止增长。

## 已交付

| 生产文件 | 消除的错误 | 合同依据 |
| --- | --- | --- |
| `app/api/endpoints/anilist.py` | `attr-defined` 1 | `MediaInfo` 改从定义模块 `app.schemas.context` 导入；运行时对象身份不变。 |
| `app/api/endpoints/douban.py` | `attr-defined` 1 | 豆瓣响应模型改从同一定义模块导入，保留原模型对象。 |
| `app/api/endpoints/recommend.py` | `attr-defined` 2、`no-redef` 1 | 响应模型直接从定义模块导入；推荐来源事件结果按登记的载荷模型校验。 |
| `app/chain/subscribe/notify.py` | `union-attr` 6 | 通知构造和失败反馈使用已识别媒体的既有阶段不变量；识别失败在调用前返回。 |
| `app/sdk/string.py` | `attr-defined` 1、`no-untyped-def` 2、`type-arg` 1 | 兼容转发器保留动态旧关键字与反射签名，并明确实现结果类型。 |

## 240 文件批次

本批从 4,140 条 / 240 文件降至 3,920 条 / 228 文件，净降 220 条。以下 12 个生产文件已清零并纳入严格清单，共消除 130 条；另有 90 条在尚未清零的文件中真实下降。对这些未清零文件不宣称严格通过。

| 清零文件 | 原错误数 | 合同依据 |
| --- | ---: | --- |
| `app/agent/tools/impl/mcp.py` | 7 | 明确 MCP 工具的异步调用参数、流回调和上下文形状。 |
| `app/application/messaging/plugin.py` | 9 | 插件输入会话索引只在键存在时删除；附件参数使用既有消息模型。 |
| `app/chain/interaction.py` | 1 | `TorrentHelper` 构造器补全真实的 `None` 返回注解，调用点随之清零。 |
| `app/chain/transfer/facade.py` | 6 | 保留可替换构造点，明确整理门面的返回类型。 |
| `app/chain/transfer/history.py` | 12 | 改从模型定义模块导入，并补充可空调用参数与无返回值签名。 |
| `app/chain/transfer/scrape.py` | 7 | 改从模型定义模块导入，明确批次回调的无返回值合同。 |
| `app/domain/meta/metaanime.py` | 8 | 动漫解析按基类现有归一化处理空副标题，保留季号解析结果。 |
| `app/domain/meta/metavideo.py` | 8 | 影视解析沿用基类副标题归一化，并准确标注 token 序列及返回值。 |
| `app/modules/qqbot/gateway.py` | 5 | WebSocket 回调按真实消息、关闭码和连接对象标注。 |
| `app/modules/themoviedb/tmdbv3api/as_obj.py` | 41 | 属性包装对象的列表/字典视图使用具体容器类型；保留空集合整数下标异常。 |
| `app/runtime/compat/imports.py` | 20 | 精确标注兼容导入 Loader、Finder 和动态模块导出的既有合同。 |
| `app/schemas/types.py` | 6 | 枚举动态成员仅在确属枚举实例时返回，Schema 输出保持原有字段。 |

### 剩余文件与设计边界

以下路径均相对 `app/`；花括号表示逐一展开的文件名，数量对应 228 个当前仍有诊断的文件。清单由全宿主扫描逐文件核对，不把外部类型桩、动态 SDK、可空值或未定义的失败分支当作可用 `Any`、`ignore` 或不实 `cast` 清除的错误。

| 责任域 | 文件数 / 错误数 | 仍有错误的路径 | 下一步设计或外部条件 |
| --- | ---: | --- | --- |
| Adapters | 7 / 199 | `adapters/cache/{backends,redis}.py`；`adapters/network/{browser,doh}.py`；`adapters/system/{fsproxy,host,rust}.py` | 同步/异步缓存父类与 Redis 客户端合同；浏览器/DoH 补丁和 OS、Rust、文件系统边界。 |
| Agent | 34 / 351 | `agent/llm/{capability,helper}.py`；`agent/{mcp,orchestrator,runtime}.py`；`agent/memory/__init__.py`；`agent/middleware/{config,jobs,memory,patching,selection,skills,subagents,summarization,usage}.py`；`agent/prompt/__init__.py`；`agent/skills/{metadata,registry}.py`；`agent/tools/{base,factory,manager}.py`；`agent/tools/impl/{apply_patch,ask_user_choice,browse_webpage,edit_file,execute_command,query_doctor_report,read_file,recognize_captcha,search_web,send_local_file,send_message,send_voice_message,write_file}.py` | LangChain/Pydantic 类型导入、PyYAML 类型桩、工厂与工具覆写合同、SSE 会话和可空用户身份。 |
| API | 22 / 279 | `api/dependencies/{history,workflow}.py`；`api/endpoints/{bangumi,dashboard,discover,download,history,media,mediaserver,message,mfa,music,openai,plugin,search,site,subscribe,system,tmdb,transfer}.py`；`api/{servarr,servcookie}.py` | 分页参数的 OpenAPI 可空合同、动态载荷/响应模型、OpenAI 流协议及媒体缺失分支。 |
| Application | 11 / 90 | `application/{directory,module,rss}.py`；`application/messaging/interaction/__init__.py`；`application/messaging/{site,subscribe}.py`；`application/plugin/management.py`；`application/security/{cookie,url}.py`；`application/site/sites.pyi`；`application/torrent/download.py` | 兼容构造器返回类型、外部 Cython 站点 ABI、动态插件合同、可空路径与种子结果。 |
| Chain | 28 / 683 | `chain/{_interaction,_messaging,_music,anilist,bangumi,base,douban,mediaserver,message,scraping,site,storage,tmdb,torrents,workflow}.py`；`chain/subscribe/{completion,create,facade,match,query,refresh}.py`；`chain/transfer/{execution,queue,records,request,retry,settlement,workflow}.py` | 动态模块派发、mixin 宿主成员、可空媒体/路径/订阅状态和整理队列生命周期。 |
| Domain | 6 / 181 | `domain/{context,metainfo}.py`；`domain/meta/{metabase,metamusic,runtime,words}.py` | Canonical 字段、继承签名、Rust 多形状结果及识别词原始配置合同。 |
| Foundation | 1 / 6 | `foundation/temporal.py` | dateparser/dateutil 类型桩及字符串时长算术合同。 |
| Modules | 62 / 1423 | `modules/filemanager/{module,transhandler}.py`；`modules/filemanager/storages/{__init__,alipan,alist,local,rclone,smb,u115}.py`；`modules/indexer/__init__.py`；`modules/indexer/parser/sunnypt.py`；`modules/indexer/spider/{haidan,hddolby,mtorrent,rousi,sunnypt,tnode,torrentleech,yema}.py`；`modules/_base/{mediaserver,notification}.py`；`modules/{anilist,bangumi,discord,douban,emby,fanart,filter,jellyfin,listenbrainz,lrclib,plex,rtorrent,subtitle,theaudiodb,thetvdb,vocechat,webpush,zspace}/__init__.py`；`modules/feishu/module.py`；`modules/imdb/{__init__,api}.py`；`modules/musicbrainz/{__init__,cache}.py`；`modules/navidrome/{__init__,navidrome}.py`；`modules/qqbot/{module,qqbot}.py`；`modules/slack/{__init__,slack}.py`；`modules/telegram/{module,telegram}.py`；`modules/themoviedb/{cache,scraper}.py`；`modules/trimemedia/{api,module}.py`；`modules/ugreen/module.py`；`modules/wechat/{__init__,wechat,wechatbot}.py`；`modules/wechatclawbot/{__init__,wechatclawbot}.py` | 存储多实现 ABI、站点缺失字段策略、外部响应/缓存装饰器和消息渠道生命周期。 |
| Monitor | 3 / 29 | `monitor/{monitor,poller,snapshot}.py` | 快照 JSON、空路径、调度器可空及失败通知语义。 |
| Runtime | 7 / 176 | `runtime/{cache,config,events,scheduling}.py`；`runtime/extensions/module/manager.py`；`runtime/extensions/plugin/{manager,projection}.py` | 缓存、事件载荷、Pydantic 配置、pytz 类型桩及插件生命周期。 |
| Schemas | 32 / 353 | `schemas/{agent,cache,category,common,context,dashboard,download,event,file,history,llm,mcp,media,mediaserver,message,mfa,monitoring,music,openai,plugin,response,servarr,servcookie,site,storage,subscribe,system,tmdb,token,transfer,user,workflow}.py` | 318 条 `misc` 源于 Pydantic 基类在全宿主导入策略中被视为 `Any`；还需处理可空 URI、Mixin 宿主字段与覆写。 |
| Startup | 5 / 43 | `startup/composition/cache.py`；`startup/initializers/{command,modules,plugins}.py`；`startup/lifecycle/__init__.py` | 下游构造器、模块启动顺序、插件动态钩子和关闭回调。 |
| Testing | 2 / 15 | `testing/{bootstrap,network}.py` | 测试引导的进程状态与网络边界合同。 |
| Workflow | 5 / 51 | `workflow/__init__.py`；`workflow/actions/{fetch_medias,fetch_rss,filter_torrents,scrape_file}.py` | 节点运行时上下文、资源过滤和可空媒体/文件结果。 |
| 入口与诊断 | 3 / 41 | `command.py`；`main.py`；`doctor/checks.py` | 命令/启动返回值与外部诊断类型。 |

Pydantic 的定向 `follow_imports=silent` 曾在全宿主实验中把诊断扩散到其他文件，因此需要单独设计并按全宿主错误码验证，不能直接修改配置压掉 318 条。`app/agent/skills/metadata.py` 与 `app/agent/runtime.py` 的 PyYAML 类型桩属于依赖/锁文件工作；`app/application/site/sites.pyi` 需与实际资源 ABI 和测试替身一起核对。文件管理存储、`chain/base.py`、整理链、启动初始化器和 Agent 工厂均须先明确跨模块业务或生命周期合同。

## 需单独设计或外部条件

| 文件 | 阻碍 |
| --- | --- |
| `app/agent/skills/metadata.py`、`app/agent/runtime.py` | PyYAML 缺类型桩；需按依赖与锁文件流程处理，不能局部抑制 `import-untyped`。 |
| `app/agent/middleware/config.py`、`app/agent/middleware/patching.py`、`app/agent/middleware/usage.py` | LangChain 基类被当前导入策略视为 `Any`；调整定向导入策略后须重新审查全宿主诊断和实际状态类型。 |
| `app/agent/tools/base.py`、`app/agent/tools/manager.py` | 外部 BaseTool 类型与动态工厂返回合同尚未统一；收紧工厂会在编排器新增参数错误。 |
| `app/modules/indexer/parser/sunnypt.py` | 外部资料和消息中的畸形日期值处理缺明确合同，收窄为字符串可能改变既有容错。 |
| `app/workflow/actions/filter_torrents.py` | 构造器已补注解；剩余可空上下文需先确定行为合同。 |
