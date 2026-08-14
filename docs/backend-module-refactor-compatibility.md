# 后端模块重构与旧导入路径兼容层设计

> 状态：设计提案。当前依赖基线取自 `v3` 分支提交 `895635c27792` 的 AST 静态扫描；实施前应在最新代码上重新生成。

## 1. 背景与目标

MoviePilot 后端计划重新划分 `core`、`helper`、`utils` 等目录的职责，消除反向依赖和循环导入，同时不能要求数量众多、版本不一的插件同步修改既有导入语句。

本方案同时解决两个问题：

1. **主程序内部结构治理**：主程序代码只使用新的规范路径，并通过静态依赖门禁维持单向依赖。
2. **插件导入兼容**：插件仍可通过旧路径导入相同对象，旧路径不需要保留同名 Python 文件。

兼容层是插件 ABI 的适配边界，不是主程序内部绕过分层规则的工具。导入成功不代表依赖方向合理；主程序一旦迁移到新路径，禁止再新增或保留旧路径引用。

### 1.1 设计目标

- 旧插件不修改源码即可继续加载。
- 旧业务模块和新路径导入得到同一个模块对象、类对象、单例和模块级状态；仅用于路由的合成父包除外。
- 物理源码只保留在新位置，不在旧目录生成大量转发文件。
- 映射按需加载，不因安装兼容层而预导入全部目标模块。
- Debug 模式下明确提示插件仍在使用的旧路径，生产环境不产生兼容警告噪声。
- 兼容行为可观测、可测试、可分批上线、可快速停用或回滚。
- 不改变插件热重载、动态导入、事件注册和打包发布的既有语义。

### 1.2 非目标

- 不用导入钩子掩盖新的循环依赖。
- 第一阶段不同时进行模块搬迁和插件公开符号改名。
- 不支持模糊匹配、正则猜测或任意旧路径重写。
- 不代理第三方包、`app.plugins.*` 或插件自己的相对导入。
- 不承诺所有 `app.*` 内部对象永久都是插件公共 API；长期公共接口应逐步收敛到 `app.sdk`。

## 2. 当前问题基线

对当前源码的静态导入图检查显示，`core`、`helper`、`utils` 并不是单向分层：

| 依赖方向 | 静态导入边数量 |
| --- | ---: |
| `core -> helper` | 9 |
| `helper -> core` | 46 |
| `utils -> core` | 5 |

当前还存在一个至少包含以下模块的强连通分量：

```text
app.core.cache
app.core.event
app.core.module
app.core.plugin
app.helper.message
app.helper.plugin
app.helper.redis
app.helper.server
app.utils.mixins
```

因此不能简单地把文件移动到新目录后依赖兼容钩子维持运行。正确顺序是先定义职责和依赖方向，拆开运行时环，再移动模块并为插件保留旧导入 ABI。

插件仓库也有大量直接导入旧目录的代码。高频入口包括 `app.core.config`、`app.core.event`、`app.utils.http`、`app.utils.string`、`app.core.context` 和 `app.core.metainfo`。兼容必须在插件首次加载之前全局可用，不能依赖逐个插件适配。

## 3. 目标架构

目录名称应表达职责，而不是继续维护三个边界含混的公共杂物目录。建议的目标依赖方向如下：

```text
Entrypoints / Plugins --> Application / Chain --> Domain + Ports --> Foundation
          |                       ^                     ^
          v                       |                     |
 Runtime Composition ------------+----> Infrastructure / Adapters / Persistence
```

其中 ports 由领域/应用侧定义，infrastructure 负责实现；运行时装配层选择实现并注入应用层，领域层不能反向导入 infrastructure。

建议按现有项目模式渐进引入以下边界，最终名称可在首批迁移评审时确定：

| 目标包 | 职责 | 允许依赖 |
| --- | --- | --- |
| `app.foundation` | 无业务状态的通用结构、字符串、URL、限流等基础能力 | 标准库、第三方库、同层代码 |
| `app.domain` | 媒体上下文、值对象、纯领域规则和协议定义 | `foundation` |
| `app.application` / `app.chain` | 用例编排、跨模块业务流程 | `domain`、ports、`foundation`；基础设施实现由 runtime 注入 |
| `app.infrastructure` | HTTP、Redis、文件系统、外部服务适配、运行时实现 | `foundation`、`domain`、ports |
| `app.runtime` | 配置、事件总线、模块/插件生命周期、启动装配 | 上述各层；其他低层不得反向依赖它 |
| `app.sdk` | 明确承诺给插件使用的稳定类型、事件和服务门面 | 只依赖稳定协议或受控门面 |
| `app.compat` | 旧导入路径兼容机制和声明式映射 | 仅 Python 标准库；不能导入业务目标模块 |

