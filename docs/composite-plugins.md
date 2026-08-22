# 一个插件登记多项同类能力

`_PluginBase`（`app.sdk.extension`）暴露十二个 `provides_*` 钩子，签名统一形如
`Optional[List[XxxDeclaration]]`：一次调用返回的是一张列表，列表里放几项由插件自己决定，
框架不设上限。这十二个钩子与它们各自的声明类型定义在
`app/runtime/extensions/contract/declaration.py`，通过 `app/sdk/declarations.py` 交给插件；
`ChannelCapabilities` 是唯一例外，它复用 `app.schemas.notification.ChannelCapabilities`
这个既有的传输数据类，见该文件顶部注释。

登记按**扩展实例键**（`plugin_id` 或 `plugin_id@instance_id`）记账。以下几族的注册表都实测
提供按实例键批量回收的方法：

| 族 | 注册表 | 回收方法 |
|---|---|---|
| 服务实例类型（含存储、登录入口） | `app/runtime/extensions/registry/service_instance.py::ServiceInstanceRegistry` | `unregister_owner(owner)` |
| 存储后端 | `app/runtime/extensions/registry/storage.py::StorageBackendRegistry` | `unregister_owner(owner)` |
| 远程命令 | `app/runtime/extensions/registry/command.py::PluginCommandRegistry` | `unregister_owner(owner)` |
| 名称解析器 | `app/runtime/extensions/registry/meta_parser.py::MetaParserRegistry` | `unregister_owner(owner)` |
| 定时任务 | `app/scheduler/plugins.py::PluginScheduling` | `remove_plugin_job(pid)`（不传 `job_id` 时移除该插件/实例的全部任务） |

因此一个插件在同一个 `provides_*()` 调用里返回三项、五项还是一项，登记与卸载的开销不随项数
变化——停用插件时框架按 owner 一次性回收它登记过的全部条目，不逐条追踪。测试证据：
`tests/test_plugin_filter_rules.py::test_projection_skips_only_the_offending_declaration`
构造一个插件、一次 `provides_filter_rules()` 调用返回两条声明，验证框架逐条校验、逐条接受
或拒绝，互不影响。

## 十二族能力表

「级别」取自 `docs/plugin-extension-architecture.md` §7.3：**扩展级**指同一插件的多个分身
（`plugin_id@instance_id`）声明同一个标识时只认一次（默认分身优先，其余按实例标识升序取
第一个，裁决收在 `app/runtime/extensions/admission/extension_scoped.py`）；**分身级**指标识本身
带着实例归属，各分身各自成立、不去重。这条轴回答的是「同一插件的分身之间怎么处理同名声明」，
与「一次调用返回几项」是两件事：无论哪个级别，单次调用返回的多项只要标识互不相同，天然互不
冲突。

| 钩子 | 声明类型 | 唯一键 | 级别（同插件分身间） |
|---|---|---|---|
| `provides_modules` | `ModuleDeclaration` | 方法名（实例内） | 分身级 |
| `provides_media_sources` | `MediaSourceDeclaration` | `media_source` | 扩展级 |
| `provides_service_instances` | `ServiceInstanceDeclaration` | `(capability, type)` | 扩展级 |
| `provides_schedules` | `ScheduleDeclaration` | `job_id`（实例内） | 分身级 |
| `provides_agent_tools` | `AgentToolDeclaration` | `name` | 扩展级 |
| `provides_dashboards` | `DashboardDeclaration` | `key`（实例内） | 分身级 |
| `provides_commands` | `CommandDeclaration` | `cmd` | 扩展级 |
| `provides_channel_capabilities` | `ChannelCapabilities`（`app.schemas.notification`） | `channel` | 分身级 |
| `provides_actions` | `ActionDeclaration` | `action_id`（实例内） | 分身级 |
| `provides_meta_parsers` | `MetaParserDeclaration` | `parser_id`（实例内） | 分身级 |
| `provides_filter_rules` | `FilterRuleDeclaration` | `rule_id` | 扩展级 |
| `provides_filter_rule_groups` | `FilterRuleGroupDeclaration` | `name` | 扩展级 |

