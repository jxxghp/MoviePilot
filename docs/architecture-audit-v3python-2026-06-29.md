# v3-python 对抗性多维架构审计报告

> **审计范围**:v3-python @ 62eecb38(MoviePilot Python 重写主线,~180K 行,17 子系统)  
> **审计日期**:2026-06-29  
> **方法**:多 agent workflow 对抗性审计 —— 11 子系统 × 4 维度并行发现 → 每条发现对抗性证伪(注入 by-design / 已修 / 已判不可行三张清单)→ 综合排序  
> **规模**:71 个 agent / 约 489 万 token / 30 分钟  
> **本轮基线**:此报告产出于 P0(安全)/ P1(技术债)/ P2(结构治理)三轮整改**已合入 v3-python 主线之后**(merge `62eecb38`),已修项不再重复上报。

---

## 一、执行摘要

整体健康度:中等偏下。功能正确性基本可用,但存在多处"静默失效"与文件系统/反序列化的信任边界缺口。已抽样逐行复核的 6 个最高权重项均与 file:line 一致、无臆测:scheduler.py:724 提交协程后丢弃 Future 并立即执行 :751 __finish_job;runtime.py:734-735 对绝对路径与未做边界校验的相对路径直接返回;plugin.py:1725-1735 构造 dest_path 无 is_relative_to 守卫;subscribe.py:988 在 :981 acquire 超时返回 False 后仍无条件全量执行,且 :1010-1013 在持锁期内逐订阅 sleep 60-300s;monitor.py:794-798 以 trigger='interval' 字符串+minutes kwarg 误用 modify_job;chain/__init__.py:1280 的 break 跳过后续 admin action。

真伪比例评价:对抗复核共处理 58 条,证伪/by-design/已修剔除 6 条,留存 52 条(真阳率约 90%),信噪比高,对抗过滤已剔除误报。其中 35 条 confirmed 为当前代码确凿缺陷;17 条 needs-scoping 并非"伪",而是"真代码缺陷但触发依赖部署配置"(Redis 无认证、未配 APP_DOMAIN、多 Bot 配置、可写缓存后端),应按"先确认触发条件再修"处理,而非降权忽略。

最值得优先做的三件事:(1) 统一收口三类文件系统/反序列化信任边界(Zip Slip + clone suffix + runtime 路径 + FileBackend key + pickle.loads),其中 Zip Slip 可覆写 app/main.py 直达 RCE,实际危害高于其 medium 定级;(2) 修复 scheduler 协程错误黑洞(async Job 失败完全不可观测且可重入并发)与 subscribe 锁超时后无锁续跑(双重下载 + episode_priority 状态损坏)这两条最危险的并发正确性缺陷;(3) 修 post_message 的 else/break——在 notify_action="user,admin" 且系统通知无 username 场景下,本应仅发管理员的通知被广播到全部公开渠道,属隐私泄漏。架构债(SubscribeChain 3413 行上帝类、post_message 同异步 230 行复制、TransferChain name-mangling 跨类访问)真实但应排在并发/安全修复之后,避免在已知竞态之上做大重构。

## 二、审计方法与可信度