这不是要求一次性创建所有目录。每个迁移批次只新增实际需要的目标包，并以依赖方向而不是文件数量作为完成标准。

### 3.1 重点拆环原则

- 配置重载 mixin 不应在底层模块导入全局事件单例。可改为由 `runtime` 装配阶段注册监听，或只依赖一个事件订阅协议。
- 事件总线不应通过类名猜测 `core`、`chain`、`helper` 路径并动态实例化对象。处理器注册时应携带明确的实例解析器，或由插件/模块管理器在装配阶段注册。
- 模块和插件管理器可以依赖插件安装、服务报告等接口，但不能直接依赖包含完整业务流程的 helper 实现。实现应注入或在更高层编排。
- 缓存抽象与 Redis 实现分离：缓存协议/本地缓存位于低层，Redis 是 infrastructure adapter，运行时选择具体实现。
- 消息通知失败处理不能从底层事件总线直接反向调用消息业务实现，应发布结构化错误事件，由上层订阅者处理。

### 3.2 现有目录的候选归属

下表用于指导逐文件评审，不是最终路径映射。一个现有文件同时承担多种职责时必须先拆分，不能为了减少改动把整份文件直接换目录。

| 现有内容 | 候选归属 | 搬迁前置条件 |
| --- | --- | --- |
| `core.context`、`core.meta*`、`core.metainfo` | `domain.media` / `domain.parsing` | 去除对全局 settings 和基础设施的直接读取，运行参数显式传入 |
| `core.config` | `runtime.config` | 先把纯路径、URL、系统操作下沉到 foundation/infrastructure，避免 runtime 被低层反向导入 |
| `core.event` 中的 `Event` 契约 | `domain.events` 或稳定 SDK contract | 与事件队列、线程、处理器实例解析分离 |
| `core.event` 中的 EventManager | `runtime.events` | 移除按类名猜路径及直接实例化 PluginManager/ModuleManager/MessageHelper |
| `core.module`、`core.plugin` | `runtime.extensions` | 安装、发现、生命周期和业务上报通过接口/装配连接 |
| `core.cache` | `foundation.cache` + `infrastructure.cache` | 拆出无 I/O 缓存算法、Redis adapter 和运行时选型 |
| `utils.string/url/identity/coalesce/structures` 等纯函数 | `foundation` 对应领域文件 | 确认不读取全局配置、不执行 I/O、不导入高层模块 |
| `utils.http/web/rust_accel/system/stdio` 等 | `infrastructure` | 将协议/返回类型留在低层，具体客户端和系统调用放适配器 |
| `utils.mixins` | 按能力拆分，配置重载部分归 `runtime` | 消除 mixin 对全局事件单例的导入期注册 |
| `helper.redis/browser/doh/display/thread/package` 等 | `infrastructure` | 生命周期由 runtime 装配，不在适配器内部反向获取管理器 |
| `helper.downloader/mediaserver/service/module` | ports + `application.services` + adapter | 分离服务协议、模块选择/业务门面和具体实现 |
| `helper.message/notification/interaction/server` | `application` 与外部 adapter 分拆 | 业务编排不能留在 infrastructure，外部请求不能留在 domain |
| `helper.torrent/audio/directory/format/nfo/rule/scraper` | `domain` 纯规则 + `application` 用例 + I/O adapter | 逐函数区分纯转换、业务流程和文件/网络访问 |
| `helper.sites` 与二进制资源 | `infrastructure.sites` + 独立资源目录 | 完成 Build、Resources、Docker、本地安装的跨仓同步迁移 |

`app.chain` 已经承担 application orchestration，可继续保留，不必仅为追求目录命名整齐而整体改名。`app.modules` 继续作为可插拔 adapter 集合，但模块间编排仍由 chain/application 完成。

