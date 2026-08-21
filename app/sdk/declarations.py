"""扩展声明面：``provides_*`` 钩子交回给宿主的声明载体与能力标签。

本模块与 `app.sdk.plugins` 方向相反：那里装的是扩展**调用**的宿主管理器，这里装的是
扩展**交出**的东西。声明是扩展面的主入口——各族扩展点经由它登记，登记期即完成契约
判定，因此它必须在 SDK 里有出口，否则扩展只能去 import 宿主内部路径。

消息渠道能力那一族的载体是 ``app.schemas`` 的 ``ChannelCapabilities``，不在本模块：
它同时是渠道分发链路上的传输数据，本就属于 schema 公开面。

``AUTH_CAPABILITY`` 与 ``STORAGE_CAPABILITY`` 是 ``ServiceInstanceDeclaration.capability``
的取值，与声明类同处一处：它们是声明的词汇而不是独立能力。其余服务族的标签取
``app.schemas`` 的 ``ModuleType``——族由宿主登记而不是写死在声明面上的枚举，SDK 照抄一份
固定清单只会在新族登记后失真。
"""

from app.runtime.extensions.declaration import (
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
from app.runtime.extensions.service_config import AUTH_CAPABILITY, STORAGE_CAPABILITY


__all__ = [
    "AUTH_CAPABILITY",
    "STORAGE_CAPABILITY",
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