- **发现总量**:58 条 → 经对抗性证伪后:**confirmed 35 / needs-scoping 17 / refuted 6**
- **对抗过滤**:每条发现由独立 opus 审查员**默认证伪**复核,强制复读 file:line 证据,并比对三张清单——
  - *by-design / 不可达*(双锁防死锁、`raise_exception` 透传、字符串分发契约、惰性 import、单进程 `Server.run()`、`WeakSingleton` 已带锁、复用机制内建等)
  - *已修*(P0:CORS凭证/XFF反代真实IP/统一限流/服务token缓存;P1:插件锁原子替换/热重载内聚/WAL连接级/原子会话/`recognize_media` raise_exception;P2:分发内核统一/message.py 拆分/ChainBase 拆 6 Mixin)
  - *已判不可行*(P2 #9 声明式作业表 / #38 通知扩展点登记表,行为保持下不可做)
- **真阳率约 90%**,信噪比高;`needs-scoping` 不是伪报,而是**真缺陷但触发依赖部署配置**(Redis 无认证 / 未配 APP_DOMAIN / 多 Bot / 可写缓存后端),处置原则为"先确认触发条件再修"。

**Confirmed 缺陷分布**

| 维度 | severity | 子系统 |
|---|---|---|
| 方法 12 · 架构 9 · 调用逻辑 8 · 边缘漏洞 6 | high 2 · medium 14 · low 19 | chain-transfer 5 · plugin-system 5 · agent-ai 5 · chain-subsearch 4 · chain-message-media 4 · entry-lifecycle 3 · modules-contract 3 · chain-core 2 · db 2 · api-auth 2 |

---

## 三、架构与调用逻辑(当前代码实测)

### 1. 进程引导 → 组合根/lifespan → service_registry 句柄持有与逆序关闭

**进程引导（`app/main.py`）**

`main.py` 以 `__name__ == '__main__'` 为入口：注册 SIGTERM/SIGINT 信号处理器 → 调 `init_db()` / `update_db()` 初始化数据库（SQLite WAL 模式，连接级 `@event.listens_for(connect)` 设置）→ 创建 `uvicorn.Server` 实例（`main.py:40`），`workers=cpu*2+1` 参数在单进程 FastAPI 下不生效（by-design，无真正多 worker）→ 调 `Server.run()` 阻塞主线程。

**组合根/lifespan（`app/startup/lifecycle.py`）**

`app/factory.py:create_app()` 构造 `FastAPI` 实例，将 `lifespan` 参数指向 `lifecycle.py:lifespan` 这个 `@asynccontextmanager` 协程（`lifecycle.py:64`）。FastAPI 启动时进入 `async with lifespan(app)` 的 `yield` 之前部分，依次执行：

1. `global_vars.set_loop(asyncio.get_event_loop())` — 保存主事件循环引用，供 `Scheduler.start()` 的 `run_coroutine_threadsafe` 跨线程提交协程（`lifecycle.py:70`）
2. 注入 `set_meta_config_provider` / `set_auth_level_provider` / `set_plugin_install_reporter` 三个函数级依赖
3. `init_routers(app)` — 动态挂载路由
4. `init_modules()` — 进入组合根（见下节）
5. （非安全模式）依次 `SystemChain().restore_plugins()` → `init_plugins()` → `init_scheduler()` → `init_monitor()` → `init_command()` → `init_workflow()`
6. `asyncio.create_task(init_extra())` — 后台异步同步插件到本地

关闭时（`yield` 之后，`lifecycle.py:101`）逆序关闭：backup_plugins → stop_workflow → stop_command → stop_monitor → stop_scheduler → stop_plugins → `await stop_modules()`。`await aclose_shared_async_transports()` 最后释放 HTTP 连接池。

**service_registry（`app/startup/service_registry.py`）**

`ServiceRegistry` 是纯字典封装，无外部依赖的叶子模块（`service_registry.py:16`）。`init_modules()`（`modules_initializer.py:141`）调用 `service_registry.clear()` 重置后，按以下顺序注册显式句柄：

- `"display"` → `DisplayHelper()` 实例（虚拟显示）
- `"module_manager"` → `ModuleManager()` 实例
- `"event_manager"` → `EventManager()` 实例，注册后立即调 `.start()` 启动消费者线程

`stop_modules()`（`modules_initializer.py:111`）通过 `service_registry.get("module_manager")` / `get("event_manager")` 取回同一实例显式关闭，而不重新构造单例。这是 P2 审计 #S6 DI 改造：明确组合根拥有生命周期，逐步去除"stop 处重新 `X()` 取单例"的隐式反模式。

---

### 2. 四类异步入口各自触发机制

**A. Scheduler（`app/scheduler.py:247`）**

`Scheduler`（`SingletonClass` 单例，`scheduler.py:247`）在 `init()` 中构造 APScheduler `BackgroundScheduler`（线程池执行器），向其注册所有定时 job，统一回调 `self.start(job_id=...)`。`start()` 方法（`scheduler.py:698`）：
- 通过 `__prepare_job()` 用 `self._lock`（RLock）做 running 标志的 CAS 防重入
- 判断 func 是否协程：若是，则 `asyncio.run_coroutine_threadsafe(func(...), global_vars.loop)` 提交到主事件循环；否则直接线程池调用
- APScheduler 线程池（`scheduler.py:416`，`executors={"default": ThreadPoolExecutor(...)}` ）与外层 `lock`（模块级 `Lock`，`scheduler.py:48`）在 `stop()` 时配合防止 `shutdown(wait=True)` 死锁（by-design 双锁设计）

插件定时任务通过 `init_plugin_jobs()` → `update_plugin_job(pid)` 从 `PluginManager().get_plugin_services(pid)` 取 service 列表后注册为独立 job，job_id 为 `"{pid}_{service_id}"` 格式（`scheduler.py:909`）。

**B. Command（`app/command.py:43`）**

`Command`（`Singleton` 单例，`command.py:43`）管理三类命令：内建 `_preset_commands`、插件 `_plugin_commands`（从 `PluginManager().get_plugin_commands()` 汇聚）、其他 `_other_commands`。

触发路径：消息模块解析出用户指令 → 向 EventManager 发 `EventType.CommandExcute` 事件 → `Command.command_event()` 处理器（`command.py:433`，被 `@eventmanager.register` 装饰）分发给 `execute()` → `__run_command()`，分两支：
- type=scheduler：直接调 `Scheduler().start(job_id=...)`
- type=func：按参数个数调 `command["func"](...)`

命令初始化通过 `ThreadHelper().submit(__init_commands_background)` 异步在线程池中完成（`command.py:161`），触发 `ChainEventType.CommandRegister` 链式事件允许插件拦截修改命令集，最终调 `CommandChain().register_commands()` 广播注册到各通知模块。

**C. Monitor（`app/monitor.py:228`）**

`Monitor`（`SingletonClass` 单例）在 `init()` 中：
- 为每个本地监控目录创建 `LocalDirectoryWatcher` 实例，启动 daemon 线程调 `watchfiles.watch()` 循环（`monitor.py:141`），文件变化（仅 `Change.added` / `Change.modified`）触发 `callback.event_handler()` 回调
- 同时创建第二个 `BackgroundScheduler`（`self._scheduler`）用于云盘/远程存储的轮询快照模式，定时执行 `__handle_file()` 有状态扫描

`event_handler()` 在经过 TTL 缓存去重（10s 窗口，`monitor.py:243`）后，调 `TransferChain().process()` 执行文件整理链路。

**D. EventManager（`app/core/event/manager.py`）**

`EventManager`（`Singleton` 单例，`manager.py:29`）持有两类订阅者：
- `__broadcast_subscribers`：`{EventType → {handler_id → Callable}}`，通过 `PriorityQueue` 异步消费（消费者线程数 `MIN_EVENT_CONSUMER_THREADS`，`manager.py:60`）
- `__chain_subscribers`：`{ChainEventType → {handler_id → (priority, Callable)}}`，按优先级同步顺序调用（`manager.py:334`）

`send_event(etype, data)` 路由：`EventType` → `__trigger_broadcast_event` 入队；`ChainEventType` → `__trigger_chain_event` 同步链式调用（`manager.py:108`）。

广播分发（`__dispatch_broadcast_event`，`manager.py:396`）：锁内快照订阅者列表（COW 防并发改字典），为每个 handler 浅拷贝独立 event_data，若 handler 是协程则 `run_coroutine_threadsafe` 提交主循环，否则 `ThreadHelper().submit()` 线程池执行。消费者 loop 内兜底捕获异常防止消费者线程崩溃（`manager.py:659`）。

handler 调度时（`__invoke_handler_by_type_sync`，`manager.py:453`）先查 `plugin_manager.get_plugin_ids()` 再查 `module_manager.get_module_ids()`，均通过 `class_name, method_name = handler.__qualname__.split(".")` 解析（`manager.py:535`），取运行实例的对应方法直接调用。

---

### 3. 两条调用内核的关系与差异

#### #60 统一后：`app/core/dispatch.py`

P2 审计 #60 已将分发内核抽到独立模块（`app/core/dispatch.py`）。核心函数是 `execute_modules` / `async_execute_modules`（`dispatch.py:28`/`76`），接受后端三元组序列 `Entry = (ident, name, func)` 和合并控制参数：

**合并规则（dispatch.py）**：
- result 为空（None/全 None 元组）→ 调 `func(*args, **kwargs)` 取结果
- `pipeline=True` 且 `check_signature(func, result)` 匹配 → `func(result)` 管道精化（将前一结果注入下一后端首参）
- result 为 list → `func(*args, **kwargs)` 结果 extend
- result 非空标量 → break 短路
- `RateLimitExceededException` → `on_rate_limit` 回调安静跳过；其他异常 → `on_error` 回调隔离（两者均可在 `raise_exception=True` 时透传上抛）

#### ChainBase.run_module（广播内核）

`ChainBase.run_module(method, *args, **kwargs)`（`chain/__init__.py:1826`）：

1. **插件钩子面**（`__plugin_entries`，`chain/__init__.py:1721`）：从 `PluginManager().get_plugin_modules()` 取各插件 `get_module()` 注册的方法字典，委托 `dispatch.execute_modules(..., pipeline=False, ...)`。插件钩子面 `pipeline=False`，不做参数精化，直接转发 kwargs，各插件独立响应。
2. **若插件返回非空非列表 → 直接返回**，不再进入系统后端面。
3. **系统后端面**（`__system_entries`，`chain/__init__.py:1734`）：从 `modulemanager.get_running_modules(method)` 取实现该方法的模块，按 `get_priority()` 升序排列（优先级数字小者先执行），委托 `dispatch.execute_modules(..., pipeline=True, ...)`。系统后端面 `pipeline=True`，支持管道精化（上一模块输出作为下一模块输入，适用于识别增强链路）。

`async_run_module` 对应异步版本，同步后端经 `run_in_threadpool` 执行避免阻塞事件循环。

#### Managers 门面分发（单一域，单步分发）

各域门面（`DownloaderManager` / `MediaServerManager` / `NotificationManager` / `MediaRecognizeManager` / `StorageManager`，`app/managers/` 下各文件，基类 `ManagerBase`，`managers/base.py:14`）持有 `ModuleManager()` 引用，同样委托 `dispatch.execute_modules` 但只遍历该域的运行后端：

- 下载器/媒体服务器：插件下载器/媒服通过 `provides_*` 接口注册为运行模块（已纳入 `get_running_modules`），直接走系统后端面，使用 `_dispatch()` 单步分发（`ManagerBase`）
- 通知/存储/媒体识别：支持插件钩子面 + 系统后端面两步分发（`PluginDispatchManager` 子类模式）

门面 Managers 与 `ChainBase.run_module` 的本质差异：
- `run_module`：全量广播（先插件面、后系统面，两步），方法名字符串动态分发，不带领域过滤
- 门面 Managers：领域内聚（仅遍历本域后端），ChainBase 某些方法现在直接调门面（如 `obtain_images` 调 `mediarecognizemanager.obtain_images()`，`chain/__init__.py:460`，注释标注"等价于 v2 run_module 但已废弃"），门面 pop 时不透传 `raise_exception`（by-design 契约差异，SystemChain 透传，门面 Managers 不透传）

---

### 4. modules（内建）/ plugins（扩展）发现、注册、按方法名分发

**内建 Modules**

`ModuleManager`（`app/core/module/manager.py`，从 `app/core/module/__init__.py` 懒加载）在 `init_modules()` 时由组合根构造并 `service_registry.register("module_manager", ModuleManager())`。`ModuleManager` 扫描 `app/modules/` 下的 Python 包，动态 `importlib.import_module` 加载，实例化后按 `init_setting()` 返回的开关名/值判断是否启用，启用的模块加入运行模块列表。模块须继承 `_ModuleBase`（`modules/__init__.py:28`），实现 `init_module()` / `init_setting()` / `get_name()` / `get_type()` / `get_priority()` 契约。

`get_running_modules(method)` 遍历运行模块列表，过滤出 `hasattr(module, method)` 的模块返回，`ChainBase.__system_entries()` 随即按 `get_priority()` 升序排列（`chain/__init__.py:1738`）。

**插件 Plugins**

`PluginManager`（`app/helper/plugin_manager.py`，`Singleton` 单例）负责：
- `start()`：扫描插件目录（本地安装 + 在线市场），`importlib.import_module` 动态加载，实例化并调 `init_plugin(config)` 初始化，加入 `running_plugins` 字典
- `get_plugin_modules()`：遍历 `running_plugins`，调每个插件实例的 `get_module()` 方法取其暴露的方法字典 `{method_name → callable}`，返回 `{(plugin_id, plugin_name) → method_dict}`，供 `ChainBase.__plugin_entries()` 遍历（`chain/__init__.py:1725`）
- `get_plugin_services(pid)`：取插件定时任务列表（`func`, `trigger`, `id` 等），由 `Scheduler.update_plugin_job()` 注册为 APScheduler job
- `get_plugin_commands()`：取插件命令列表，由 `Command.__build_plugin_commands()` 汇聚
- 热重载：`_plugins_lock`（RLock）锁内原子整体替换 `_plugins` 字典后再广播（P1 #37 已修复）

EventManager 中 handler 的插件识别：`__parse_handler_names` 取 `handler.__qualname__.split(".")` 得 `(class_name, method_name)`，`class_name in plugin_manager.get_plugin_ids()` 即认定是插件 handler，从 `running_plugins.get(class_name)` 取 live 实例反射调用（`manager.py:467`）。`@eventmanager.register(EventType.xxx)` 装饰器在 import 期（模块加载时）即向 `EventManager` 单例（`eventmanager = EventManager()`，`manager.py:728`）注册 handler，插件 `stop()` 前通过 `eventmanager.remove_event_listener()` 显式注销。

---

### 5. 端到端样例调用链：用户下载请求 → chain → module → 落库 → 事件

以 Telegram 用户发送 `/subscribes` 触发订阅搜索为例：

```
[用户消息] Telegram Bot 收到消息
    → app/modules/telegram/telegram.py: message_parser()
        返回 CommingMessage{channel, text="/subscribes", userid}
    → app/chain/message.py: MessageChain.process()
        → ChainBase.run_module("message_parser", ...)
            先插件钩子面：无插件拦截
            后系统后端面：TelegramModule.message_parser() 返回 CommingMessage
        → 识别为命令 "/subscribes"
        → eventmanager.send_event(EventType.CommandExcute, {"cmd": "/subscribes", "user": userid, ...})

[EventManager 消费者线程]
    → broadcast_queue 取出事件
    → __dispatch_broadcast_event → Command.command_event() 处理器（@eventmanager.register）
        → Command.execute("/subscribes", ...)
        → __run_command → command["func"] = SubscribeChain().remote_list(channel, userid, source)

[SubscribeChain.remote_list]
    → self.run_module("search_torrents", ...)  [搜索种子]
        → 插件钩子面：无
        → 系统后端面：SiteModule.search_torrents() → 返回 List[TorrentInfo]
    → DownloadChain().download(content=torrent_file, ...)
        → self.downloadermanager.download(...)  [门面分发]
            → dispatch.execute_modules([QbittorrentModule.download, ...], ...)
            → QbittorrentModule.download() → 向 qBittorrent 添加任务 → 返回 (downloader, hash, layout, None)
    → DownloadHistOper().add(...)  [落库]
        → db session via SessionFactory()/atomic_session 写 download_history 表
    → eventmanager.send_event(EventType.DownloadAdded, {"hash": hash, "context": context})
        → 入广播队列 → 异步触发订阅更新状态、通知消息等下游 handler

[消息回复]
    → ChainBase.post_message(Notification{channel, userid, title="开始下载..."})
        → MessageTemplateHelper.render() 渲染模板
        → messagequeue.send_message("post_message", message, immediately=True)
            → NotificationManager._dispatch("post_message", ...)
                → dispatch.execute_modules([TelegramModule.post_message, ...])
                → TelegramModule.post_message() → Telegram Bot API 发送消息
```

关键 file:line 汇总：
- `app/main.py:40` — uvicorn.Server 构造（workers 参数不生效说明）
- `app/startup/lifecycle.py:64` — lifespan 组合根入口
- `app/startup/service_registry.py:16` — ServiceRegistry 句柄注册表
- `app/startup/modules_initializer.py:141` — init_modules，组合根注册顺序
- `app/startup/modules_initializer.py:111` — stop_modules，逆序关闭
- `app/scheduler.py:48` — 模块级 Lock（stop 防死锁）；`:277` — 实例 RLock（job running CAS）
- `app/scheduler.py:698` — Scheduler.start()，协程/同步/多进程三分支
- `app/command.py:433` — Command.command_event() @eventmanager.register 装饰
- `app/monitor.py:141` — LocalDirectoryWatcher._run_watch watchfiles 主循环
- `app/core/event/manager.py:29` — EventManager，双订阅者字典
- `app/core/event/manager.py:396` — __dispatch_broadcast_event COW + 线程池/协程提交
- `app/core/event/manager.py:453` — __invoke_handler_by_type_sync plugin→module→global 三段路由
- `app/core/dispatch.py:28` — execute_modules 分发内核（#60 统一落点）
- `app/chain/__init__.py:1826` — ChainBase.run_module，插件面优先 + 系统面 pipeline
- `app/chain/__init__.py:1602` — ChainBase 继承六 Mixin（#16 拆分结果）
- `app/chain/__init__.py:1632` — ChainBase.__init__ 8 个关键字注入依赖
- `app/managers/base.py:14` — ManagerBase，门面分发基类
- `app/modules/__init__.py:28` — _ModuleBase 契约基类，`DEFAULT_MODULE_PRIORITY=9999`

---

## 四、跨子系统共性主题

1. 信任边界缺失(文件系统 sink + 反序列化):用户/外部可控数据未经校验直达 open()/mkdir()/pickle.loads()。Zip Slip(plugin.py:1725/2742)、clone_plugin suffix(plugin_manager.py:1652)、runtime extra_context_files(runtime.py:734)、FileBackend key(cache.py:767)、pickle.loads(redis.py:65 / cache.py / chain/__init__.py:66) 同源——缺一个统一的 is_within(base,target) 守卫,以及'仅 JSON / HMAC 签名'的反序列化策略。

2. 锁边界过宽或锁外续跑:set() 把 DB I/O 包进 _rlock 阻塞事件循环(systemconfig_oper.py:36);search() 在持锁期逐订阅 time.sleep(60-300s)(subscribe.py:1010)使 7200s 超时成为必然,而超时后工作体仍无锁续跑(subscribe.py:979);check() 改 episode_priority 不进锁。锁的获取/失败/释放与临界区不对齐。

3. 错误被静默吞没、可观测性断层:scheduler 协程 Future 被丢弃(scheduler.py:724)使 async Job 异常无 log 无事件;_execute_agent 吞 CancelledError(agent/__init__.py:1392)致 stop 失效;modify_job 的 TypeError 被宽 except 吞成误导日志(monitor.py:801);[PLUGIN] ValueError 被 background task 静默丢(message.py:601);provider.py 三处 print(err) 绕过结构化日志。同步 Job 有完整 except 链,异步/边界路径却是黑洞。

4. 同步/异步双实现语义漂移 + 逐字节复制:post_message vs async_post_message(230 行复制,同一 break bug 存于两处)、async_recognize_help 用 falsy 而同步用 is not None(media.py:1657)、search_movies/async_search_movies 同缺 null title 守卫、set/async_set。双实现缺共享抽象,bug fix 易漏一侧导致行为分裂。

5. 类级/模块级可变状态在多实例间共享(单进程内,非多进程误报):Telegram 类级 dict 与 telebot.apihelper 模块全局(telegram.py:39-47/76-85)在多 Bot 配置下互相污染;MessageChain _user_sessions(message.py)、MemoryBackend 类级 _region_caches/_lock(cache.py:364)、DbManager 模块单例(manager.py)同型。ServiceBase 多配置实例化暴露此类'实例写穿透类属性'。

6. check-then-act / read-modify-write 非原子(GIL 字节码边界或锁间隙):register_plugin(manager.py:141)、clone_plugin 的 UserInstalledPlugins(plugin_manager.py:1699)、save_auth/clear_auth(provider.py:1385)、start() 双锁窗口(plugin_manager.py:112)、SiteOper.add 域名 TOCTOU(site.py:19)、check() 的 episode_priority、TransferResultProcessor is_finished+pop(transfer.py:1063)。单进程内复合操作非原子,导致更新丢失/僵尸实例/资源泄漏。

7. None/falsy 边界守卫缺失致崩溃或静默删配:_resolve_media_download_dir 返回裸 None 被 2-tuple 解包(download.py:129)、__re_transfer 对 None 调 .value(transfer.py:3221)、set() 用 if value 而非 is not None 误删合法 falsy 配置(systemconfig_oper.py:44)、SiteOper.update 不查 None(site_oper.py:74)、search_movies title 为 None(tmdbapi.py:59)、async_recognize_help 对 season=0 误判。边界处缺空值安全。

8. 上帝类/职责过载结构债:SubscribeChain 3413 行承载洗版/搜索/RSS/元数据/社交/日历≥8 域(subscribe.py:48-3413);TransferChain 拆分后 helper 类靠 _ClassName__method name-mangling 跨类访问私有 API(transfer.py:851 等 8 处),静态工具不可见、重构安全网失效;post_message 230 行同异步复制。

9. 部署配置依赖型安全风险(needs-scoping,非伪报):pickle RCE 依赖 Redis 无认证/可写后端(redis.py:65、cache.py:767、chain/__init__.py:66);SSO Host 头注入依赖未配 APP_DOMAIN(auth.py:344);Zip Slip 依赖恶意 PLUGIN_MARKET。应'先确认触发条件再修',并在配置模板/文档强制 requirepass 等加固。

---

## 五、Top 优先问题与修复方案

> 按 severity × 影响排序;每条含具体修复方案、风险/反噬点、工作量。

### #1 [🟡 MEDIUM] Release zip 解压 Zip Slip,可覆写主程序任意可写文件直达 RCE

- **子系统/位置**:`plugin-system` — `app/helper/plugin.py:1725-1735 (sync), 2742-2753 (async)`
- **维度**:边缘漏洞
- **问题**:zipfile.namelist() 可含 '../' 段;base_prefix 剥离只处理统一顶层前缀,对条目内 '../' 不限制。dest_path = dest_base / rel_path 后直接 open()/mkdir(),无 resolved().is_relative_to(dest_base) 校验。恶意插件市场仓库发布含路径穿越条目的 release zip 可写到 dest_base 之外(如 app/main.py)→ RCE。sync 与 async 两套实现同缺。
- **修复方案**:抽 util is_within(base, target):dest_resolved = dest_path.resolve(); base_resolved = dest_base.resolve(); 若 not dest_resolved.is_relative_to(base_resolved) 则 logger.warning 并 continue(跳过该条目)。Python<3.9 用 try: dest_resolved.relative_to(base_resolved) except ValueError。sync(1725-1735)与 async(2742-2753)两处同时修,并对目录条目 mkdir 前同样校验。
- **风险/反噬**:必须两处对称修复,漏一侧造成异步路径仍可被绕过;校验前需先 resolve(),注意符号链接目录的合法插件不被误拒(实际插件不应含 '..',风险低)。
- **工作量**:small

### #2 [🟠 HIGH] _resolve_relative_path 接受绝对路径与 .. 穿越,extra_context_files 可读任意系统文件经 LLM 泄露

- **子系统/位置**:`agent-ai` — `app/agent/runtime.py:733-735`
- **维度**:边缘漏洞
- **问题**:candidate.is_absolute() 时原样返回;相对分支 (root/candidate).resolve() 解析 '..' 后无 runtime_dir 边界校验。extra_context_files 存于 CURRENT_PERSONA.md frontmatter,is_admin 工具可改;加载内容注入每次 LLM 系统提示词。攻击者写入 /etc/passwd 或 DB 密钥绝对路径即可让服务可读文件经 LLM 输出泄露。
- **修复方案**:在 _resolve_relative_path/_resolve_optional_paths 内:对绝对路径直接拒绝(raise AgentRuntimeConfigError);相对路径 resolve 后强制 resolved.is_relative_to(root.resolve()),否则拒绝并记录。复用与 #1 同一 is_within 守卫。
- **风险/反噬**:需确认现有任何 persona 配置未合法依赖 runtime 目录外的绝对路径(如共享 bundled 资源);提供明确错误信息便于用户迁移配置。
- **工作量**:small

### #3 [🟠 HIGH] Scheduler.start() 丢弃协程 Future,__finish_job 提前执行致 async Job 可重入并发且异常全静默

- **子系统/位置**:`entry-lifecycle` — `app/scheduler.py:703-751`
- **维度**:调用逻辑
- **问题**:asyncio.run_coroutine_threadsafe 返回的 Future 在 :724 被丢弃,:751 __finish_job 立即把 running 置 False;下一次定时触发看到 running=False 允许与上次协程并发(agent_heartbeat 等);协程内异常存于被弃 Future,无 logger.error/无 MessageHelper/无 SystemError 事件,与同步 Job 的完整 except 链(733-749)黑白对比。
- **修复方案**:捕获 future = __start_coro(...),用 future.add_done_callback 在回调里:取 future.exception() 记 logger.error + MessageHelper.put + send_event(SystemError),并在回调内调用 __finish_job(job_id);协程分支不再走末尾同步 __finish_job(改为仅同步/多进程分支末尾 finish)。保持单进程事件循环 global_vars.loop 不变。
- **风险/反噬**:不要用 future.result() 阻塞调度线程(会拖死 APScheduler executor);必须走 done-callback 异步收口。注意 :48/:277 的 by-design 双锁不可动。回调里 send_event 需避免与 #44 SystemError 处理器循环(见事件管理器无界广播项)叠加。
- **工作量**:medium

### #4 [🟡 MEDIUM] post_message/async_post_message 的 else/break 在 action='user' 无 username 时静默丢弃后续 admin,系统通知泄漏到公开渠道

- **子系统/位置**:`chain-core` — `app/chain/__init__.py:1276-1280 (sync), 1393-1397 (async)`
- **维度**:调用逻辑
- **问题**:action='user' 但 dispatch_message.username 为 None(系统级通知)时落入 else,设 send_orignal=True 并 break,后续 'admin' action 永不执行,最终广播原始消息到所有渠道。notify_action='user,admin' 是最常用配置且下载完成/整理完成等后台事件无 username,本应仅推管理员的通知被广播至 Telegram/企业微信/公共 Bot。
- **修复方案**:将 'user' 且 username 为空的分支由 break 改为 continue(让后续 admin 正常执行),仅在明确全量标识(如 'all')时才 send_orignal=True+break;else 分支补 fallback 日志。先在 sync 与 async 两处同步修 break,加单测断言 user,admin+无 username 时只发管理员、不广播,再做 #14 的共享抽象。
- **风险/反噬**:改 break→continue 会改变路由,必须保留真正需要全量广播场景的原行为;务必同改两处避免同异步分裂;补测试约束双路径一致。
- **工作量**:small

### #5 [🟡 MEDIUM] search()/match() 锁 acquire 超时后无锁续跑全量工作体,破坏互斥致双重下载与状态损坏

- **子系统/位置**:`chain-subsearch` — `app/chain/subscribe.py:979-987 (search), 1353-1361 (match)`
- **维度**:方法
- **问题**:工作主体(订阅查询/搜索/下载/状态更新)写在 if/else 之外无条件执行;_rlock.acquire(timeout=7200) 超时返回 False 时仅 warn 后继续全量执行,与持锁的另一线程并发:同一订阅重复处理→重复下载;update_subscribe_priority 对同一 subscribe.id 并发写 episode_priority 后写覆盖先写。
- **修复方案**:在 lock_acquired=False 的 else 分支末尾立即 return(跳过本轮),或将整个工作体移入 if lock_acquired: 分支内。finally 已用 lock_acquired 守卫 release,不会误放未持有的锁。match() 同构修复。
- **风险/反噬**:跳过一轮调度可接受(下一周期会补);需确认 finally 释放逻辑只在 lock_acquired=True 时 release(现已具备)。与 #6 联动:必须同时把 sleep 移出锁,否则超时仍频发。
- **工作量**:trivial

### #6 [🟡 MEDIUM] search() 在持有 _rlock 期内逐订阅 time.sleep(60-300s),使锁超时成为必然并长期封锁 match()

- **子系统/位置**:`chain-subsearch` — `app/chain/subscribe.py:1010-1013`
- **维度**:方法
- **问题**:for 循环体内、持 _rlock 之中,每个 R/P 订阅阻塞 60-300 秒,N 个订阅累计 60N-300N 秒持锁睡眠,N>=24 即突破 7200s 触发 #5 的无锁并发;整个周期 match() 被完全封锁,RSS 缓存匹配无法运行。
- **修复方案**:采用'释放-睡眠-重新获取'模式:在 sleep 前 release _rlock,sleep 后重新 acquire 并重读 subscribe 最新状态;或将 sleep 移至循环外/搜索动作之间的非持锁段。修复后 #5 的超时概率大幅下降。
- **风险/反噬**:重新获取锁后必须重读 DB 快照(subscribe 可能已被 check() 改动),否则引入新的陈旧写;reorder 会改变搜索节奏,需保留随机退避语义。
- **工作量**:small

### #7 [🟡 MEDIUM] monitor polling_observer 误用 modify_job(trigger='interval' 字符串),动态间隔调整必抛 TypeError 永久失效

- **子系统/位置**:`entry-lifecycle` — `app/monitor.py:791-799`
- **维度**:调用逻辑
- **问题**:modify_job 把 **changes 转发给 Job._modify,trigger 必须是 BaseTrigger 实例而非字符串;传 trigger='interval' 抛 TypeError,minutes 也非合法 Job 属性抛 AttributeError,均被外层 try/except(801-803)吞成误导性'轮询监控出现错误'日志,间隔始终不变。
- **修复方案**:改用 self._scheduler.reschedule_job(f'monitor_{storage}', trigger='interval', minutes=new_interval)——这是 APScheduler 3.x 修改触发器的正确入口。
- **风险/反噬**:reschedule_job 会替换触发器并重算 next_run_time,需验证作业存在(current_job 已判空);确认大型远端存储降频后不会因 next_run_time 重置导致一次空窗。
- **工作量**:trivial

### #8 [🟡 MEDIUM] polling_observer 在所有存储快照均失败时把空快照写入持久缓存,下次轮询全量重处理

- **子系统/位置**:`entry-lifecycle` — `app/monitor.py:731-787`
- **维度**:方法
- **问题**:(needs-scoping)所有路径 snapshot_storage 返回 None(网络抖动/权限)时 new_snapshot 保持 {},save_snapshot 覆盖上次有效快照;下次 old_snapshot={} 而 new_snapshot 含实际文件,compare_snapshots 把全部文件判为新增→存量文件全量重整理,严重时覆盖已整理目标。
- **修复方案**:保存前判断 if not any(snapshot is not None for ...):跳过覆盖,保留旧快照;或引入 partial_failure 标志,仅当至少一路成功才覆盖持久快照。
- **风险/反噬**:需区分'全失败'与'真的全空目录'两种 {};建议以'是否有任一路径返回非 None'为判据而非 file_count==0,避免合法空目录被永久冻结快照。
- **工作量**:small

### #9 [🟡 MEDIUM] systemconfig set() 在 _rlock 内执行 DB I/O,async_set 并发时阻塞事件循环

- **子系统/位置**:`db` — `app/db/systemconfig_oper.py:36-53`
- **维度**:方法
- **问题**:_rlock 本为保护内存 dict(微秒级),却把 1-3 次 DB I/O(可达数百毫秒)全包在 with self._rlock: 内;async_set 在事件循环线程也需短暂取 _rlock(仅缓存赋值),后台线程 set() 做 DB I/O 时事件循环线程在 with 处阻塞,整个 loop stall,FastAPI 表现为请求集体超时。
- **修复方案**:把 DB I/O 移出锁:先锁外读旧值,with self._rlock: 仅比较+更新内存缓存,释放锁后再写 DB(与 async_set 对称);或将 set() 异步化、DB I/O 入 executor 由 _alock 串行。
- **风险/反噬**:DB I/O 移出锁引入 check-then-write 窗口,需接受 DB 层 last-writer-wins(与 async_set 现状一致),并保证缓存与 DB 最终一致;勿改动 _alock/_rlock 既有职责划分。
- **工作量**:small

### #10 [🟡 MEDIUM] upload_avatar 在 async 端点内同步 file.file.read() 且无上传大小限制,可阻塞事件循环并 DoS

- **子系统/位置**:`api-auth` — `app/api/endpoints/user.py:116`
- **维度**:方法
- **问题**:file.file 是 SpooledTemporaryFile,超阈值时 file.file.read() 触发同步磁盘 I/O 阻塞 asyncio 事件循环;端点无 max_size,攻击者上传超大文件长时间独占 loop 并把超大 base64 写入 DB 致 OOM/存储膨胀,低成本 DoS。
- **修复方案**:改 content = await file.read();读取前用 Content-Length/中间件限制上传大小(头像 <=2MB),读取后再对 len(content) 应用层校验;超限返回 413。
- **风险/反噬**:await file.read() 仍整体载入内存,必须在 read 前先按 Content-Length 或流式分块拒绝超大请求才能真正防 OOM;注意与反代 client_max_body_size 协同。
- **工作量**:small

### #11 [🟡 MEDIUM] _execute_agent 吞 CancelledError 致 stop_current_task 失效、heartbeat 阻塞最多 60 秒

- **子系统/位置**:`agent-ai` — `app/agent/__init__.py:1392-1395, 1896-1910`
- **维度**:调用逻辑
- **问题**:asyncio cancel 只注入一次 CancelledError;_execute_agent 在 1392 catch 后正常 return,task 不被标 CANCELLED,_session_worker 继续进入 wait_for(queue.get(), timeout=60)。后果:stop_current_task 的 task.cancel()+await 因 CancelledError 已消耗最多等 60s;heartbeat_check_jobs 在 queue.join() 后直接 await worker 同样阻塞最多 60s。
- **修复方案**:_execute_agent 在 CancelledError 处理必要清理(stop_streaming/emit)后 raise 重新传播;heartbeat_check_jobs 在 queue.join() 后改调 clear_session(内部已含 cancel+await),删除直接 await worker 的 1901-1905 段。
- **风险/反噬**:re-raise 改变控制流,需确认无调用方依赖原'吞掉'语义;清理动作必须在 raise 前完成,避免泄漏流式连接。
- **工作量**:medium

### #12 [🟡 MEDIUM] save_auth/clear_auth 无锁 read-modify-write,并发 OAuth 完成互相覆盖 token

- **子系统/位置**:`agent-ai` — `app/agent/llm/provider.py:1385-1406`
- **维度**:方法
- **问题**:两段操作间无锁;两个不同 provider 的 OAuth 几乎同时完成,各自读旧 config、写自己的 auth,后写覆盖先写,先完成的 token 静默丢失。现有 _lock(RLock)只保护内存 _pending_sessions,不覆盖持久化写。
- **修复方案**:用 asyncio.Lock(async 上下文)包裹 read→modify→write 三步;或在 _read_agent_config/_write_agent_config 层引入乐观锁重试,或改用 SystemConfigOper 的原子 compare-and-set。
- **风险/反噬**:provider 方法为 async,务必用 asyncio.Lock 而非 threading,混用易死锁;与 #9 systemconfig 写路径若共享底层 SystemConfig 需统一原子策略避免互相覆盖。
- **工作量**:small

### #13 [🟡 MEDIUM] _validate_obtain_images_params 的 RECOGNIZE_SOURCE 哨兵语义反转,跳过 tmdb_id 守卫

- **子系统/位置**:`modules-contract` — `app/modules/themoviedb/__init__.py:960-969`
- **维度**:方法
- **问题**:约定'返回 None=继续抓图,返回 MediaInfo=提前退出',但 RECOGNIZE_SOURCE!='themoviedb' 时直接 return None,使调用方继续抓图,后续 tmdb_id 空检查与'图已完整'检查被全跳过:tmdb_id 为 None(Douban/Bangumi 结果)时仍以 None 调 get_movie_images,图已完整也额外发 TMDB 请求。
- **修复方案**:首个判断改 return mediainfo:if settings.RECOGNIZE_SOURCE != 'themoviedb': return mediainfo,与后续守卫语义一致(不需处理则返回 mediainfo 提前退出)。
- **风险/反噬**:改为提前返回会改变流程,需确认调用方对非 None 返回正确视为'已完成/跳过',不会误吞应抓图的 themoviedb 路径。
- **工作量**:trivial

### #14 [🟡 MEDIUM] Telegram 多实例共享类级可变 dict 与 telebot.apihelper 模块全局,多 Bot 配置状态互污染/端点静默走错

- **子系统/位置**:`modules-contract` — `app/modules/telegram/telegram.py:39-47, 76-85`
- **维度**:调用逻辑
- **问题**:类级 _typing_tasks/_user_chat_mapping/_callback_handlers 经'实例写穿透类属性'在多实例间共享:A 的 typing 任务出现在 B、user_id 映射被后者覆盖致回复走错 chat;__init__ 无条件覆盖 telebot.apihelper 的 API_URL/代理(模块级全局),后初始化的实例 last-write-wins,使用自定义中转端点的实例被静默切回官方 api.telegram.org(token 仍在,请求正常返回,极难察觉)。
- **修复方案**:在 __init__ 把各 dict 初始化为实例属性(self._callback_handlers={} 等);每个 TeleBot 通过 base_url=api_url 独立持有端点,避免改进程级 apihelper 全局;或 init_service 层拒绝多 Telegram 混用不兼容 URL 配置。
- **风险/反噬**:需确认 telebot 版本支持 TeleBot(base_url=...);类→实例 dict 迁移安全但要核对是否有依赖类级共享的历史逻辑;单 Bot 配置不受影响,改动不应回归单 Bot 行为。
- **工作量**:small

### #15 [⚪ LOW] clone_plugin suffix 无路径字符校验,可将插件文件树写入 plugins 目录外任意位置

- **子系统/位置**:`plugin-system` — `app/helper/plugin_manager.py:1652-1669`
- **维度**:边缘漏洞
- **问题**:suffix='/../../../tmp' 使 clone_plugin_dir 解析到 ROOT_PATH/tmp,shutil.copytree 递归复制并 modify_plugin_files 覆写 __init__.py,全程无 suffix 字符白名单。超级用户可把修改后的 Python 源码写到任意可写目录(污染 cron / 覆盖其他应用文件)。
- **修复方案**:入口校验 if not re.match(r'^[A-Za-z0-9_]+$', suffix): return False, '分身后缀含非法字符';与 #1/#2 复用同一 is_within 二次兜底。
- **风险/反噬**:正则需保证现有合法后缀仍匹配(确认无现存含连字符等的分身命名);仅做白名单不破坏现有数据。
- **工作量**:trivial

### #16 [⚪ LOW] pickle.loads 反序列化 Redis/文件缓存内容,后端可写时 RCE(部署条件型)

- **子系统/位置**:`core-infra-event` — `app/core/redis.py:65-75; app/core/cache.py:767; app/chain/__init__.py:66,79`
- **维度**:边缘漏洞
- **问题**:(needs-scoping)RedisHelper.deserialize/FileCache 直接 pickle.loads 不校验来源,cache key 固定可预测(__torrents_cache__ 等)。Redis 默认无认证(Docker 常见)或 TEMP_PATH 可写时,攻击者写入已知 key→下次 loads 以进程权限执行任意代码(CWE-502)。
- **修复方案**:首选改 JSON/msgpack 安全序列化(不可 JSON 化对象由调用方先转换);若须保留 pickle,set 时对字节追加 HMAC-SHA256(密钥取 settings.SECRET_KEY),get 时先验签再 loads;同时配置模板强制 Redis requirepass 并文档标注。FileBackend key 同步加 #1 的 is_within 守卫(cache.py:767/784/795/801 及 AsyncFileBackend:884)。
- **风险/反噬**:切换序列化格式会使既有缓存条目不可读,需在缓存 key 加版本前缀或部署时清缓存;json 无法序列化部分对象需保留兼容回退路径——这是其 needs-scoping 的原因。
- **工作量**:medium

### #17 [🟡 MEDIUM] SubscribeChain 3413 行上帝类 + TransferChain helper 经 name-mangling 跨类访问私有 API,结构债放大并发修复半径

- **子系统/位置**:`chain-subsearch` — `app/chain/subscribe.py:48-3413; app/chain/transfer.py:851,873,959,1098,1130-1134,1363`
- **维度**:架构
- **问题**:SubscribeChain 把洗版状态机/搜索调度/RSS 匹配/元数据刷新/社交同步/日历缓存≥8 域压进一类,共享 _rlock 但 check() 不遵守(#5/#6 类竞态难单独修);拆出的 TransferService/ScrapeBatchCoordinator/TransferResultProcessor 经 _TransferChain__method name-mangling 访问 TransferChain 私有方法,mypy/IDE 不可见,重命名即运行期 AttributeError,P2 拆分封装目标被反向耦合。
- **修复方案**:先把 8 个被跨类访问的私有方法提升为单下划线受保护或包内公开,并以 Protocol/ABC 声明契约使依赖可静态检查;再将 SubscribeChain 按域拆 BestVersionStateMachine / SubscribeSearchService(search/match) / SubscribeMetadataService(check/follow/cache_calendar),主类以组合调用。务必排在 #5/#6 并发修复落地之后。
- **风险/反噬**:大重构,必须保持 _rlock 语义不变且不引入新竞态,严禁在已知竞态之上做结构搬迁;name-mangling→受保护是机械改动但触及 8 处调用点,需全测试回归。注意 P2 已确认不可行项(#9 声明式作业表 / #38 通知扩展点登记表)勿复活。
- **工作量**:large

---

## 六、推进方案(Rollout)

分层与序列(尊重单进程架构与既有 by-design 边界:scheduler 双锁 :48/:277、字符串方法名分发、渠道能力双轨、惰性 import、ChainBase raise_exception 透传契约差异均不动;P0 安全/P1 技术债已修项不重复):

P0(安全/可观测黑洞,立即修,小改面):
- PR-A 路径与反序列化信任边界统一收口:新增 app/core/path_safety.py 的 is_within(base, target);应用到 Zip Slip(plugin.py:1725-1735 + 2742-2753,#1)、clone_plugin suffix 白名单(plugin_manager.py:1652,#15)、runtime extra_context_files(runtime.py:733-735 拒绝绝对+边界校验,#2)、FileBackend key(cache.py:767/784/795/801 + AsyncFileBackend:884,#16 的 file 部分)。验证:对 '../evil'、绝对路径、合法相对路径各写单测(RED→GREEN),断言越界被拒、正常写入不受影响。
- PR-B scheduler 协程错误收口(scheduler.py:703-751,#3):捕获 Future + add_done_callback 内 logger.error/MessageHelper/send_event(SystemError) 并 __finish_job;单测断言异常被记录且 running 在协程结束后才复位、不可重入。注意与事件管理器 SystemError 无界广播项联动(避免回调递归)。

P1(并发正确性 + 隐私/可用性,核心):
- PR-C subscribe 锁正确性(#5+#6 同 PR,强耦合):subscribe.py:979-987/1353-1361 超时即 return;1010-1013 的 sleep 改'释放-睡眠-重读-重获取'。验证:多线程模拟 search/match 并发 + 长 N 订阅,断言不重复下载、episode_priority 无覆盖。必须先于 PR-K 结构拆分。
- PR-D monitor 调度修复(#7+#8):modify_job→reschedule_job(monitor.py:794);全失败时不覆盖快照(731-787)。验证:断言 reschedule_job 被调用且间隔生效;mock 全路径返回 None 时旧快照保留。
- PR-E post_message 路由 bug(#4):sync/async 两处 break→continue(chain/__init__.py:1280/1397)+ fallback 日志。验证:user,admin+无 username 断言只发管理员、不广播。本 PR 先修 bug 不重构。
- PR-F 事件循环阻塞:systemconfig set() DB I/O 移出 _rlock(systemconfig_oper.py:36,#9);upload_avatar await read + 大小限制(user.py:116,#10)。验证:并发 set/async_set 不 stall;超限上传 413。
- PR-G agent 取消/鉴权:_execute_agent re-raise CancelledError + heartbeat 改 clear_session(agent/__init__.py:1392/1896,#11);save_auth/clear_auth 加 asyncio.Lock(provider.py:1385,#12)。验证:stop 立即生效、heartbeat 不阻塞 60s;并发双 provider OAuth 两 token 均保留。
- PR-H 模块契约修正:themoviedb 哨兵反转(themoviedb/__init__.py:960,#13);Telegram 实例级 dict + per-instance base_url(telegram.py:39-47/76-85,#14)。验证:非 themoviedb 源不发 None tmdb_id 请求;多 Bot 配置端点/映射互不污染。

P2(健壮性 + 结构债 + 部署条件型):
- PR-I None/falsy 守卫族(分文件小 PR 或合并):download.py:129 失败返回 (None,None)、transfer.py:3221 mtype.value 保护、systemconfig set/async_set 改 is not None(:44/:80)、site_oper.py:74 None 检查、tmdbapi.py 同异步 title/name null 安全(:59/76/1739/1756)、media.py:1657 改 is not None。每项配最小复现单测。
- PR-J 反序列化加固(needs-scoping,先确认触发条件):pickle→JSON/HMAC(redis.py:65、chain/__init__.py:66,#16 的 redis 部分),配置模板强制 requirepass;缓存 key 加版本前缀避免旧条目不可读。
- PR-K 结构治理(排在 PR-C 之后,#17):TransferChain name-mangling→受保护方法 + Protocol 契约(transfer.py 8 处);SubscribeChain 按域拆 BestVersionStateMachine/SubscribeSearchService/SubscribeMetadataService。大 PR,需全回归。
- PR-L 其余低危:exclude words re.escape+try/except(transfer.py:3398)、字幕临时路径 uuid 前缀(download.py:217)、aes_decrypt 全量 PKCS7 校验(security.py:392)、verify_resource_token 401 替 403(security.py:217)、recognize_lock 死代码删除(media.py:33)、_user_sessions 实例锁/setdefault(message.py:1000)、[PLUGIN] ValueError 守卫(message.py:601)、DbManager.register_plugin 加锁/setdefault(manager.py:141)、clone_plugin UserInstalledPlugins 原子化(plugin_manager.py:1699)、start() 双锁 TOCTOU 守卫(plugin_manager.py:112)、ModuleManager.stop 持锁快照(core/module/manager.py:73,needs-scoping)、MemoryBackend 类变量→实例+按 cache_type 隔离 region(cache.py:364)、plugin_cloner 改 AST 重写(plugin_cloner.py:124)、Site.domain UNIQUE+IntegrityError(site.py:19 + Alembic 迁移)、LLM 响应截断净化(search.py:457)、provider.py print→logger(:2043/2545/2570)、SSO Host 头校验(auth.py:344,needs-scoping)、EventManager SystemError 递归/无界队列防护(manager.py:668)。

序列依赖:PR-A 的 is_within util 是 #1/#2/#15/#16 的共同前置,先落;PR-C(#5+#6)必须在 PR-K 结构拆分前;PR-E 先修 break 再做 PR-K 中 post_message 共享抽象;PR-B 的 SystemError 回调与 PR-L 的 EventManager 递归防护需协调,避免互相引发广播循环。

通用验证方式:每条按 TDD——先写复现缺陷的失败单测(RED),再修(GREEN);并发类用多线程/asyncio 压测断言无重复副作用;APScheduler 类验证 reschedule_job 真实改触发器;FastAPI 类验证大小限制与状态码语义;反序列化类验证篡改载荷被拒。needs-scoping 项交付前先在目标部署形态(Redis 无认证/多 Bot/未配 APP_DOMAIN)确认触发条件再合入。运行测试时区分仓库既有失败基线(见 MEMORY:v2→v3-python 同步方法论的 pre-existing 失败基线),只对新增/受影响用例做绿灯门禁。

---

## 七、完整 Confirmed 缺陷清单(35)

### `agent-ai`

- **[🟠 HIGH] [边缘漏洞] _resolve_relative_path 接受绝对路径和 .. 穿越，extra_context_files 可加载任意系统文件**  
  位置 `app/agent/runtime.py:733-735`  
  机制:对 value 为 /etc/passwd 或 ../../secrets/config.yaml 等情形，函数不做任何 runtime_dir 边界校验即返回路径。extra_context_files 字段存于 CURRENT_PERSONA.md YAML frontmatter，agent 工具层（switch_persona、write_file 等 admin 工具）可修改该文件；加载后内容以 <agent_extra_context> 块注入每次 LLM 系统提示词，进而出现在 LLM 响应输出中。  
  影响:具有 is_admin=True 的 Agent 会话（或直接写 CURRENT_PERSONA.md 的攻击者）可将 /etc/passwd、DB 密钥文件等绝对路径写入 extra_context_files，服务器进程可读的任意文件内容会在下次 Agent 调用时被带入系统提示词并可能通过 LLM 输出泄露。  
  修复:在 _resolve_relative_path 或 _resolve_optional_paths 中校验解析后路径必须以 runtime_dir.resolve() 为前缀（Path.is_relative_to）；对绝对路径直接拒绝或同等校验，确保 extra_context_files 只能引用 runtime 目录下文件。

- **[🟡 MEDIUM] [调用逻辑] _execute_agent 吞掉 CancelledError 致 stop_current_task 失效、heartbeat 阻塞 60 秒**  
  位置 `app/agent/__init__.py:1392-1395, 1896-1910`  
  机制:asyncio cancel 只注入一次 CancelledError；_execute_agent 在 line 1392 catch 后正常 return，task 不被标为 CANCELLED，_session_worker 继续运行并进入 wait_for(queue.get(), timeout=60)。后果双重：① stop_current_task 调用 task.cancel() 再 await task，由于 CancelledError 已消耗，await 最多等 60 秒（worker 空闲超时）才返回，stop 语义失效；② heartbeat_check_jobs 在 queue.join() 后应立即取消 worker，却直接 await 等其自然退出，同样阻塞最多 60 秒。  
  影响:① 用户调用停止 Agent 后，当前消息继续执行到 LLM 返回，stop 动作对调用方显示已停止但实际未中断；② 心跳任务每次完成检查后额外阻塞最多 60 秒，心跳周期被大幅拉长，定时任务检查窗口变宽，且多次心跳时可能出现并发叠加。  
  修复:① _execute_agent 在 CancelledError 处理完必要清理（stop_streaming、emit 通知）后 raise 以保持传播链；② heartbeat_check_jobs 在 queue.join() 后直接调用 clear_session(session_id, user_id)（内部已含 cancel+await），删除直接 await worker 的代码段（line 1901-1905）。

- **[🟡 MEDIUM] [方法] save_auth / clear_auth 无锁 read-modify-write，并发 OAuth 完成可互相覆盖**  
  位置 `app/agent/llm/provider.py:1385-1406`  
  机制:两段操作之间没有任何锁保护。若请求 A 和请求 B 几乎同时完成两个不同 provider 的 OAuth 授权流程，两者都读到旧 config，各自写入自己的 provider auth，后写者覆盖先写者，先完成的 token 静默丢失。现有 _lock (threading.RLock) 只保护内存中的 _pending_sessions，不覆盖持久化写入路径。  
  影响:用户同时为两个 provider（如 chatgpt + github-copilot）完成设备码授权时，先授权成功的 token 可能被后授权者的写入覆盖，导致其中一个 provider 认证失效，用户需重新授权。  
  修复:引入异步锁（asyncio.Lock）包裹 read→modify→write 三步，或改用 SystemConfigOper 的原子 compare-and-set 操作（如果底层支持）；亦可在 _read_agent_config/_write_agent_config 层面引入乐观锁重试。

- **[⚪ LOW] [方法] provider.py 三处 print(err) 绕过结构化日志，错误信息泄露到 stdout**  
  位置 `app/agent/llm/provider.py:2043, 2545, 2570`  
  机制:使用 print() 替代 logger，输出走 stdout 而非应用日志系统。在容器化部署场景下，stdout 和结构化日志走不同管道；JWT decode 错误（含 token 内容片段）、model lookup 异常等会出现在 stdout 而被日志监控漏报，同时 print 不携带日志级别、时间戳、上下文字段，难以关联排查。  
  影响:生产异常被日志系统遗漏（Sentry/ELK 等采集的是 logger 输出）；line 2043 的 JWT 解码错误可能在 stdout 中打印含 token 内容的异常信息；整体降低可观测性，错误无法被监控告警捕获。  
  修复:将三处 print(err) 替换为 logger.debug 或 logger.warning，添加上下文信息（如 provider_id、token 前缀掩码等），与其他错误日志风格保持一致。

- **[⚪ LOW] [架构] load_runtime_config 在缓存命中路径仍执行 ensure_layout + rglob 双重目录扫描**  
  位置 `app/agent/runtime.py:271-291`  
  机制:缓存门控放在 ensure_layout 和 _build_signature 之后：即使配置未变，每次 awrap_model_call 都先执行 ensure_layout（包含 _sync_bundled_defaults 的 rglob 扫描）再执行 _build_signature（两次 rglob），最后才检查缓存。对于一次含 10 次工具调用的 Agent 执行，等价于 20+ 次 rglob + 70 次 mkdir exists_ok 调用。RuntimeConfigMiddleware 注释明确说明'不缓存到 middleware state'以支持热切换人格，但因此把代价全留在 load_runtime_config 的文件系统层。  
  影响:每条消息的多轮 LLM 调用会触发数十次 rglob 目录扫描，在慢速存储（Docker volume、NFS）上造成明显延迟；随着 personas/memory 目录文件增多，性能退化呈线性。ensure_layout 的 _sync_bundled_defaults 还在每次调用时检查是否需要覆写默认文件，生产环境不应该逐请求执行。  
  修复:将 ensure_layout() 移到应用启动时一次性调用（与 agent_manager.initialize() 同处）；load_runtime_config 只保留 _build_signature + 缓存检查，或将 signature 改为轻量 inotify/mtime 快照而非 rglob；RuntimeConfigMiddleware 若需热切换，可在切换工具执行后主动调用 invalidate_cache()，其余路径复用缓存。

### `api-auth`

- **[🟡 MEDIUM] [方法] upload_avatar 在 async 端点内调用同步 file.file.read()，且无上传大小限制**  
  位置 `app/api/endpoints/user.py:116`  
  机制:`file.file` 是 Starlette 的 `SpooledTemporaryFile`；当文件超过溢出阈值时 `file.file.read()` 触发同步磁盘 I/O，直接阻塞 asyncio 事件循环（违反 FastAPI async 端点规范）。此外端点声明中无 `max_size` 或中间件级上传大小限制，攻击者可上传任意大小文件：一是在 `read()` 期间长时间独占事件循环；二是将超大 base64 字符串写入数据库，导致 OOM 或 DB 存储膨胀。  
  影响:普通用户（或被劫持会话）可上传超大文件，阻塞事件循环（影响全体并发请求）并持久化到 DB。对 async 密集型部署影响尤为明显，可用作低成本 DoS。  
  修复:将 `file.file.read()` 改为 `content = await file.read()`；在端点前置或中间件中限制上传大小（如 FastAPI `Request.body` 最大长度或 nginx `client_max_body_size`），并在读取后对 `len(content)` 做应用层校验（如限制头像 ≤ 2 MB）。

- **[⚪ LOW] [调用逻辑] verify_resource_token 在 Cookie 缺失时返回 403 Forbidden 而非 401 Unauthorized**  
  位置 `app/core/security.py:217-221`  
  机制:`resource_token_cookie` 配置了 `auto_error=False`，Cookie 不存在时 FastAPI 将 `resource_token` 注入为 `None` 而非抛异常。`__verify_token(None, "resource")` 的 `not token` 分支触发，抛出 HTTP 403 Forbidden。RFC 7235 规定 403 表示服务器理解请求但拒绝授权（credentials 存在但权限不足），缺少 Cookie 应返回 401 Unauthorized（缺少有效凭据）。与 `verify_token` 在无 Bearer/API token 时返回 401（行 275-278）行为不一致，且导致 OAuth2/前端逻辑误以为用户已认证但无权限，而不是提示重新登录。  
  影响:使用 `/system/img`、`/system/cache/image` 等资源端点的 HTTP 客户端/前端框架在 Cookie 丢失时得到 403，会当作权限不足而非重定向到登录页，导致功能静默失败而不是正常的认证恢复流程。  
  修复:将 `__verify_token` 中 `not token` 分支（第 218 行）的状态码改为 `status.HTTP_401_UNAUTHORIZED`，并增加 `WWW-Authenticate: Cookie` 响应头；或在 `verify_resource_token` 包装层做前置检查并返回 401，使整套认证层 status code 语义一致。

### `chain-core`

- **[🟡 MEDIUM] [调用逻辑] post_message / async_post_message 中 else 分支 break 在 action='user' 无用户名时静默丢弃后续动作（如 'admin'）**  
  位置 `app/chain/__init__.py:1239-1290 (同步), 1354-1406 (异步)`  
  机制:当 action='user' 但 dispatch_message.username 为 None（系统级通知无用户上下文）时，elif 条件为 False，控制流进入 else 分支：设置 send_orignal=True 并 break。后续配置的 'admin' action 被永远跳过。最终发送原始 dispatch_message（无 targets，广播到所有通知渠道），而非像预期那样精确发送给管理员。async_post_message 存在完全相同的逻辑复制品（line 1393-1397）。  
  影响:在 notify_action="user,admin"（最常用的配置）且系统通知（无 username，如下载完成、整理完成等后台事件）场景下：消息被广播到所有已配置的通知渠道（Telegram、企业微信、公共 Bot 等），而非仅推送给管理员。可能导致本应仅管理员可见的系统通知泄漏到公共频道。  
  修复:将 else 分支的 break 改为 continue，并在 else 分支中记录 fallback 到全量发送的日志；或将 break 替换为仅在 action 为 'all' 等明确全量标识时才中断循环，对 'user'（无 username 时）改为直接 continue 让后续 'admin' action 正常执行。

- **[🟡 MEDIUM] [架构] post_message / async_post_message 230 行消息路由状态机逐字节重复，无共享抽象**  
  位置 `app/chain/__init__.py:1188-1302 (同步 post_message), 1304-1418 (异步 async_post_message)`  
  机制:整段 actions 状态机（含 #4 指出的 break 逻辑缺陷）在同步和异步两个方法中逐行复制，无任何共享辅助函数。任何业务逻辑变更（如 #4 的 bug fix、新增路由策略）必须在两处独立修改，且无测试约束双路径一致性。当前 #4 的 break bug 就同时存在于两处（line 1280 和 line 1397）。  
  影响:维护负担：任一路径的 bug fix 若漏掉另一路径，造成同步/异步行为分裂——用户在不同调用场景（同步 Chain 方法 vs asyncio 协程上下文）获得不同的消息路由结果。历史上已积累若干细节分歧（如 messageoper.add 调用的时机细节）。  
  修复:抽取 _build_routing_tasks(dispatch_message, useroper) -> List[Notification] 函数，返回按目标分组后的消息列表，同步和异步版本共享该函数；两者仅在 send 层 await 展开不同。也可使用 async_to_sync 适配器将整段逻辑统一为异步，同步版本通过 run_sync 包装。

### `chain-message-media`

- **[⚪ LOW] [边缘漏洞] [PLUGIN] 回调缺少 | 分隔符导致 ValueError 吞没后台消息任务**  
  位置 `app/chain/message.py:601-603`  
  机制:callback_data 若格式为 '[PLUGIN]plugin_id'（无 | 分隔符），split('|',1) 返回长度 1 的列表，元组解包立即抛出 ValueError。_handle_callback 无 except 块，handle_message（line 174）只有 try/finally 无 except，ValueError 穿透 finally（typing 状态正确清理）后被 Starlette background_tasks 捕获并静默丢弃，不向调用者重抛。  
  影响:用户点击某生成了格式错误回调数据的插件按钮后，消息处理后台任务整体失败，用户得不到任何错误提示，体验为按钮静默失效。触发条件：任意插件在 callback_data 中省略 | 分隔符即可触发。  
  修复:在 _handle_callback 的 [PLUGIN] 分支中改用 parts = callback_data.split('|', 1) 并检查 len(parts) == 2，不满足时走 logger.error 并向用户发送错误提示；或在解包前加 try/except ValueError。

- **[⚪ LOW] [方法] async_recognize_help 对 Season 0 / Episode 0 使用 falsy 检查，与同步版本语义不一致**  
  位置 `app/chain/media.py:1657 vs 708`  
  机制:begin_season=0 表示特别季（Specials），是合法值；但 `if org_meta.begin_season or ...` 对 0 求值为 False，导致 org_meta.type 不被修改为 MediaType.TV。同步版本以 `is not None` 正确区分了 None（未知）与 0（特别季）。  
  影响:在 RECOGNIZE_PLUGIN_FIRST 模式下，插件辅助识别返回 season=0 或 episode=0 时，异步识别路径（async_recognize_by_meta -> _async_recognize_with_fallback_by_meta -> async_recognize_help）的 org_meta.type 保持原值（可能为 MOVIE），后续 async_recognize_media 以错误类型查 TMDB，导致识别失败或识别为错误媒体。Agent 工具链（async_recognize_by_meta）均走异步路径，受此影响。  
  修复:将 media.py:1657 改为与同步版本一致的 `if org_meta.begin_season is not None or org_meta.begin_episode is not None:`。

- **[⚪ LOW] [架构] recognize_lock 在 media.py 定义后从未使用，是孤立死代码**  
  位置 `app/chain/media.py:33`  
  机制:全仓库 grep 显示 recognize_lock 仅在 media.py:33 处被定义，从未被 import 或 acquire。scraping_lock 被正确地用于保护 scrape_metadata_event 的并发执行（line 907）。recognize_lock 的存在暗示识别流程曾有或曾被设计为需要序列化保护，但锁的引用在重构中被删除而定义未清理。  
  影响:当前无功能影响，但造成维护误导：读者可能误以为识别路径有全局锁保护。若将来有开发者在 recognize_by_meta 或 recognize_media 中基于此锁假设添加 with recognize_lock:，将引入虚假的全局串行化，显著降低并发识别吞吐量。  
  修复:删除 media.py:33 的 recognize_lock = Lock() 定义；若识别确需全局串行化，明确在 recognize_by_meta 内用该锁并补充注释说明原因。

- **[⚪ LOW] [调用逻辑] _user_sessions 类级共享字典无锁保护，TOCTOU 竞态可导致 AI 消息处理失败**  
  位置 `app/chain/message.py:1000-1001 / 48`  
  机制:MessageChain 非 Singleton（ChainBase 无 Singleton metaclass），每次请求创建新实例，但所有实例共享同一类级字典。CPython GIL 在字节码边界可切换：线程 A 通过 line 1000 的 in-check（True），GIL 切换，线程 B 执行 clear_user_session/remote_clear_session 弹出同一 userid，GIL 回到 A，line 1001 的 dict[userid] 访问引发 KeyError。该异常被 _handle_ai_message 的 except Exception（line 1388）捕获，用户收到 'AI智能体处理失败' 错误而非正常响应。并发场景：用户同时发送消息与 /clear_session 命令，或两个 HTTP 请求处理同一用户的消息。  
  影响:在用户同时操作（消息 + /clear_session 命令并发）或高并发情况下，AI 消息处理静默失败，记录错误日志但不崩溃。会话状态可能在清理和创建之间出现短暂不一致，导致用户意外地获得新会话而非预期的已有会话。  
  修复:为 _user_sessions 访问引入实例级 threading.Lock（不影响 by-design 类级共享语义），或在 _get_or_create_session_id 中改用 dict.setdefault/get 的原子操作替代 in-check + access 双步骤；同时将 clear_user_session 和 _cleanup_expired_user_sessions 也纳入同一锁保护。

### `chain-subsearch`

- **[🟡 MEDIUM] [方法] search()/match() 锁超时后继续无锁执行，破坏互斥保障**  
  位置 `app/chain/subscribe.py:979-987 (search), 1353-1361 (match)`  
  机制:try-body 的工作主体（订阅查询、搜索、下载、状态更新）写在 if/else 之外，无条件执行。finally 只在 lock_acquired=True 时 release。当 _rlock.acquire(timeout=7200) 超时返回 False，代码仅打一条 warn，随即继续全量执行——锁已被另一线程持有，两者同时跑。match() 同结构（1353-1361 行）。  
  影响:APScheduler 分配的 search 线程与 match 线程同时执行时：相同订阅被双重处理 → 重复下载；update_subscribe_priority 对同一 subscribe.id 并发写 episode_priority → 后写覆盖先写导致状态损坏。订阅数量多（see finding #2）时 2 小时超时实际可达。  
  修复:在 lock_acquired=False 的 else 分支末尾立即 return，中止后续执行：`else: logger.error('无法获取锁，跳过本次执行'); return`。或将整个工作体移入 `if lock_acquired:` 分支内。

- **[🟡 MEDIUM] [方法] time.sleep(60~300s) 在 _rlock 持有期内逐订阅执行，使超时触发成为必然**  
  位置 `app/chain/subscribe.py:1010-1013`  
  机制:search() 的 for subscribe in subscribes 循环体内、外层 try（持有 _rlock）之中，每迭代一个 R/P 状态订阅就阻塞 60-300 秒。N 个订阅累计 60N-300N 秒持锁睡眠，N≥24 即可突破 7200 秒限制，直接触发 #1 的无锁并发执行；且整个周期内 match() 被完全封锁，RSS 缓存匹配无法运行。  
  影响:订阅数多时 match() 长时间阻塞，导致 RSS 新资源无法及时匹配；累计睡眠超限 → 触发 bug #1 竞态 → 双重下载。  
  修复:将每次循环的 sleep 移至释放锁后再执行（循环外），或采用「释放-睡眠-重新获取」模式，避免在持锁期间阻塞线程。

- **[🟡 MEDIUM] [架构] SubscribeChain 3413 行上帝类承载 ≥8 个独立职责，违反单一职责原则**  
  位置 `app/chain/subscribe.py:48-3413`  
  机制:洗版状态机、搜索调度、RSS 匹配、元数据刷新、社交关注同步、日历缓存等完全独立的业务域被压缩进同一个类。它们共享 _rlock 但 check() 不遵守（见 finding #3），各路径的接触面互相放大 bug 影响半径，任一域的修改都需要通读 3400+ 行才能评估影响范围。  
  影响:单文件超过编码规范上限（800 行）4 倍以上；洗版状态机与搜索循环耦合导致 #1/#2 类并发问题难以单独修复；无法对洗版逻辑单独测试而不涉及下载/消息路径。  
  修复:将洗版状态机提取为 BestVersionStateMachine 独立类；将 search()/match() 提取为 SubscribeSearchService；将 check()/follow()/cache_calendar() 提取为 SubscribeMetadataService；通过组合而非继承在主类中调用。

- **[⚪ LOW] [调用逻辑] check() 在不持 _rlock 的情况下修改 episode_priority，与持锁的 search()/match() 形成 read-modify-write 竞态**  
  位置 `app/chain/subscribe.py:1698-1775 (check), 1197-1225 (update_subscribe_priority under lock)`  
  机制:check() 与 search() 由 APScheduler 分配到不同线程独立运行，两者均对同一 subscribe 的 episode_priority JSON 字段做「读-改-写」，但 check() 不进入 _rlock。竞态窗口：search() 在 line 1153 重新从 DB 拉取 subscribe 后，check() 写入新增剧集的 priority=0 条目，search() 随后用早一步的快照写回，覆盖 check() 的写入。  
  影响:新播出集的 episode_priority 初始条目被静默丢弃，该集在下一次 search() 的洗版逻辑中被视为「未在范围内」，须等下一轮 check() 才能恢复，造成洗版订阅漏检一个周期。  
  修复:check() 修改 episode_priority 时也应先获取 _rlock，或改用 DB 级原子更新（如 JSON_SET/JSON_PATCH 操作）代替 read-modify-write。

### `chain-transfer`

- **[🟡 MEDIUM] [方法] _resolve_media_download_dir 失败路径返回裸 None，调用方 2-tuple 解包 → TypeError 崩溃**  
  位置 `app/chain/download.py:129, 303`  
  机制:_resolve_media_download_dir 的成功路径返回 Tuple[str, Path]，但当 dir_info 为 None 且 save_path 也为空时，返回裸 None 而非元组。调用方 download_subtitle() 无条件做 2-tuple 解包，Python 抛出 TypeError: cannot unpack non-iterable NoneType object；'if not target_dir' 的兜底检查永远不会执行。  
  影响:用户下载字幕但媒体类型未配置下载目录时，download_subtitle() 以 TypeError 崩溃而不是返回 (False, '未找到下载目录', [])，导致 API 500 错误。  
  修复:失败路径改为 return None, None，或重构为 return storage, None，让调用方的 if not target_dir 正常生效。同时修正函数类型注解（实际返回 Tuple[str, Optional[Path]] 或 None）。

- **[⚪ LOW] [方法] __re_transfer 错误路径在 mtype=None 时对 None 调用 .value → AttributeError**  
  位置 `app/chain/transfer.py:3221`  
  机制:redo_transfer_history() 调用 __re_transfer(logid=id)，mtype 保持默认值 None。当 recognize_by_path 无法识别媒体时，进入错误分支，f-string 中 mtype.value 对 None 调用 .value，抛出 AttributeError 而非返回友好错误字符串。  
  影响:用户在历史记录页点击「重试」整理无法识别的文件时，API 返回 500 错误，而不是提示性的错误信息；Telegram /redo 命令同理会收到系统异常而非说明性消息。  
  修复:将错误信息改为 f"未识别到媒体信息，类型：{mtype.value if mtype else '未知'}，id：{mediaid}"，或在格式化前保护 None：type_str = mtype.value if mtype else '(自动识别)'。

- **[⚪ LOW] [边缘漏洞] _is_blocked_by_exclude_words 将用户配置词汇原样拼入 re.search，无 re.escape 也无异常捕获**  
  位置 `app/chain/transfer.py:3398`  
  机制:整理屏蔽词来自 SystemConfigOper().get(SystemConfigKey.TransferExcludeWords)，由管理员在 UI 配置。若管理员输入无效正则（如 (unclosed 或 [a-）），re.search 抛出 re.error，此异常未被 _filter() 或任何调用层捕获，直接穿透 __get_trans_fileitems() 到 do_transfer()，最终导致整个定时 process() 调用崩溃（downloader_lock 内）。此外，精心构造的回溯炸弹（ReDoS）对长路径可导致秒级阻塞。  
  影响:管理员误输入一个无效正则即可导致周期性整理定时任务全部失败；攻击者若能写入系统配置，可通过 ReDoS 阻塞整理线程。  
  修复:改用 re.escape(keyword) 或在 _is_blocked_by_exclude_words 内包裹 try/except re.error 并 logger.warn 提示无效词汇后跳过；或在保存屏蔽词时预验证每条正则。

- **[⚪ LOW] [边缘漏洞] 并发字幕下载共享无唯一前缀的临时路径 TEMP_PATH/file_name，互相覆盖文件内容**  
  位置 `app/chain/download.py:217-218`  
  机制:download_subtitle() 是同步方法，FastAPI 在线程池并发执行。两个并发字幕下载若解析出相同 file_name（如同一剧集使用相同 fallback_name），均写入 settings.TEMP_PATH/file_name：请求 A 写完后，请求 B 覆盖该文件；请求 A 随后上传的是请求 B 的内容。最终两个请求都把错误的字幕上传到目标目录；finally 中 unlink 也会互相干扰。  
  影响:并发字幕下载时，用户 A 的字幕文件被用户 B 的内容替换并上传；解压路径 temp_extract_dir 同理，rar/zip 解压结果相互污染。  
  修复:在 file_name 前添加进程唯一前缀，如 temp_file = settings.TEMP_PATH / f'{uuid.uuid4().hex}_{file_name}'，或使用 tempfile.mkstemp/NamedTemporaryFile 创建唯一临时文件。

- **[⚪ LOW] [架构] TransferService/ScrapeBatchCoordinator/TransferResultProcessor 通过 _TransferChain__xxx 名称改写绕过封装访问私有 API**  
  位置 `app/chain/transfer.py:851, 873, 959, 1098, 1130, 1132, 1134, 1363`  
  机制:P2 重构将 TransferChain 拆分为 4 个协作类，但拆出的三个 helper 类通过 Python name-mangling 绕过（_ClassName__method）访问 TransferChain 的私有方法。Python 对 mangled 名不做编译期检查，重命名 TransferChain 的任何双下划线前缀方法均导致 AttributeError，且静态分析工具（mypy、pylint）和 IDE 均无法感知这些隐式耦合，重构安全网实际失效。  
  影响:任何对 TransferChain 私有方法的重命名或提取均产生运行时 AttributeError 而非编译期错误；P2 拆分的封装目标被反向耦合；code review 无法通过普通方法调用检索工具发现这些依赖关系。  
  修复:将这 8 个被跨类访问的私有方法提升为受保护方法（单下划线前缀）或包内公开方法，并通过正式接口（协议/ABC）声明契约，使依赖可见且可静态检查。

### `db`

- **[🟡 MEDIUM] [方法] set() 在持有 _rlock 期间执行 DB I/O，async_set 并发时阻塞事件循环**  
  位置 `app/db/systemconfig_oper.py:36-53`  
  机制:_rlock 的设计意图是保护内存 dict __SYSTEMCONF（dict 赋值，微秒级），但 set() 把 1~3 次 DB I/O（可达数百毫秒）全包在 with self._rlock: 块内。与此同时，async_set 在 asyncio 事件循环线程内也需在 line 75/88 短暂获取 _rlock（仅做缓存赋值）。若后台线程正在 set() 里做 DB I/O，事件循环线程在 with self._rlock: 处阻塞，整个事件循环 stall。  
  影响:触发条件：后台定时任务/热重载线程调用 set() 期间，任一 async 路由也触发 async_set()。SQLite 默认 5s 超时下 stall 可长达秒级，FastAPI 表现为请求全部超时。  
  修复:将 DB I/O 移出 _rlock 块：先在锁外做 DB 读，再 with self._rlock: 仅判断旧值+更新缓存，释放锁后再写 DB（与 async_set 保持对称）。或将 set() 改为异步并统一由 _alock 序列化，DB I/O 放入 executor。

- **[⚪ LOW] [架构] DbManager.register_plugin 无锁保护，并发调用同一 plugin_id 导致 Engine 泄漏**  
  位置 `app/db/manager.py:141-148`  
  机制:check-then-set 操作非原子。DbManager 是模块级单例（db_manager = DbManager()），可被 PluginManager（持有自身 _plugins_lock）、run_plugin_migrations 以及 setup_plugin_database 从不同调用路径以不同时序触发。同 plugin_id 并发注册时，两个路径都通过 if existing is not None 检查，各自创建 PluginDatabase（含独立 Engine/QueuePool），后写入的 bundle 覆盖先写入的；先写入的 Engine 持有连接池但永远不会被 dispose，造成连接资源泄漏。  
  影响:触发条件：热重载（reload）与插件安装/启用并发执行，或 run_plugin_migrations 与 setup_plugin_database 针对同一插件并发调用。每次泄漏一个 Engine 及其 QueuePool（默认 5 连接），长期运行文件描述符耗尽。  
  修复:在 DbManager.__init__ 中添加 threading.Lock()，register_plugin/drop_plugin 均在锁内操作 _plugins；或采用 setdefault 原子模式（先构建 bundle，再 _plugins.setdefault(plugin_id, bundle)，若已有则 dispose 新建的）。

### `entry-lifecycle`

- **[🟠 HIGH] [调用逻辑] Scheduler.start() 协程分支丢弃 Future，__finish_job 提前执行且异常完全静默**  
  位置 `app/scheduler.py:703-751`  
  机制:asyncio.run_coroutine_threadsafe 返回 concurrent.futures.Future，但返回值未被捕获（丢弃）。程序在提交协程后立即执行到 __finish_job(job_id)，将 running 标志设回 False。协程在事件循环线程中异步运行，其完成时间与 __finish_job 完全脱钩。两个后果：① 同一 Job（如 agent_heartbeat、async_get_online_plugins 等 async Job）的下一次定时触发到来时，__prepare_job 看到 running=False，允许再次启动，与上一次协程并发运行；② 协程内任何异常均存储在被丢弃的 Future 中，既无 logger.error，也无 MessageHelper 通知，也无 SystemError 事件，与同步 Job 的完整 except 链路（行 733-749）形成黑白对比。  
  影响:agent_heartbeat 若执行慢于调度间隔，高负载时协程堆积；async 插件 Job 的失败完全不可观测，运维无感知。  
  修复:捕获 Future 并在调度线程上 .result(timeout=...) 等待，使 __finish_job 在协程真正结束后执行；或改用 run_in_executor 将协程包装为同步调用再统一进入已有 try/except 路径，同时补齐错误日志与事件通知。

- **[🟡 MEDIUM] [调用逻辑] monitor.py polling_observer 调用 modify_job(trigger='interval') 必然抛 TypeError，动态间隔调整功能完全失效**  
  位置 `app/monitor.py:791-799`  
  机制:modify_job 将 **changes 直接转发给 Job._modify；_modify 要求 trigger 必须是 BaseTrigger 实例而非字符串。传入 trigger='interval'（str）立即抛 TypeError；同时 minutes=new_interval 也不是合法的 Job 属性，同样会导致 AttributeError。两者均被外层 try/except Exception（行 801-803）吞掉，日志显示为误导性的「轮询监控出现错误」，实际间隔始终不变。正确 API 是 reschedule_job(job_id, trigger='interval', minutes=N)。  
  影响:每次 polling_observer 判断需要调整间隔时均静默失败并记录错误日志；动态间隔特性自始至终不生效，大型远端存储始终以固定初始间隔轮询，无法根据文件数量自动降频。  
  修复:将 modify_job 替换为 self._scheduler.reschedule_job(f'monitor_{storage}', trigger='interval', minutes=new_interval)，这才是 APScheduler 3.x 中修改触发器的正确入口。

- **[⚪ LOW] [架构] Scheduler.init() 直接原地修改全局 settings.SUBSCRIBE_RSS_INTERVAL，每次重载持久破坏配置状态**  
  位置 `app/scheduler.py:498-511`  
  机制:settings 是进程内全局单例（Pydantic BaseSettings）。init() 对其字段的原地赋值会永久改变全局可见值，后续任何读取 settings.SUBSCRIBE_RSS_INTERVAL 的路径（包括 CONFIG_WATCH 监听到变更后再次调用 on_config_changed → init()）都看到被截断后的值，而非用户实际配置的原始值。此写操作发生在 with lock: 内部，但 lock 并非 settings 的写保护锁，持有 self._lock 的其他读取路径（如 list()）也不受保护。同时 SUBSCRIBE_RSS_INTERVAL 已列在 CONFIG_WATCH 中，每次配置变更触发 init() 都会重复执行此写操作，导致配置值单向漂移到 5 或 30，与用户配置永久脱钩。  
  影响:用户配置 SUBSCRIBE_RSS_INTERVAL=3 后，每次 Scheduler 重新初始化都自动变为 5 并打印无警告日志；用户看到行为不符时难以诊断原因，因为 settings 值已在内存中被改写。  
  修复:引入局部变量 rss_interval = max(int(settings.SUBSCRIBE_RSS_INTERVAL or 30), 5) 或类似处理，不修改 settings 字段本身；若确需运行时校正，应写入独立的 SystemConfigOper 或 global_vars，不应污染只读的 env-derived settings 对象。

### `modules-contract`

- **[🟡 MEDIUM] [方法] _validate_obtain_images_params 的 RECOGNIZE_SOURCE 哨兵语义反转，跳过 tmdb_id 守卫**  
  位置 `app/modules/themoviedb/__init__.py:960-969`  
  机制:本方法以「返回 None = 继续抓图，返回 MediaInfo = 提前退出」为约定。但当 `RECOGNIZE_SOURCE != "themoviedb"` 时，方法直接 `return None`，导致调用方认为「验证通过，继续抓图」。这使后续的 tmdb_id 为空检查和「所有图已完整」检查均被完全跳过：（1）若 mediainfo.tmdb_id 为 None（Douban/Bangumi 识别结果），仍会以 None 为参数调用 `self.tmdb.get_movie_images(None, ...)`；（2）即便图片已全部完整也不会提前返回，触发不必要的 TMDB 网络请求。正确语义应为：source != themoviedb 时 `return mediainfo`（提前退出），以与后两个守卫保持一致。  
  影响:触发条件：`RECOGNIZE_SOURCE` 设置为 `douban`、`bangumi` 等非 tmdb 来源，且代码调用 `obtain_images`。后果：以 None 作为 tmdb_id 发出 TMDB API 请求，若库层未守卫则报错并写 error 日志；图片已完整也额外发出网络请求，产生无效流量和延迟。  
  修复:将第一个判断的返回值改为 `return mediainfo`：`if settings.RECOGNIZE_SOURCE != "themoviedb": return mediainfo`，使其与后续守卫语义一致（「不需要处理则返回 mediainfo」）。

- **[🟡 MEDIUM] [调用逻辑] Telegram.__init__ 直接覆盖 telebot.apihelper 模块级全局变量，多实例配置 last-write-wins 导致前实例静默走错端点/代理**  
  位置 `app/modules/telegram/telegram.py:76-85`  
  机制:`telebot.apihelper` 使用模块级变量存储 API 端点和代理，所有 TeleBot 实例在发送请求时均读同一全局变量。每次 `Telegram.__init__` 都无条件覆盖这三个全局值。若系统配置了两个 Telegram 实例（实例 A 使用自定义中转 api_url、实例 B 使用官方地址），后初始化的 B 会将 `apihelper.API_URL` 改回官方地址，导致 A 后续的所有请求静默切换到官方 Telegram API（可能因网络限制失败）；同理代理也被覆盖。两个实例竞争设置同一全局变量，结果取决于初始化顺序（不稳定）。  
  影响:触发条件：配置多个 Telegram 渠道，且其中至少一个使用自定义 API 地址（`API_URL` 配置项非空）。后果：使用自定义端点的机器人在另一实例初始化后静默转向默认 Telegram API，在受网络限制的部署环境（需要中转）下导致消息发送失败，且无任何错误提示（host 变了但 bot token 还在，请求会发到 api.telegram.org 并正常返回，很难察觉端点已切换）。  
  修复:每个 TeleBot 实例应独立管理端点：通过 `TeleBot(token, ..., base_url=api_url)` 或构造前临时 patch 后立刻还原，避免修改进程级模块全局变量。或在上层确保多 Telegram 配置只允许一个使用自定义 URL，并在 `init_service` 层显式拒绝不兼容的混合配置。

- **[⚪ LOW] [架构] Telegram 类级可变 dict 在多实例间共享，多 Bot 配置下状态互污染**  
  位置 `app/modules/telegram/telegram.py:39-47`  
  机制:Python 对可变类属性的「实例写穿透」语义：`self._typing_tasks['key'] = v` 会修改类级字典而非创建实例级副本。ServiceBase.init_service 会为每个配置名创建独立的 Telegram 实例，当用户配置多个 Telegram 机器人时，实例 A 的 typing 任务、callback handler、用户 chat_id 映射均与实例 B 共享同一 dict：A 开启的 typing 任务会出现在 B 的视图中，B 关闭时可能误停 A 的 typing 线程；不同 bot 收到同一 user_id 的消息时 `_user_chat_mapping` 会被后者覆盖，致前者回复走到错误 chat_id。  
  影响:触发条件：用户在系统配置中添加两个或以上 Telegram 机器人渠道（目前 ServiceBase 支持多配置）。后果：typing 状态混乱（线程泄漏或被提前停止）、用户-聊天映射交叉污染（消息回复到错误 chat）、callback handler 注册冲突。单机器人配置不受影响。  
  修复:在 `__init__` 中将各 dict 初始化为实例级属性：`self._callback_handlers = {}; self._user_chat_mapping = {}; self._typing_tasks = {}; self._typing_stop_flags = {}`，消除类级共享状态。

### `plugin-system`

- **[🟡 MEDIUM] [边缘漏洞] Zip Slip: release zip 条目路径未边界校验，可写入插件目录外任意文件**  
  位置 `app/helper/plugin.py:1726-1735 (sync __install_from_release); 2742-2753 (async __async_install_from_release)`  
  机制:zipfile.namelist() 可含 '../../' 段。Python Path 拼接不解析 '..'，open() 和 mkdir() 跟随内核路径解析，dest_base / '../../evil' 实际写到 dest_base 以外位置。base_prefix 剥离逻辑（lines 1716-1721/2732-2737）只处理统一顶层前缀，对条目内的 '../' 不作限制。两套 sync/async 实现都没有 resolved().is_relative_to() 校验。  
  影响:恶意 PLUGIN_MARKET 仓库发布含路径穿越条目的 release zip，可覆盖主程序任意可写文件（如 app/main.py），触发 RCE。异步安装路径同样受影响。  
  修复:写入前校验 resolved 路径在 dest_base 内：dest_resolved = dest_path.resolve(); if not dest_resolved.is_relative_to(dest_base.resolve()): logger.warning(...); continue。sync 和 async 两处同时修复。

- **[⚪ LOW] [边缘漏洞] clone_plugin suffix 无路径字符校验，可将插件文件树写入 plugins 目录外任意位置**  
  位置 `app/helper/plugin_manager.py:1652-1669`  
  机制:suffix='/../../../tmp' 使 clone_plugin_dir 解析到 ROOT_PATH/tmp；shutil.copytree 递归复制原插件全部文件到该路径，随后 modify_plugin_files 在此覆写 __init__.py。整个流程未对 suffix 做任何路径字符白名单校验。  
  影响:超级用户可将任意已加载插件的文件树（含修改后的 Python 源码）写到服务器文件系统任意可写目录，可用于污染 cron 目录或覆盖其他应用文件。  
  修复:在 clone_plugin 入口校验 suffix 仅包含字母数字和下划线：if not re.match(r'^[A-Za-z0-9_]+$', suffix): return False, '分身后缀含非法字符'。

- **[⚪ LOW] [方法] start() 双锁窗口：认证通过路径无 TOCTOU 守卫，并发 stop() 可造成 _running_plugins 与 _plugins 不一致**  
  位置 `app/helper/plugin_manager.py:112-120`  
  机制:在第一锁和第二锁之间，另一线程（API 触发的 reload_plugin / init_config）调用 stop(plugin_id)，原子地从 _plugins 和 _running_plugins 中移除 pid。start() 随后在第二锁内将 plugin_obj 写回 _running_plugins，但 _plugins 中已无对应类。结果：_running_plugins[pid] 有实例，_plugins[pid] 为空——即'僵尸实例'。  
  影响:僵尸实例持续响应事件和调度任务；get_plugin_config / save_plugin_config / delete_plugin_config 均检查 _plugins.get(pid) 并返回 {} 或 False，导致该插件配置读写失效且对外不可见。  
  修复:第二锁内写入前加同款守卫：with self._plugins_lock: if plugin_id in self._plugins: self._running_plugins = {**self._running_plugins, plugin_id: plugin_obj}，与不满足认证路径保持对称。

- **[⚪ LOW] [架构] clone_plugin() 对 UserInstalledPlugins 的读-改-写无锁保护，并发克隆可致列表更新丢失**  
  位置 `app/helper/plugin_manager.py:1699-1704`  
  机制:clone_plugin 是普通实例方法，无任何互斥保护。若两个并发请求同时克隆不同插件，两个线程均在 line 1701 读到相同的旧列表，分别 append 各自 clone_id 后各自 set 覆写，后写者覆盖先写者的更新，导致其中一个克隆 ID 从已安装列表中消失。同一方法还在 clone 之前使用 self._plugins（line 1649/1673）做无锁读，与 stop() 的写存在读写竞争。  
  影响:克隆成功（目录已创建）但 clone_id 未进入 UserInstalledPlugins，下次启动时该分身插件不被加载，表现为'克隆命令返回成功但分身消失'。  
  修复:将 UserInstalledPlugins 的读-改-写封装成原子操作（如数据库行锁或应用级 threading.Lock），或使用 SystemConfigOper 的 compare-and-swap 原语（若已有）。clone_plugin 整体应串行化。

- **[⚪ LOW] [方法] plugin_cloner.modify_python_file 用 str.index() 注入 is_clone 标志，首次匹配可命中文档字符串且硬编码 4 空格缩进与原文件不符**  
  位置 `app/helper/plugin_cloner.py:124-127`  
  机制:str.index() 找到第一次出现的 'def init_plugin(self'，若该串先出现在类文档字符串（docstring）或注释中，注入会将 'is_clone = True\n\n    ' 插入字符串字面量内部——is_clone 属性不生效，但 __init__.py 仍可作为合法 Python 解析（逻辑静默失效）。若原文件使用 2 空格缩进，content[:init_index] 末尾为 '  '（2 空格），注入后实际缩进为 '  is_clone = True'（正确）+ '    def init_plugin'（4 空格，错误）——IndentationError 导致克隆插件无法加载。  
  影响:使用 2 空格缩进的插件克隆后 import 失败（IndentationError），PluginManager.start() 捕获后跳过，clone_plugin 返回 True 但分身无法运行。文档字符串中含该签名的插件克隆后 is_clone 标志静默丢失，分身行为与原插件无法区分。  
  修复:改用 AST 重写（ast.parse + ast.NodeTransformer）定位目标类中的 init_plugin 方法，在类体正确位置插入 is_clone = True 赋值节点，再用 ast.unparse 或 libcst 重新生成源码，避免文本搜索的歧义和缩进硬编码。

---

## 八、Needs-Scoping 清单(17,真缺陷·触发依赖部署配置)

> 这些是**真实代码缺陷**,但触发需特定部署条件(无认证 Redis / 未配 APP_DOMAIN / 多 Bot / 可写缓存后端等)。处置:先确认环境是否满足触发条件,满足则按对应 severity 修复,并在配置模板/文档强制加固(如 Redis `requirepass`)。

### `api-auth`

- **[⚪ LOW] [边缘漏洞] aes_decrypt 手动 PKCS7 padding 校验不完整，产生无声错误明文**  
  位置 `app/core/security.py:392-395`  
  机制:PKCS7 规范要求末尾 `padding` 个字节的值均等于 `padding`；此处只检查最后一字节的范围是否在 [1,16] 内，不验证其余填充字节是否一致。若密文解密后末尾字节恰好在 [1,16] 但前面的 `padding-1` 个字节不全相等，函数会静默截掉错误数量的字节并返回错误明文，而不是返回空串或抛错。PyCryptodome 的 CBC 模式不自动剥离 padding，此人工处理逻辑存在缺陷。  
  影响:损坏或被篡改的密文能产生错误的明文，且调用方无感知。下游逻辑（站点 Cookie 解析、外部服务鉴权串解密）会以错误值运行，可能导致认证绕过或数据损坏，具体影响取决于调用场景。  
  修复:在截断前增加全量 padding 字节一致性校验：`if result[-padding:] != bytes([padding] * padding): return ""`；或直接使用 `Crypto.Util.Padding.unpad(result, AES.block_size)` 并捕获 `ValueError` 返回空串。

- **[⚪ LOW] [边缘漏洞] SSO 回调 redirect_uri 由用户可控 Host 头构造，存在 Host 头注入风险**  
  位置 `app/api/endpoints/auth.py:344-351`  
  机制:未配置 `APP_DOMAIN` 时，`_flow_callback_uri` 从 `request.base_url` 拼接 redirect_uri。FastAPI/Starlette 的 `base_url` 直接来自 HTTP `Host` 头（含反向代理未覆盖时的 `X-Forwarded-Host`）。攻击者若能在 `/auth/flow/begin` 请求时注入伪造 Host 头，可使 OAuth2 授权请求携带指向自控服务器的 redirect_uri，若 IdP 使用前缀/宽泛匹配，授权码将被回调至攻击者服务器，进而完整接管会话。代码自身已有此警告 log，但仍继续执行。  
  影响:配置缺失（未设 APP_DOMAIN）且 IdP 允许宽泛 redirect_uri 匹配时，攻击者可通过注入 Host 头窃取 OAuth 授权码，完成 Account Takeover。需要：攻击者能控制到达 FastAPI 的 Host 头，且 IdP 使用非精确匹配。  
  修复:方案一：`APP_DOMAIN` 未配置而有 SSO provider 注册时启动期抛出 ConfigurationError；方案二：通过 `settings.FORWARDED_ALLOW_IPS`（已配的可信代理范围）验证 X-Forwarded-Host 来源后再使用；方案三：对 `request.base_url` 与配置的允许域名做严格比对，不匹配则拒绝发起 SSO。

### `chain-core`

- **[⚪ LOW] [边缘漏洞] pickle.loads() 直接反序列化 Redis/文件缓存内容，后端可写时 RCE**  
  位置 `app/chain/__init__.py:66,79`  
  机制:FileCache() 在 Redis 模式下（settings.CACHE_BACKEND_TYPE == 'redis'）将 pickle 序列化字节直接存入 Redis，读取时 pickle.loads() 不做来源校验。cache key 名称固定且可预测（__torrents_cache__、__rss_cache__、__system_restart__ 等，见 app/chain/torrents.py:27-28, system.py:23）。若 Redis 无密码认证（Docker 场景常见），攻击者可通过 redis.set('__torrents_cache__', malicious_pickle) 注入载荷；下次 MovePilot 加载缓存时触发任意代码执行（CWE-502）。文件模式下 FileBackend.get() 直接 open(base/region/key, 'rb') 读文件，若 TEMP_PATH 目录可被攻击者写入同样触发 RCE。  
  影响:Redis 无认证部署（常见于 Docker 容器内）：网络可达的攻击者写入 Redis 已知 key → 服务端 pickle.loads() → 以运行进程权限执行任意命令。文件模式需本机文件写权限，影响范围较小但原理相同。  
  修复:改用 json / msgpack 等安全序列化替代 pickle；或对 content 做 HMAC-SHA256 签名校验（签名密钥来自 settings.SECRET_KEY）后再 pickle.loads()；同时强制 Redis 配置 requirepass 并在文档/配置模板中标注。

- **[⚪ LOW] [方法] ModuleManager.stop() 不持锁遍历 _running_modules，与 unregister_modules() 并发致同一模块被双重 stop**  
  位置 `app/core/module/manager.py:73-84 (stop) vs 277-290 (unregister_modules)`  
  机制:stop() 在无锁状态下通过 list(self._running_modules.items()) 快照遍历并对每个模块调用 module.stop()。unregister_modules() 持 _lock 从 _running_modules 中 pop 模块后也调用 running.stop()。若线程 A 在 reload() → stop() 中已对模块 X 完成快照，线程 B 的 unregister_modules() 随即 pop 模块 X 并 stop()，线程 A 继续迭代快照又 stop() 一次，同一模块实例 stop() 被并发调用两次。  
  影响:若模块的 stop() 非幂等（关闭 DB 连接、释放文件句柄、设置 _running=False 后再设一次），双重调用可导致 ResourceWarning、连接双关、或内部标志态损坏，影响后续模块重载后的正常运行。  
  修复:在 stop() 函数体内加 with self._lock:，或在 unregister_modules 中 pop 前先检查 owner 归属，使 reload().stop() 只停内建模块、unregister_modules 只停外部模块，两者不交叉。最简单的修复是让 stop() 持锁后再快照：with self._lock: snapshot = list(self._running_modules.items())，然后在锁外调用各模块 stop()（避免持锁期间调用用户代码死锁）。

- **[⚪ LOW] [方法] reload() 在 stop() 与 load_modules() 之间存在分发窗口：已停模块仍留在 _running_modules 中**  
  位置 `app/core/module/manager.py:87-93 (reload), 44-71 (load_modules), 137-146 (get_running_modules)`  
  机制:stop() 调用各模块的 stop() 方法（关闭连接/资源），但不从 _running_modules 中移除它们。在 stop() 返回后、load_modules() 内 self._running_modules = {} 执行前，_running_modules 中存有已停止的模块实例。此窗口期内，FastAPI 后台任务或 APScheduler 定时任务通过 get_running_modules() 可获取这些已停模块并调用其方法，导致方法在资源已释放后被调用。  
  影响:reload 期间的在途请求（如定时刷新任务、API 调用的 Chain 分发）会命中已停模块，方法调用失败后被 on_error 捕获并记录错误，但产生误报性错误事件（EventType.SystemError）、误导日志，增加 reload 期间故障噪声。  
  修复:在 stop() 的每次 module.stop() 调用成功后，同步从 _running_modules 中移除该 module_id（持锁移除）；或在 reload() 首先将 _running_modules 原子置为空字典（加锁），再在后台停止旧实例，最后 load_modules() 填充新实例。

### `chain-subsearch`

- **[⚪ LOW] [边缘漏洞] LLM 原始响应内容未净化直接写入 API 错误消息**  
  位置 `app/chain/search.py:457-483`  
  机制:ValueError 的消息字符串直接嵌入未截断的 ai_response 原文，该字符串经 str(err) 存入类变量后被 API 层原封不动地放入 response.message 返回客户端。如 LLM 服务被投毒或返回精心构造的内容（HTML/script/超长字符串），它会无过滤地抵达前端。  
  影响:若前端对 message 字段使用 innerHTML/v-html 等非转义渲染，可触发 XSS；超长 LLM 响应可被用于带外内存压力攻击；敏感提示词内容被反射到日志和 API 消费者。  
  修复:将 ai_response 截断后再嵌入错误消息（如 ai_response[:200]）；用户层返回通用提示（"AI响应解析失败"），完整内容仅记录到服务端日志。

- **[⚪ LOW] [调用逻辑] _build_ai_recommend_status 以状态查询方法的形式向类变量写入缓存，绕过 hash 校验门**  
  位置 `app/chain/search.py:114-136`  
  机制:get_current_recommend_status_only()（check_only 路径）直接调用 _build_ai_recommend_status 而不做请求 hash 校验。服务重启后 _ai_recommend_result=None、_current_recommend_request_hash=None，但磁盘缓存文件若存在，本方法会将上一会话的索引列表无条件写入类变量并返回 "completed"。此时若搜索结果缓存文件也来自上一会话但结果条数不同，索引将越界或对应错误条目；若来自当前会话新搜索（cancel_ai_recommend 已清除缓存的正常路径），则此条件不可达。  
  影响:极窄的时间窗口（重启后首次 check_only 轮询、在 start_recommend_task 调用之前）返回与当前搜索结果不对应的 AI 推荐索引，前端可能尝试访问不存在的结果条目导致客户端异常；状态查询方法产生隐式写副作用使调用链语义不清。  
  修复:将缓存懒加载逻辑移至 start_recommend_task 的初始化阶段，或在 _build_ai_recommend_status 加载缓存后校验其长度不超过当前 search_results_count；将状态读和缓存填充分离为不同方法。

### `chain-transfer`

- **[⚪ LOW] [调用逻辑] TransferResultProcessor.handle() 中 is_finished() 检查与 _success_target_files.pop() 非原子，多 worker 并发完成同一作业时重复发送入库成功通知**  
  位置 `app/chain/transfer.py:1063-1095, 1258-1267`  
  机制:TransferService 启动 settings.TRANSFER_THREADS 个 worker 线程并发处理队列。当同一剧集的两个文件（task1/task2）分属不同 worker 且几乎同时完成时：worker-A 调用 finish_task(task1)，worker-B 调用 finish_task(task2)；此后两个 worker 各自独立调用 is_finished()（内部持锁，但读完即释放），均得到 True；两者均进入 with job_lock: pop 分支（先到的 pop 到文件列表，后到的 pop 到空列表 []），然后各自调用 _notify_transfer_complete，发出两条入库成功消息。is_finished 检查与 pop 操作不在同一个锁区间内，存在 TOCTOU 窗口。  
  影响:同一部剧集入库时用户收到重复的入库成功推送通知（Telegram/微信等所有渠道）；第二条通知的 file_list_new 为空，可能导致通知模板字段缺失。  
  修复:将 is_finished() 的判断与 pop() 合并到同一个 job_lock 区间内，或在 JobManager 中增加 claim_notification(task) 原子方法（返回 True 表示「本线程获得发送通知的权利」），确保只有一个线程负责发出最终通知。

### `core-infra-event`

- **[⚪ LOW] [边缘漏洞] RedisHelper.deserialize 使用 pickle.loads 反序列化不可信数据，可触发 RCE**  
  位置 `app/core/redis.py:65-75`  
  机制:pickle.loads() 在反序列化时可执行任意 Python 字节码。此函数被 RedisHelper.get() / items() 调用，即每次从 Redis 读取值都会到达该路径。Redis 实例默认无认证（CACHE_BACKEND_URL 默认为 redis://localhost:6379），也无 HMAC 签名验证。攻击者若能写入 Redis（网络暴露的 Redis、SSRF 等），可构造 PICKLE 格式 payload 实现 RCE。  
  影响:Redis 可被网络访问或被 SSRF/注入写入时，读取任意缓存 key 即可触发；成功利用可在服务器进程权限下执行任意代码。  
  修复:1) 移除 pickle 序列化分支，强制仅 JSON（对不可序列化为 JSON 的对象，要求调用方先做 JSON 兼容转换，或改用 msgpack）；2) 若必须保留 pickle，在 set 时对 serialized_value 追加 HMAC-SHA256 签名，get 时先验签后 loads。

- **[⚪ LOW] [边缘漏洞] FileBackend.set/get/exists/delete 未过滤 key 路径，可路径穿越写任意文件**  
  位置 `app/core/cache.py:767, 784, 795, 801`  
  机制:pathlib.Path 在路径拼接时：(a) 若 key 含 '..', 如 '../../cron.d/evil'，实际路径逃逸出 base；(b) 若 key 为绝对路径如 '/etc/cron.d/evil'，pathlib 会完全替换 base，直接指向系统路径。set() 会以 NamedTemporaryFile+replace 原子写入该路径，同时会 mkdir parents，可在任意目录建立文件。AsyncFileBackend（line 884）存在同等问题。  
  影响:若 key 由外部 API 响应（如 TMDB 图片路径）或用户输入派生，攻击者可覆写敏感文件。Docker 部署中可写入 /etc/cron.d/、/root/.ssh/ 等高权限目录。  
  修复:写入前对 key 进行规范化并验证路径仍在 base 内：`resolved = (base / region / key).resolve(); assert resolved.is_relative_to(base.resolve())`，或使用 `Path(key).name` 仅取文件名部分。

- **[⚪ LOW] [架构] MemoryBackend._region_caches/_lock 为类变量，TTL/LRU 类型由首次写入者决定，后来实例静默用错类型**  
  位置 `app/core/cache.py:364-368, 403-411`  
  机制:`setdefault` 的语义是「已存在则返回已有值」。第一个对 region 调用 `set()` 的 MemoryBackend 实例决定该 region 的缓存类型（TTL 或 LRU）；后续 cache_type 不同的实例拿到的是错误类型的缓存对象，且不会报任何错误。具体场景：`TTLCache(region='DEFAULT')` 和 `LRUCache(region='DEFAULT')` 均使用默认 region，若 TTLCache 先写入，则 LRUCache 操作的是 MemoryTTLCache，LRU 逐出策略静默失效，换成 TTL 过期逻辑。同时，单一类锁串行化所有 region 的缓存操作，在高并发下形成全局锁瓶颈。  
  影响:依赖 LRU 逐出语义（无 TTL，仅按访问频率淘汰）的缓存场景在 TTLCache 先启动时静默退化为 TTL 过期，导致热数据意外失效或冷数据无法被及时淘汰，且无任何错误日志。  
  修复:将 `_region_caches` 和 `_lock` 改为实例变量（从 `__init__` 初始化），并在 region key 中编码 cache_type（如 `region:ttl:DEFAULT` vs `region:lru:DEFAULT`）以保证类型隔离；或将 TTL/LRU 两种后端拆为不共享类状态的独立子类。

- **[⚪ LOW] [调用逻辑] EventManager.__handle_event_error 在错误处理中无条件 send_event(SystemError)，SystemError 处理器自身报错时形成无界广播循环**  
  位置 `app/core/event/manager.py:668-689`  
  机制:`__handle_event_error` 是所有事件处理器异常的统一出口，它向 PriorityQueue 投入 SystemError 广播事件。若应用中存在 EventType.SystemError 的订阅处理器，且该处理器本身抛出异常，错误处理器将被再次调用，再次投入 SystemError，形成周期性循环。由于 PriorityQueue 无界（`PriorityQueue()` 默认无 maxsize），循环事件会持续积压，占用内存并消耗 CPU。  
  影响:前提：存在 SystemError 处理器且该处理器有 bug。满足前提后，应用进入事件队列无限增长的内存泄漏模式，直至 OOM 或进程崩溃，且日志会被同类错误日志淹没。  
  修复:在 `__handle_event_error` 中：(1) 判断触发本次错误的事件本身是否已为 SystemError，若是则跳过递归发送；(2) 为 PriorityQueue 设置 maxsize（如 settings.CONF.scheduler * 10），入队时用 put_nowait+Full 异常保护；(3) 或对 SystemError 类型的处理器调用包一层额外的 try/except，不再触发 __handle_event_error。

### `db`

- **[⚪ LOW] [方法] set()/async_set() 用 if value: 而非 if value is not None: 导致合法 falsy 配置被静默删除**  
  位置 `app/db/systemconfig_oper.py:44-47 (set), 80-83 (async_set)`  
  机制:Python 的 if value: 对 0、False、[]、{}、"" 均为 falsy。当调用方显式存储这些合法配置值时，代码走 delete 分支删除 DB 记录。内存缓存 __SYSTEMCONF 仍保留该 falsy 值（line 40/89），导致缓存与 DB 不一致；重启后从 DB 重建缓存，该 key 消失，配置永久丢失。  
  影响:触发条件：任何配置项显式设置为空列表（如清空白名单）、空字典、0 或 False。重启后配置静默丢失，上层功能回退到默认行为，且无任何错误日志。  
  修复:将判断改为 if value is not None:，确保 falsy 但非 None 的合法值正常更新 DB；仅在 value is None 时删除记录（或按业务需求选择保留空记录）。

- **[⚪ LOW] [边缘漏洞] Site.domain 缺少 UNIQUE 约束，SiteOper.add 存在 TOCTOU 导致并发请求可插入重复域名**  
  位置 `app/db/models/site.py:19`  
  机制:应用层的 get_by_domain -> create 是非原子的 check-then-act。DB 层无 UNIQUE 约束兜底。并发两个新增同域名的请求都能通过 get_by_domain 返回 None 的检查，随后各自 INSERT 成功，产生重复记录。get_by_domain 使用 .first() 只返回第一条，后续 update_cookie/update_rss 均操作第一条，第二条成为孤儿数据永远不被更新也不被清理。  
  影响:触发条件：前端或 API 客户端并发/重试新增同一站点。重复记录导致 update_cookie 等操作只更新其中一条，功能不一致；统计数据（SiteStatistic）按 domain 聚合时可能被重复计算。  
  修复:在 Site.domain 列添加 unique=True 并生成 Alembic 迁移（对已有重复数据需先去重）；SiteOper.add 同时处理 IntegrityError 以返回友好错误，而非依赖 TOCTOU 检查。

- **[⚪ LOW] [调用逻辑] SiteOper.update 未对 Site.get 返回 None 做保护，sid 不存在时抛出 AttributeError**  
  位置 `app/db/site_oper.py:74-80`  
  机制:Site.get 在 sid 不存在时返回 None（见 Base.get：scalars().first() 无结果时返回 None）。SiteOper.update 直接在返回值上调用 .update() 方法，无 None 检查。参考同层的 SubscribeOper.update（line 172-179）已正确加 if subscribe: 保护，SiteOper.update 存在一致性缺陷。  
  影响:触发条件：API 路由传入无效 sid，或站点被并发删除后 update 请求仍在途中。AttributeError 向上传播被 FastAPI 捕获为 HTTP 500，而非应有的 404；错误信息包含内部实现细节。  
  修复:添加 None 检查：if site is None: raise ValueError(f'站点不存在: {sid}')（或返回 None 并在调用侧转为 404）。统一与 SubscribeOper.update 的防御性模式。

### `entry-lifecycle`

- **[🟡 MEDIUM] [方法] polling_observer 在所有存储快照均失败时将空快照写入持久化缓存，导致下次轮询触发全量重处理**  
  位置 `app/monitor.py:731-787`  
  机制:当 mon_paths 中所有路径的 snapshot_storage 调用均返回 None（网络抖动、权限错误等），new_snapshot 保持初始值 {}，file_count=0。save_snapshot 将空快照写入 FileCache，覆盖上一次有效快照。下次 polling_observer 执行时，old_snapshot={} 而 new_snapshot 包含实际文件，compare_snapshots 将所有文件判定为「新增」（new_files - {} = all files），触发对存量文件的全量重处理。  
  影响:网络一次瞬断即可导致下次轮询将远端存储的所有媒体文件当作新下载内容重新整理，产生大量重复转移任务；严重时覆盖已整理的目标文件。  
  修复:在保存快照前判断：if not any(snapshot is not None for ...)，即至少有一个路径成功时才覆盖持久化快照；或引入明确的 partial_failure 标志，失败时保留旧快照不做覆盖。

### `modules-contract`

- **[⚪ LOW] [方法] search_movies/search_tvs 在 TMDB 返回 null title/name 时抛 TypeError**  
  位置 `app/modules/themoviedb/tmdbapi.py:59, 76, 1739, 1756`  
  机制:TMDB API 偶尔会在搜索结果中返回 title/name 字段缺失的条目（常见于未正式发布的影片）。`movie.get("title")` 返回 `None` 时，`title in None` 触发 `TypeError: argument of type 'NoneType' is not iterable`。该异常未被 try/except 包裹，会直接逃出方法并沿调用链传播到 `search_medias`，导致整次搜索失败。同步与异步两个版本（search_movies/search_tvs 及 async_search_movies/async_search_tvs）均存在相同问题。  
  影响:触发条件：TMDB 返回 title 或 name 字段为 null 的搜索结果，在高峰期/API 数据不完整时可复现。后果：调用 `search_medias` 的所有路径（手动搜索、媒体识别管道、订阅匹配）抛出未处理异常，dispatch 层记录 SystemError 并广播错误事件，该次识别/搜索请求整体失败。  
  修复:将判断改为空值安全形式：`if title in (movie.get("title") or ""):` 和 `if title in (tv.get("name") or ""):` ，对 async 变体同步修改。

---

## 九、对抗复核被剔除项(6)

> 透明记录:以下发现经复读代码后被证伪 / 判为 by-design / 已修,**不应再当 bug**。

- **reload_plugin() 吞掉 register_plugin() 异常，插件处于'有实例无路由/调度/命令'的静默降级态**(`app/helper/plugin_manager.py`)  
  剔除理由:复读了 app/helper/plugin_manager.py:639-660（reload_plugin）、app/api/endpoints/plugin.py:145-154（register_plugin）、:425-433（API 端点）、:415-419（文件监控调用方），并比对了 commit db1ef1bc(#36) 的 reload_plugin diff。代码证据属实：register_plugin 异常被 try/except 捕获后仅 logger.error，无 re-raise/rollback/stop，send_event 在 try 内 register_plugin 之后。但这恰是 P1 #36（注入的 already-fixed 清单项「热重载内聚重建后再广播」）刻意产出的代码——#36 前是裸 send_event，#36 故意把 register_plugin 移入 try、把广播挪到绑定成功之后，commit 明确写「reload 同步绑定失败后仍发事件→改为绑定成功后再广播」且经对抗式复核。因此发现的两条 load-bearing 主张被证伪：(1) 标题「静默降级」不成立——失败路径有服务端 logger.error，非静默；(2) 「PluginReload 事件未发出」被当作缺陷，实则是 #36 的刻意设计（绑定不全时不应发出「已重载」信号，否则下游消费者误判）。catch-and-log 而非 re-raise 是 #36 经评审的防御性取舍：单个插件 get_service/get_command/get_api 故障不应拖垮文件监控的多插件重载循环（该循环 :416-419 已自带 try/except，re-raise 也只是另一条日志）或让 reload API 500。真正残留仅一处低危观测缺口——API 端点对单插件部分绑定失败仍返回 success=True 且无回滚；触发需插件自身 bug，仅影响该插件，服务端有日志可查。综合：证据属实但核心机制为 #36 刻意设计且已治理，自评 medium 偏高，校正为 low。

- **run_plugin_migrations PostgreSQL 路径 engine.connect() 未 commit，CREATE SCHEMA + 全量迁移静默回滚**(`app/db/plugin_migration.py`)  
  剔除理由:证据中引用的代码属实（app/db/plugin_migration.py:71-76 确实用 `with bundle.engine.connect()` 且块内无显式 connection.commit()，与 manager.py:195/213 的 `engine.begin()`（自动提交）写法不一致）。但发现的核心机制与影响结论是错误的，问题不成立。

复读链路：
1) app/db/plugin_migration.py:50-76 —— PLUGIN_ALEMBIC_ENV 模板（:33-34）执行 `with context.begin_transaction(): context.run_migrations()`。
2) app/db/manager.py:72-74 —— `_pg_create_schema_ddl` 确实在 connect 后、upgrade 前执行，PG 路径触发 autobegin（环境实测 in_transaction=True）。

实测/源码反驳点（sqlalchemy 2.0.49 + alembic 1.16.5）：
- 发现声称「Alembic 收到已有活跃事务的 connection，其 begin_transaction() 创建 SAVEPOINT」——错误。alembic.util.sqla_compat._safe_begin_connection_transaction 的逻辑是 `t = connection.get_transaction(); return t if t else connection.begin()`，即【复用已存在的根事务 T】而非新建 SAVEPOINT/嵌套事务。
- 发现声称「with 块退出无 commit → ROLLBACK T → 全部 DDL 撤销」——错误。Alembic 用 `_ProxyTransaction` 包裹被复用的根事务 T；env 模板 `with context.begin_transaction()` 正常退出时，_ProxyTransaction.__exit__ 调 T.__exit__(None,None,None)，进入 sqlalchemy.engine.util.TransactionalContext.__exit__ 的 `if type_ is None and self._transaction_is_active(): self.commit()` 分支 —— 根事务 T（含 CREATE SCHEMA + 全部迁移 DDL + alembic_version）被【提交】。之后 `with engine.connect()` 关闭时 T 已提交，close() 的兜底 rollback 仅作用于其后可能 autobegin 出的空只读事务，不影响已提交 DDL。

两次隔离环境真机复现（真实 alembic command.upgrade）：先执行一条预语句触发 autobegin，再经同一 connection 跑 upgrade，with engine.connect() 块退出后用全新连接查询——widgets/alembic_version/预语句标记表【全部持久化】。第二次更将 SQLiteImpl.transactional_ddl 强置为 True，精确走 PostgreSQL 的 transactional_ddl=True 最终 else 分支（与 PG 完全同一代码路径），结果同样持久化。

结论：PG 路径下 schema 与迁移表会被正常提交，不会出现「relation does not exist」。该 high 级缺陷不成立——属对 SQLAlchemy autobegin 与 Alembic begin_transaction 复用-提交语义的误判。不在 by-design 清单，也未被 P0/P1/P2 修复（无需修复）。补记：现有 connect()/begin() 写法不一致可作 LOW 级风格统一建议，但无功能性后果。

- **GET /system/global 用硬编码字面量 'moviepilot' 替代标准认证框架，形成伪鉴权门**(`app/api/endpoints/system.py`)  
  剔除理由:复读 app/api/endpoints/system.py:649-676（get_global_setting）及邻接的 :679-682（get_user_global_setting），并查 git blame/commit。证据属实：确为 `if token != "moviepilot": raise HTTPException(403)`。但这是刻意设计的"登录前 UI 初始化"公开端点，非被破坏的鉴权边界：(1) 返回值由显式白名单 `settings.model_dump(include={"TMDB_IMAGE_DOMAIN","GLOBAL_IMAGE_CACHE","ADVANCED_MODE"})` + 前后端版本号构成，全部非敏感，DEV 模式才追加 BACKEND_DEV；(2) 存在刻意的双端点拆分——`/global/user` 用真正的 `Depends(get_current_active_user_async)` 鉴权返回用户态/业务设置，`/global` 专供尚无 JWT/会话的登录前阶段，标准 verify_token 框架在此结构上不可用；(3) git 历史显示该端点由上游维护者 jxxghp 在 commit b50599b7（提交信息 "fix：增加安全性"）从"仅本地调用"改为静态 token 守卫，是上游 v2 刻意的安全决策，未被 P0/P1/P2 重构触及。"moviepilot" 是公开的前端标识常量而非密钥，出现在源码/访问日志中不泄露任何东西，因为端点本就刻意公开且载荷非敏感。白名单 include={} 机制意味着未来新增字段不会被自动暴露，需维护者显式 opt-in，这恰好反驳了发现所依赖的"先例/未来扩展"论点——该论点纯属推测性滑坡，当前实际安全影响为零。属"等价的刻意设计"，故 refuted，corrected_severity=none。

- **GlobalVar.is_transfer_stopped 无锁 check-then-remove，并发调用导致 ValueError**(`app/core/config.py`)  
  剔除理由:复读 config.py:1187/1282-1291 确认字面证据属实：EMERGENCY_STOP_TRANSFER 是无锁普通 list，is_transfer_stopped 确为非原子的 in→remove 两步操作，且 SUBSCRIPTIONS 有 SUBSCRIPTIONS_LOCK、本列表无锁。但断言的崩溃机制不可达，故 refuted：(1) 触发条件要求"两线程并发检查同一 path"——复读 transfer.py:154-218 jobview.add_task 在 job_lock 下对同一源文件 file_key 跨所有作业去重（注释 166 明示），重复任务直接返回 False 不入队（1492-1493）；transfer.py:769/797 每个 worker 从队列取互异任务，一个源 path 只由一个 worker 在途处理，在途期间去重阻止再入队。因此同一 path 的两次 is_transfer_stopped 并发 remove 不会发生；stop_transfer 的 append 与 remove 在 CPython 下各自 GIL 原子，append/remove 交错不产生 ValueError，ValueError 仅来自同 key 的二次 remove。(2) 影响被夸大：即便触发，所有存储调用点均把 is_transfer_stopped 包在 try/except Exception 内（local.py:205-225 捕获后 return False、alist.py:679-690 捕获后 return），是被记录的单文件失败，而非"未捕获异常致整理线程崩溃、任务静默丢失"。这是单进程多线程问题（非多 worker），但可达性仍因跨作业去重而不成立。残留仅为与 SUBSCRIPTIONS 不一致的 low 级代码异味（EMERGENCY_STOP_WORKFLOWS 同样无锁，属既定约定），非所报 medium 并发崩溃。未在 P0/P1/P2 修复，也不在注入的 by-design 清单。

- **EventManager.__parse_handler_names 对顶层函数触发 IndexError，链式事件调用方崩溃**(`app/core/event/manager.py`)  
  剔除理由:复读 app/core/event/manager.py:528-536（__parse_handler_names）、98-114（send_event）、310-363（__trigger_chain_event/__dispatch_chain_event）、429-439（__safe_invoke_handler 无 try/except）、453-505（__invoke_handler_by_type_sync，parse 在 try/except 之前调用）、396-427（__dispatch_broadcast_event，line 427 executor.submit 吞 future 异常）、691-724（register）、135-171（add_event_listener）。代码层面证据属实：顶层函数 __qualname__ 无点，split('.') 仅 1 元素，names[1] 必抛 IndexError，且链式路径无单 handler try/except、广播路径 future 静默吞异常。但"合法用法触发"这一影响判断被证伪：整个事件架构以 qualname→ClassName.method 的约定式分发为刻意契约——__invoke_handler_by_type_sync 全凭 class_name 路由到 plugin_manager/module_manager/__get_class_instance(class_name)，顶层函数无类名，即便加固 parse 也永远无法被分发（__get_class_instance 会按函数名找模块、返回 None）。这等价于注入清单中"字符串方法名分发=刻意契约"的设计。可达性核查：全仓 10 处 @eventmanager.register 全部是 4 空格缩进的类方法，零顶层/模块级函数注册（零缩进 grep 无命中）。故 register(EventType.XYZ)(top_level_func) 既无任何现存调用，也被架构契约排除，触发面实质为零。充其量是不可达路径上的防御式硬化（IndexError 文案不友好），自评 medium 过高，校正为 low。

- **TmdbCache.save() 在无锁状态下读取 _cache 并写文件，与并发 update()/clear() 存在竞态**(`app/modules/themoviedb/tmdb_cache.py`)  
  剔除理由:复读了 app/modules/themoviedb/tmdb_cache.py:138-159（save/update/clear/get）与 app/core/cache.py:360-491（MemoryBackend）、1255-1360（CacheProxy/TTLCache）。行级证据属实：save() 确实不持有模块级 lock，而 update()/clear()/get() 持有。但核心竞态机制(a)被证伪：save() 中的 self._cache.items() 经 CacheProxy.items()→MemoryBackend.items()(cache.py:475-491)，在类级 self._lock 内 list(region_cache.items()) 完整快照化后才 yield，而 set()(404)/clear()(466)/delete()(451) 均持有同一 self._lock。因此 save() 的字典推导式消费的是稳定快照，不存在"TTLCache 内部 dict 遍历中被并发改写"的隐患；发现误以为 items() 直接遍历活动 dict。机制(b)文件写入虽确实无锁，但：①快照内部一致（每条目由 update() 在锁内整体原子写入，不会撕裂），写点时快照是正常周期 flush 语义而非脏数据；②并发 save() 需两个 save() 同时触发，实际调用者仅 scheduler_job（10 分钟周期、单作业、亚毫秒执行不自重叠）、stop()（仅关停）、__del__（WeakSingleton 被 TheMovieDbModule.cache 强引用，仅进程拆除时 GC），正常运行下并发不可达；③即便写撕裂，__load()(97-104) try/except 捕获并返回 {}，缓存从 API 重建，不会出现所声称的"错误识别结果被持久化"。非 by-design 清单项，亦未在 P0/P1/P2 修复（均未触及 tmdb_cache.py）。残留仅为极低概率且自愈的无锁文件写代码异味，故校正为 low，原 medium 的字典损坏/数据投毒机制与影响不成立。

---

## 十、附录:与历史审计的衔接

- 本轮为 P0/P1/P2 整改合入主线后的**复审**,刻意不重复已修项与已判不可行项(见 §二)。
- **P2 遗留 #9 / #38**(声明式作业表 / 通知扩展点登记表)仍为"行为保持约束下不可行",本轮未推翻该结论。
- 本报告的 `needs-scoping` 与 `confirmed` 共 52 条构成下一轮整改 backlog;建议优先级见 §一(最值得做的三件事)与 §六(推进方案):**并发/安全正确性缺陷 → 信任边界收口 → 结构债重构**,切勿在已知竞态之上先做大重构。