## 4. 兼容层总体方案

### 4.1 为什么使用导入钩子

每个旧模块保留一个转发文件虽然简单，但会留下大量虚假目录和文件，容易被主程序继续误用，也需要维护重复的 `__all__`、模块元数据和符号转发。统一导入钩子更符合“源码只存在于新位置”的目标。

兼容层使用 Python 标准导入协议：

- 一个 `MetaPathFinder` 仅匹配声明过的旧路径；
- 一个 `Loader` 在真正命中旧路径时按需导入目标模块；
- 加载完成后让旧路径和新路径指向同一个模块对象；
- 映射表是代码仓内唯一事实来源，并经过启动前校验和测试。

### 4.2 建议目录

```text
app/
  compat/
    __init__.py
    imports.py       # Finder、Loader、安装和卸载入口
    manifest.py      # 不导入业务模块的静态映射数据
    diagnostics.py   # Debug 诊断与插件源码扫描
  sdk/
    __init__.py
    events.py
    media.py
    services.py
```

`app.compat` 自身只使用标准库，尤其不能导入 `settings`、logger、事件总线、插件管理器或映射目标。`app/__init__.py` 只负责无业务依赖地安装钩子；配置初始化完成后、插件加载前，再由启动装配代码调用类似 `configure_diagnostics(enabled=settings.DEBUG, emit=logger.warning)` 的入口注入 Debug 状态和日志回调，避免兼容层再次进入当前依赖环。

### 4.3 声明式映射

映射必须精确到完整模块路径，并携带治理元数据：

```python
MODULE_ALIASES = {
    "app.core.event": ModuleAlias(
        target="app.runtime.events",
        introduced="3.x.y",
        owner="runtime",
    ),
    "app.utils.http": ModuleAlias(
        target="app.infrastructure.http",
        introduced="3.x.y",
        owner="infrastructure",
    ),
}
```

以上路径只展示映射格式，不代表已经确定这些模块的最终归属。正式映射必须在对应领域完成依赖拆分和所有权评审后加入。

约束如下：

- 旧路径和目标路径都必须是完整绝对模块名。
- 不允许 `app.core.* -> app.runtime.*` 这类通配规则自动覆盖未知模块。
- 旧路径不能仍有真实 `.py` 文件，避免标准查找器绕过兼容 Finder。
- 目标不能再指向另一个旧路径；启动校验应将别名链视为错误。
- 一个旧模块只能映射到一个目标模块。
- 多个旧模块只有在历史上本就代表同一公共模块时才能映射到同一目标。
- 物理搬迁阶段保持插件可见符号名称不变；符号改名另行显式登记，不能由 `__getattr__` 猜测。

建议同时维护机器可读的兼容清单，CI、文档生成和插件扫描均读取同一数据源，不再维护第二份路径列表。

### 4.4 模块身份必须唯一

兼容层的核心不只是“能导入”，而是保证实际承载业务对象的模块身份一致：

```python
import app.core.event as legacy
import app.runtime.events as canonical

assert legacy is canonical
assert legacy.Event is canonical.Event
```

如果分别执行同一份源码生成两个模块对象，会产生严重问题：

- `isinstance` 对同名类判断失败；
- 单例元类在两个模块命名空间各创建一个实例；
- 装饰器、事件监听器和模块级缓存重复注册；
- pickle、Pydantic 类型路径、日志和调试信息不一致。

Loader 因此不能在旧模块名下再次 `exec` 目标源码，而应导入 canonical 模块，并将旧键绑定到该对象。实现需要覆盖“先导入旧路径”和“先导入新路径”两种顺序，以及并发导入时的锁语义。

建议的加载算法是：

1. Finder 精确命中旧业务模块并返回 alias spec。
2. Loader 的 `create_module()` 在 Python 导入锁内导入 canonical 模块并返回该对象。
3. `exec_module()` 不重复执行目标源码，只校验 canonical 模块已经完整初始化。
4. 导入结束后 `sys.modules[legacy]` 与 `sys.modules[canonical]` 指向同一对象；canonical 的 `__name__`、`__spec__` 和 `__package__` 不被旧路径覆盖。
5. 兼容层独立记录 legacy 名称用于诊断，不把旧身份写回 canonical 模块。