「实例内」表示唯一性只在声明它的那个分身范围内要求，宿主按 `(实例键, 标识)` 建键，天然不与
其它分身或其它插件冲突。

### 跨插件冲突：不是"谁先注册谁赢"

`docs/plugin-extension-architecture.md` §7.2 的判据是**绝不取第一个、绝不取任意一个**。同一
标识被两个不同插件声明时，结局按标识是否指称同一个外部对象分两支：

- **后登记覆盖**：`u115` 就是那个存储后端，`downloader`+`qbittorrent` 就是那个下载器类型——
  标识指称一个共同的外部对象，后声明的一方接管，先声明的一方停用后自动恢复。适用于
  `provides_service_instances`（含存储与登录入口）与 `provides_channel_capabilities`。渠道
  能力另有一条内建优先规则：内建渠道的静态能力表命中时插件登记不会被查到。证据：
  `app/runtime/extensions/registry/service_instance.py::unregister_owner` 的文档字符串
  ——"类型一旦被更晚的登记覆盖，owner 随之更新为新的登记方，因此本方法只回收…"。
- **双方一并失效并告警**：`/sync` 在两个插件里做的是两件不相干的事，宿主分辨不出谁对，双方
  都不生效。适用于 `provides_filter_rules`、`provides_filter_rule_groups`、
  `provides_commands`（跨插件之间）。裁决收在 `app/runtime/extensions/admission/command_arbitration.py`
  的模块级文档。
- **接管须显式声明**：插件命令词撞上**内建**命令时既不是"共同对象"也不是"两个不相干的
  东西"——声明 `overrides_builtin=True` 才按接管处置，插件命令生效、内建命令被压住；不声明
  就按撞车处置，插件命令作废、内建命令保持生效。裁决同样在
  `app/runtime/extensions/admission/command_arbitration.py`。
- **分身级的族不构成跨插件冲突**：`provides_modules`、`provides_schedules`、
  `provides_dashboards`、`provides_actions`、`provides_meta_parsers` 的唯一性只在声明它的
  实例范围内要求，两个插件天然分处两个实例键，不会撞在一起（模块方法表的多来源契约方法是唯一
  例外，按调用方传入的 `source` 参数路由，非本来源必须返回 `None` 让出——见
  `app/sdk/extension.py` 的 `provides_modules` 与 `provides_media_sources` 文档字符串）。

## 三个参考实现怎么用它

`tests/test_plugin_import_boundary.py` 的 `REFERENCE_PLUGINS` 只登记了三个插件；它们是唯一
被门禁保护、保证"只用 `app.sdk` 也写得出来"的范例。三个插件目前都只在各自的
`provides_*()` 调用里返回**一项**，这里如实注明，不假装它们做了更多。

### `app/plugins/githubsso/__init__.py`——登录入口（`capability="auth"`）

```python
from app.sdk.declarations import ServiceInstanceDeclaration
from app.sdk.extension import _PluginBase

def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
    return [
        ServiceInstanceDeclaration(
            capability=SERVICE_CAPABILITY,  # "auth"
            type=SERVICE_TYPE,              # "github"
            name="GitHub 单点登录",
            icon="mdi-github",
            impl=GithubSsoEntry,
            multi_instance=True,
            config_form=config_form(),
            config_schema=CONFIG_SCHEMA,
        )
    ]
```

`multi_instance=True` 回答的是"用户能为 `github` 这个类型配几份"，不是"这次调用返回几项"：
用户在登录认证设置里配两份 GitHub 部署的凭据，宿主按配置数扇出两个登录入口，与
`provides_service_instances()` 本身只声明一项类型不矛盾——这正是 §7.4 讲的"实例数由声明
表达，与声明它的调用返回几项、扩展建了几个分身，都是不同的轴"。

