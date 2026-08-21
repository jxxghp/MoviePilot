"""
数据访问层（Oper）。

与 app/db/models 一一对应：models 声明表结构，oper 承载针对该表的读写。
两个包同名文件互为镜像（models/subscribe.py ↔ oper/subscribe.py），
文件名只写实体，角色由包名表达，因此这里不再有 `_oper` 后缀。

本文件只做符号解析，不在 import 期执行任何动作——没有建引擎、没有连库、
也不会把全部 Oper 模块一并拉起。`from app.db.oper import SubscribeOper`
经下方 __getattr__ 惰性解析，只导入被点名的那一个模块。

这一点不是洁癖：多处测试靠往 sys.modules 塞桩来隔离单个 Oper（例如
app.db.oper.systemconfig），若本文件改成 models/__init__.py 那样的即时
re-export，导入任意一个 Oper 都会连带把其余全部真正拉起来，桩就被绕过了。
按模块直连（from app.db.oper.subscribe import SubscribeOper）仍是仓库内的
首选写法，本入口是给「只想要一个类名」的调用方备的门面。
"""
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 运行期由 __getattr__ 解析，模块 __dict__ 里并不存在这些名字；
    # 这里为静态检查补上真实类型，同时消掉 __all__ 的未定义告警。
    from app.db.oper.agentchat import AgentChatOper
    from app.db.oper.agenttask import AgentTaskOper
    from app.db.oper.downloadfailure import DownloadFailureOper
    from app.db.oper.downloadhistory import DownloadHistoryOper
    from app.db.oper.mediaserver import MediaServerOper
    from app.db.oper.message import MessageOper
    from app.db.oper.pluginconfig import PluginConfigOper
    from app.db.oper.plugindata import PluginDataOper
    from app.db.oper.serviceconfig import ServiceConfigOper
    from app.db.oper.site import SiteOper
    from app.db.oper.subscribe import SubscribeOper
    from app.db.oper.subscribehistory import SubscribeHistoryOper
    from app.db.oper.systemconfig import SystemConfigOper
    from app.db.oper.transferhistory import TransferHistoryOper
    from app.db.oper.transferpending import TransferPendingOper
    from app.db.oper.user import UserOper
    from app.db.oper.userconfig import UserConfigOper
    from app.db.oper.workflow import WorkflowOper

# 类名 -> 所在子模块。子模块名即实体名，与 app/db/models 对齐。
_OPER_MODULES = {
    "AgentChatOper": "agentchat",
    "AgentTaskOper": "agenttask",
    "DownloadFailureOper": "downloadfailure",
    "DownloadHistoryOper": "downloadhistory",
    "MediaServerOper": "mediaserver",
    "MessageOper": "message",
    "PluginConfigOper": "pluginconfig",
    "PluginDataOper": "plugindata",
    "ServiceConfigOper": "serviceconfig",
    "SiteOper": "site",
    "SubscribeHistoryOper": "subscribehistory",
    "SubscribeOper": "subscribe",
    "SystemConfigOper": "systemconfig",
    "TransferHistoryOper": "transferhistory",
    "TransferPendingOper": "transferpending",
    "UserConfigOper": "userconfig",
    "UserOper": "user",
    "WorkflowOper": "workflow",
}


def __getattr__(name: str) -> Any:
    """
    惰性解析 Oper 类，只导入被点名的那个子模块。
    :param name: 属性名
    :return: 对应的 Oper 类
    """
    module_name = _OPER_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{module_name}"), name)


def __dir__() -> list[str]:
    """
    让 dir() 与自动补全看得见惰性名字。
    :return: 属性名列表
    """
    return sorted({*globals(), *_OPER_MODULES})


__all__ = [
    "AgentChatOper",
    "AgentTaskOper",
    "DownloadFailureOper",
    "DownloadHistoryOper",
    "MediaServerOper",
    "MessageOper",
    "PluginConfigOper",
    "PluginDataOper",
    "ServiceConfigOper",
    "SiteOper",
    "SubscribeHistoryOper",
    "SubscribeOper",
    "SystemConfigOper",
    "TransferHistoryOper",
    "TransferPendingOper",
    "UserConfigOper",
    "UserOper",
    "WorkflowOper",
]