实现阶段必须用目标 Python 版本验证上述元数据行为；如果自定义 Loader 无法在所有支持版本上保持 canonical spec，允许改用等价的受锁 `sys.modules` alias 实现，但仍禁止二次执行源码。

### 4.5 包和子模块处理

模块别名存在父包导入语义。例如导入 `app.core.meta.words` 时，Python 会依次处理父包。采用以下规则：

1. 优先逐个登记实际被插件使用的叶子模块。
2. 旧父包仍有物理 `__init__.py` 时沿用该父包；目录完全迁空后，由 Finder 创建 `__path__` 为空的合成兼容包，不保留散落的物理转发文件。
3. 合成父包只是路由容器，不承载业务状态，不要求与 canonical 父包是同一对象；实际叶子模块和公开符号仍必须保持 canonical 身份。
4. 旧包 `__init__.py` 曾公开导出的符号，要在 manifest 中登记精确的包级符号映射，由合成包惰性解析。
5. 不把 canonical 包的真实文件系统 `__path__` 暴露给旧包，否则标准 `PathFinder` 可能把未登记的新子模块以旧名称再次执行。
6. 兼容层不得根据目标包文件系统自动开放未登记的新内部模块给旧命名空间。
7. 测试必须覆盖 `from old.package import child`、`from old.package import PublicName`、`import old.package.child`、`find_spec()` 和相对导入。

兼容承诺覆盖 Python 模块协议下的普通 `import`、使用常量旧路径的 `importlib.import_module()`，以及 pickle 等通过模块名重新导入公开符号的场景。以下行为不由通用钩子模拟：

- 按旧模块 `__file__` 拼接数据文件路径；
- 用 `pkgutil.iter_modules()` 或旧包 `__path__` 枚举已经迁走的内部文件；
- 通过绝对磁盘路径直接加载已经删除的旧 `.py` 文件；
- 依赖旧模块 repr、traceback 或对象 `__module__` 永久保持旧名称。

这类插件如确属有效公共用例，应迁到 `app.sdk` 的资源/发现 API，或增加经过评审的专用 adapter，不能扩大通用 Finder 的文件系统伪装范围。

### 4.6 安装时机

钩子应在 `app/__init__.py` 最早期安装，早于 `app.factory`、启动生命周期、模块初始化和 `PluginManager.start()`。安装过程必须满足：

- 幂等，多次调用只保留一个 Finder；
- 放在 `sys.meta_path` 中标准 `PathFinder` 之前，但只拦截白名单旧路径；
- 不预导入映射目标；
- 提供仅供测试使用的卸载和状态复原能力；
- 安装失败应在启动阶段明确失败，不能等某个插件加载后才随机暴露。

Finder 在诊断回调尚未配置时仍可暂存命中的旧路径和调用模块；`configure_diagnostics()` 完成后仅刷新能够确认来自插件/扩展的记录。这样不需要在 `app/__init__.py` 导入配置，又不会漏掉非常早期的插件式扩展导入。

不建议只在 `PluginManager` 中临时安装钩子。主程序启动、CLI、脚本、插件依赖扫描和测试都可能在插件管理器初始化前导入旧路径。

## 5. Debug 模式旧路径警告

### 5.1 运行时警告行为

当 `settings.DEBUG` 为真且兼容 Finder 命中旧路径时，记录一次 Debug 兼容警告：

```text
[兼容导入] 插件 AutoSignIn 使用旧路径 app.utils.http，已映射到 app.infrastructure.http；请迁移到 app.sdk.http
```

警告应包含：

- 旧模块路径；
- 当前实际目标路径；
- 推荐的插件稳定路径；
- 能识别时的插件 ID 或触发模块；
- 兼容规则引入版本。

日志级别建议用 `WARNING`，但仅在 `settings.DEBUG=true` 时启用。`DEV` 仍只控制热重载等开发行为，不作为本兼容警告的开关；本地启动脚本目前会同时打开两者，但实现和测试必须保持语义独立。

去重键使用 `(plugin_id, legacy_module)`；同一插件同一路径每个进程只提示一次。插件热重载清理 `app.plugins.<id>` 时不清除这份诊断去重集合，避免每次保存文件都重复刷屏。测试可以显式清空诊断状态。

### 5.2 为什么还要做插件源码扫描