### `app/plugins/p123disk/__init__.py`——存储后端（`capability="storage"`）

```python
from app.sdk.declarations import ServiceInstanceDeclaration
from app.sdk.extension import _PluginBase

def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
    return [
        ServiceInstanceDeclaration(
            capability="storage",
            type=STORAGE_ID,        # "p123"
            name=STORAGE_NAME,
            icon=STORAGE_ICON,
            multi_instance=True,
            impl=P123Storage,       # 继承 app.sdk.storage.StorageBase
            config_form=storage_config_form(),
            config_schema=STORAGE_CONFIG_SCHEMA,
        )
    ]
```

存储族的 `impl` 回答的是"按令牌取用时后端类是谁"而不是"怎么构造"：宿主不按
`impl(name=..., **config)` 展开构造，缺省用默认工厂
`app.runtime.extensions.registry.storage.storage_instance_factory` 按实例归属交付后端，插件
不必自己写工厂——`app/plugins/p123disk/__init__.py` 的声明也确实没有给 `factory`。

### `app/plugins/servicehealth/__init__.py`——智能体工具

```python
from app.sdk.declarations import AgentToolDeclaration
from app.sdk.extension import _PluginBase

def provides_agent_tools(self) -> Optional[List[AgentToolDeclaration]]:
    return [
        AgentToolDeclaration(
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            impl=ServiceInstanceHealthTool,  # 继承 app.sdk.agent.MoviePilotTool
        )
    ]
```

工具实现类 `ServiceInstanceHealthTool`（`app/plugins/servicehealth/probe.py`）本身消费另一族
声明的产物：它调用 `app.sdk.service_instances.service_capabilities()` 与
`service_instance_required_methods()` 查询当前登记的服务族与必填只读方法，再经
`app.sdk.services` 的 `DownloaderHelper`/`MediaServerHelper`/`NotificationHelper` 取实例状态——
可见能力表不是各族互不相干，`provides_agent_tools` 声明的工具可以读取
`provides_service_instances` 登记的结果。

## 想让一次调用登记多项时怎么写

`githubsso` 现在的 `provides_service_instances()` 只 `return` 了一个
`ServiceInstanceDeclaration`；若该插件要再接入第二个登录方式，只需要在同一个返回列表里
追加第二个 `ServiceInstanceDeclaration`，把 `type`（连同它自己的 `impl`、`config_form`、
`config_schema`）换成新类型自己的一份——写法上就是给同一个 Python 列表字面量多加一个元素，
不涉及额外的注册调用或生命周期钩子。

两项的 `(capability, type)` 不同，`provides_service_instances` 是扩展级族，登记表按
`(capability, type)` 建键，二者各自独立登记、各自独立被 `unregister_owner` 回收，互不影响。
`provides_commands`、`provides_media_sources`、`provides_filter_rules` 等其余扩展级族同理：
只要一次调用返回的多项标识互不相同，登记与卸载不需要插件多做任何事。分身级的族
（`provides_modules`、`provides_schedules`、`provides_dashboards`、`provides_actions`、
`provides_meta_parsers`、`provides_channel_capabilities`）连"标识互不相同"这个前提都不必满足
到跨实例的程度——它们的唯一性只在声明它的那个分身范围内要求。

本仓库当前没有一个 `provides_*()` 调用实际返回过一项以上——三个参考插件都只声明一项，
`tests/test_plugin_filter_rules.py::test_projection_skips_only_the_offending_declaration`
用测试桩而非参考插件构造了两项。这里如实说明，不用虚构类去凑一个看着更完整的例子。

## 与本文档保持同步的检查

本文档提到的每一个 `provides_*` 钩子名，都必须真实存在于 `app.sdk.extension._PluginBase`；
本文档代码块里的每一条 `import`，都必须能在当前代码库里解析成功。这两条由
`tests/test_composite_plugins_doc.py` 在每次测试运行时核对，不是靠人工比对维持。
