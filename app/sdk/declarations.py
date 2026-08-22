"""扩展声明面：``provides_*`` 钩子交回给宿主的声明载体与能力标签。

本模块与 `app.sdk.plugins` 方向相反：那里装的是扩展**调用**的宿主管理器，这里装的是
扩展**交出**的东西。声明是扩展面的主入口——各族扩展点经由它登记，登记期即完成契约
判定，因此它必须在 SDK 里有出口，否则扩展只能去 import 宿主内部路径。

消息渠道能力那一族的载体是 ``app.schemas`` 的 ``ChannelCapabilities``，不在本模块：
它同时是渠道分发链路上的传输数据，本就属于 schema 公开面。

``ServiceInstanceDeclaration.capability`` 的取值不在本模块以常量形式出现，一族都不给：
族由宿主登记而不是写死在声明面上的枚举，SDK 照抄一份固定清单只会在新族登记后失真，而
给一部分族配常量、另一部分族不配，等于把同一件事写成两种口径。标签本身是稳定字符串，
照 ``type`` 字段的写法直接给字面量即可；本次运行的宿主认哪些族由
`app.sdk.service_instances` 的 ``service_capabilities()`` 回答，那一族要实现什么方法由
同处的协议与 ``service_instance_required_methods()`` 回答。
"""

from app.runtime.extensions.contract.declaration import (
    ActionDeclaration,
    AgentToolDeclaration,
    CommandDeclaration,
    DashboardDeclaration,
    ExtensionDeclaration,
    FilterRuleDeclaration,
    FilterRuleGroupDeclaration,
    MediaSourceDeclaration,
    MetaParserDeclaration,
    ModuleDeclaration,
    ScheduleDeclaration,
    ServiceInstanceDeclaration,
    ServiceInstanceRequirement,
)


__all__ = [
    "ActionDeclaration",
    "AgentToolDeclaration",
    "CommandDeclaration",
    "DashboardDeclaration",
    "ExtensionDeclaration",
    "FilterRuleDeclaration",
    "FilterRuleGroupDeclaration",
    "MediaSourceDeclaration",
    "MetaParserDeclaration",
    "ModuleDeclaration",
    "ScheduleDeclaration",
    "ServiceInstanceDeclaration",
    "ServiceInstanceRequirement",
]