运行时钩子本身不能完整识别所有旧引用：如果目标旧模块已经由另一个插件加载并存在于 `sys.modules`，后续插件导入可能直接命中缓存，不再调用 Finder。只靠运行时钩子会漏报。

因此在 DEBUG 模式下，`PluginManager` 导入插件前应对该插件的 Python 文件执行一次轻量 AST 扫描：

- 识别 `import app.core.xxx`、`from app.core.xxx import Name`，以及 `from app.core import xxx` 这类包级写法；
- 对照同一份兼容映射表生成警告；
- 报告插件 ID、文件相对路径和行号；
- 结果按文件修改时间或内容摘要缓存；
- 解析失败只警告，不阻止插件加载；
- 不执行插件源码，也不通过正则猜测 Python 语法。

动态字符串导入如 `importlib.import_module(variable)` 无法全部静态识别，仍由运行时 Finder 兜底。两种诊断共享去重/聚合器，避免产生重复日志。

### 5.3 不要使用 `DeprecationWarning` 作为唯一通道

Python 默认通常隐藏 `DeprecationWarning`，并且难以稳定带出插件 ID。兼容层可以额外调用标准 `warnings.warn()` 方便测试或 IDE 捕获，但 MoviePilot 的 DEBUG 日志警告才是插件开发者可依赖的诊断通道。

### 5.4 生产环境行为

- 兼容映射继续生效；
- 不扫描插件源码；
- 不输出旧路径警告；
- 可维护内部计数，但不得产生高基数日志或遥测；
- 兼容导入失败仍按普通 `ModuleNotFoundError`/`ImportError` 记录真实错误。

## 6. 插件公共接口策略

导入兼容可以保证旧插件继续运行，但不能让插件永久依赖重构后的内部目录。应建立 `app.sdk` 作为新的插件稳定入口：

```python
from app.sdk.events import Event, EventType, eventmanager
from app.sdk.media import MediaInfo, MetaInfo
from app.sdk.services import RequestClient
```

`app.sdk` 的原则：

- 只暴露有兼容承诺的对象；
- 不通过 `from internal_module import *` 无限制导出内部实现；
- SDK 门面尽量依赖协议和稳定数据结构；
- 新插件文档只展示 `app.sdk` 路径；
- 旧路径映射到当前 canonical 实现，但警告中的推荐路径优先指向 `app.sdk`；
- 将来内部位置再次变化时，只维护 SDK 门面和映射目标，不要求插件再迁移。

第一阶段不强制现有插件改为 SDK；官方插件可在后续常规版本中逐步消除警告。

## 7. 符号级兼容

模块搬迁和符号改名应拆成不同批次。绝大多数迁移只做模块别名，并保持原有类/函数名称。

确实需要改名时，使用显式的符号映射：

```python
SYMBOL_ALIASES = {
    ("app.core.context", "MediaInfo"): SymbolAlias(
        target_module="app.domain.media",
        target_name="MediaDescriptor",
    ),
}
```

符号兼容仅支持 `from old.module import OldName` 和 `old_module.OldName` 等明确场景，并保证返回 canonical 对象。禁止自动遍历、相似名称匹配或静默参数转换。构造参数/返回值契约发生变化时，应增加真正的 adapter，并单独评审行为兼容性。

## 8. 分阶段迁移计划

### 阶段 0：冻结基线和生成清单

- 用 AST 构建主仓和官方插件仓库的导入图。
- 输出 `core/helper/utils` 的强连通分量、反向边和插件使用频率。
- 区分插件公共契约、主程序内部实现和资源二进制落点。
- 为首批迁移建立精确的 `old -> canonical -> sdk` 清单。
- CI 保存依赖图摘要，后续批次不得增加反向边或新环。

### 阶段 1：先落兼容基础设施

- 新增只依赖标准库的 `app.compat`。
- 在 `app/__init__.py` 最早安装 Finder。
- 实现 DEBUG 运行时告警与插件 AST 扫描。
- 映射表先为空或只放一个无副作用的试点模块。
- 完成模块身份、并发导入、包语义、诊断去重和状态恢复测试。

### 阶段 2：拆环，不急于搬所有文件

