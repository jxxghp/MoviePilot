"""ORM 模型的惰性兼容导出与显式注册入口。"""

from importlib import import_module
from typing import Any

from . import _identity  # noqa: F401  注册全局媒体身份写入不变量

_MODEL_EXPORTS = {
    "AgentChat": ("app.db.models.agentchat", "AgentChat"),
    "AgentTask": ("app.db.models.agenttask", "AgentTask"),
    "AgentTaskRun": ("app.db.models.agenttaskrun", "AgentTaskRun"),
    "DownloadFailure": ("app.db.models.downloadfailure", "DownloadFailure"),
    "DownloadFiles": ("app.db.models.downloadhistory", "DownloadFiles"),
    "DownloadHistory": ("app.db.models.downloadhistory", "DownloadHistory"),
    "MediaServerItem": ("app.db.models.mediaserver", "MediaServerItem"),
    "Message": ("app.db.models.message", "Message"),
    "OutboxMessage": ("app.db.models.outbox", "OutboxMessage"),
    "PassKey": ("app.db.models.passkey", "PassKey"),
    "PluginData": ("app.db.models.plugindata", "PluginData"),
    "PluginInstallation": (
        "app.db.models.plugininstallation",
        "PluginInstallation",
    ),
    "PluginIdentity": (
        "app.db.models.pluginidentity",
        "PluginIdentity",
    ),
    "Site": ("app.db.models.site", "Site"),
    "SiteIcon": ("app.db.models.siteicon", "SiteIcon"),
    "SiteStatistic": ("app.db.models.sitestatistic", "SiteStatistic"),
    "SiteUserData": ("app.db.models.siteuserdata", "SiteUserData"),
    "Subscribe": ("app.db.models.subscribe", "Subscribe"),
    "SubscribeHistory": (
        "app.db.models.subscribehistory",
        "SubscribeHistory",
    ),
    "SubscriptionSearchBatch": (
        "app.db.models.subscriptionsearch",
        "SubscriptionSearchBatch",
    ),
    "SubscriptionSearchTask": (
        "app.db.models.subscriptionsearch",
        "SubscriptionSearchTask",
    ),
    "SubscriptionSiteBudget": (
        "app.db.models.subscriptionsearch",
        "SubscriptionSiteBudget",
    ),
    "SystemConfig": ("app.db.models.systemconfig", "SystemConfig"),
    "TransferHistory": ("app.db.models.transferhistory", "TransferHistory"),
    "TransferExecutionStep": (
        "app.db.models.transferexecutionstep",
        "TransferExecutionStep",
    ),
    "TransferSettlementReceipt": (
        "app.db.models.transfersettlementreceipt",
        "TransferSettlementReceipt",
    ),
    "TransferPending": ("app.db.models.transferpending", "TransferPending"),
    "User": ("app.db.models.user", "User"),
    "UserConfig": ("app.db.models.userconfig", "UserConfig"),
    "Workflow": ("app.db.models.workflow", "Workflow"),
}


def load_all_models() -> None:
    """显式导入全部 ORM 模型，供建表和 Alembic 元数据收集使用。"""
    for module_name, _ in dict.fromkeys(_MODEL_EXPORTS.values()):
        import_module(module_name)


def __getattr__(name: str) -> Any:
    """按需解析旧模型包级导出，并缓存模型类。"""
    contract = _MODEL_EXPORTS.get(name)
    if contract is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = contract
    value = getattr(import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """返回模型包的兼容公开面。"""
    return sorted({*globals(), *_MODEL_EXPORTS, "load_all_models"})


__all__ = [*_MODEL_EXPORTS, "load_all_models"]