- 优先拆 `event/module/plugin/cache/redis/message/mixins` 强连通分量。
- 用协议、注册表和 startup composition root 替代类名猜测及底层反向实例化。
- 每拆一条环都增加静态依赖测试和相关生命周期测试。
- 此阶段允许部分文件暂留旧目录，但主程序新代码必须遵守目标依赖方向。

### 阶段 3：按垂直批次搬迁

每个 PR 只处理一个可独立验证的领域，例如“HTTP 基础设施”或“媒体领域模型”：

1. 在新位置建立 canonical 模块。
2. 将主程序、脚本和测试切换到新路径。
3. 删除旧物理源码文件。
4. 添加旧路径映射。
5. 验证旧插件导入、主程序新导入和对象身份。
6. 更新依赖图，确认没有新增 SCC。

不要在一个 PR 中同时移动几十个不相关模块。Git 能识别文件移动，但运行时副作用、插件 API 和依赖方向必须逐域验证。

### 阶段 4：建设 SDK 并迁移官方插件

- 从实际高频插件入口开始建立 `app.sdk`。
- 更新插件开发文档和模板。
- 官方插件在正常版本发布中逐步改用 SDK；第三方插件继续由兼容层支持。
- DEBUG 扫描报告可输出剩余旧路径统计，用于安排迁移优先级。

### 阶段 5：兼容策略长期维护

- V3 生命周期内默认不删除已发布的旧路径映射，除非明确宣布大版本破坏性变更。
- 映射只能新增或纠正目标，不随内部清理随意删除。
- 删除规则前必须确认官方插件仓库、已知第三方插件样本和文档均已迁移，并经过至少一个明确弃用周期。

## 9. 静态门禁和测试

### 9.1 主程序依赖门禁

新增 AST 级测试或独立脚本，至少检查：

- `app.compat` 不导入任何 MoviePilot 业务模块；
- `app.foundation` 不导入 `domain/application/infrastructure/runtime`；
- `domain` 不导入 `application/infrastructure/runtime`；
- 低层不导入 `PluginManager`、`ModuleManager` 等运行时实现；
- `app/` 主程序代码不再导入已登记的旧路径，插件目录除外；
- 导入图不存在新增强连通分量；
- 映射目标真实存在，旧物理文件不存在，无别名链和重复冲突。

检查应解析 AST，不用文本正则替代 Python 导入语义。

### 9.2 兼容层单元测试

- 新路径先导入、旧路径后导入；
- 旧路径先导入、新路径后导入；
- `import old.module` 和 `from old.module import Name`；
- 模块、类、单例、枚举身份一致；
- 模块初始化副作用只执行一次；
- 多线程并发导入不会得到半初始化模块；
- 物理父包和合成父包下的子模块、包级公开符号、相对导入、`find_spec()` 行为正确；
- 未登记的新子模块不能通过旧合成父包的 `__path__` 泄漏或被重复执行；
- 未登记的旧路径仍抛出正常 `ModuleNotFoundError`；
- Finder 对第三方包和 `app.plugins.*` 零干扰；
- 安装幂等，测试卸载后完整恢复 `sys.meta_path`、`sys.modules` 和诊断状态。

### 9.3 Debug 警告测试

- DEBUG=false 时不扫描、不告警，即使 DEV=true 也一样；
- DEBUG=true 时告警包含插件、旧路径、新路径和推荐 SDK 路径；
- 同一插件同一路径只告警一次；
- 不同插件使用同一路径分别可见；
- 模块已在 `sys.modules` 时，AST 扫描仍能发现后加载插件的旧导入；
- 热重载不重复刷屏，源文件新增旧导入后可被重新扫描；
- AST 语法错误不会阻止插件正常走原有加载错误处理。

### 9.4 集成与回归测试

- 选择高频旧入口构造一个未改源码的兼容插件样本并启动。
- 覆盖插件首次启动、停止、单插件热重载、全部插件重载。
- 覆盖事件装饰器、配置重载、模块枚举、缓存/Redis 和消息错误路径。
- 覆盖 CLI、FastAPI lifespan、safe mode 和本地插件同步。
- 按仓库规则运行聚焦测试、Pylint 和完整 `python tests/run.py`。

## 10. 资源、构建和跨仓影响

当前 `MoviePilot-Resources/resources.v3`、Docker 更新脚本、本地安装脚本以及 `MoviePilot-Build` 会把站点扩展和数据文件同步到 `app/helper/`，其中编译扩展的模块名也是 `app.helper.sites`。

如果目标是彻底清理 `helper` 物理目录，需要把这部分作为独立发布批次处理：

- 为站点运行时扩展确定 canonical 路径，例如 `app.infrastructure.sites`；
- 数据文件放到明确的资源目录，不再与 Python helper 源码混放；
- 同步修改 `MoviePilot-Build` 的扩展名和输出参数；
- 同步修改 `MoviePilot-Resources` 的 package target；
- 修改 Dockerfile、`docker/update.sh`、entrypoint、本地安装/卸载和相关文档；
- 为 `app.helper.sites` 保留旧路径兼容，并验证 CPython 扩展通过别名加载的实际行为；若扩展初始化名限制不允许直接别名，保留一个专用原生 adapter，而不是通用 Python 转发文件；
- 分别验证 macOS/Linux 和当前支持的 Python 版本产物。

该跨仓迁移不能混在普通纯 Python 模块搬迁 PR 中，否则发布镜像、本地安装和源码开发环境会出现不同结果。

## 11. 可观测性与故障处理

兼容层建议提供只读诊断快照，至少包含：

- 已安装 Finder 数量和版本；
- 当前映射表版本/摘要；
- 本进程已命中的旧路径集合；
- DEBUG 模式下按插件聚合的旧路径列表；
- 最近一次兼容导入失败及原始异常链。

不要让兼容层吞掉目标模块自己的 `ImportError`。需要区分：

- **旧路径未登记**：`ModuleNotFoundError` 指向旧路径；
- **映射目标不存在**：兼容配置错误，应包含 old/target 信息并在测试或启动校验失败；
- **目标内部导入失败**：保留原始 traceback，附加兼容上下文但不改写根因；
- **循环初始化**：明确报告当前别名解析栈，不能重试后返回半初始化模块。

## 12. 回滚方案

每个迁移批次必须可以独立回滚：

- 映射表带批次或版本元数据，便于定位新规则。
- 兼容层本身提供全局禁用开关仅用于故障诊断；生产默认开启，不能要求用户手动开启才能兼容插件。
- 单个错误映射可被精确禁用，不影响其他已迁移路径。
- 搬迁 PR 不删除旧实现逻辑，只移动 canonical 所有权；Git 回滚后旧文件和映射可以一起恢复。
- 数据库、配置格式和插件存储不应在纯模块搬迁批次变化。
- 跨仓资源迁移保留一个发布周期的旧产物回退能力，并验证旧镜像/新资源及新镜像/旧资源组合的兼容边界。

## 13. 验收标准

单个迁移批次只有同时满足以下条件才算完成：

1. 主程序只导入 canonical 新路径，静态门禁无新增反向依赖或循环。
2. 旧路径不存在同名业务源码文件，映射表是唯一兼容定义。
3. 未修改源码的旧插件样本可以正常启动、执行和热重载。
4. 旧业务模块与新路径的模块、类、枚举和单例身份一致；合成父包只承担白名单路由。
5. DEBUG 模式会对每个插件的每个旧路径首次发出可行动的警告，生产模式无警告噪声。
6. 映射安装不预导入目标模块，不增加可感知的启动副作用。
7. 聚焦测试、静态检查、完整测试以及涉及的构建/资源验证通过。
8. 文档、插件 SDK 推荐路径和跨仓发布脚本与真实运行路径一致。

## 14. 建议的首个实施切片

首个 PR 不应直接搬迁 `config`、`event` 或 `plugin` 这类高扇入模块。建议只完成兼容基础设施：

1. 新增 `app.compat`、空映射表和导入图门禁。
2. 在 `app/__init__.py` 安装 Finder。
3. 增加 DEBUG 诊断聚合器与插件 AST 扫描接口。
4. 选择一个无全局副作用、插件引用较少的纯工具模块作为试点。
5. 用一个保持旧导入的测试插件验证启动和热重载。

试点稳定后，再处理 HTTP/string 等低状态模块，最后拆分并迁移 event/config/plugin 等运行时核心。这样兼容机制、目录重构和循环依赖治理可以分别验证，出现问题时也能定位到具体批次。
