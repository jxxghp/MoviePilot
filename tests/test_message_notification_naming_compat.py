"""
message/notification 命名统一的兼容守护测试

边界定义：notification 表示通知渠道能力，message 表示各渠道发送或接收的消息。
canonical 源码不保留任何旧名别名，旧名一律经由 runtime/compat 映射表
（SYMBOL_ALIASES）在导入器层惰性解析，供存量插件继续使用。
"""
import importlib

from app.runtime.compat.manifest import SYMBOL_ALIASES
from app.schemas.message import (
    IncomingMessage,
    Message,
    MessageClearBefore,
    MessageClearData,
    MessageClearScope,
    MessageHistoryItem,
)
from app.schemas.notification import (
    ChannelCapabilities,
    ChannelCapability,
    ChannelCapabilityManager,
)
from app.schemas.types import MessageType, NotificationChannel


# 旧名 -> canonical 对象的期望映射，覆盖全部登记的兼容入口
_EXPECTED_RESOLUTIONS = {
    ("app.schemas.types", "MessageChannel"): NotificationChannel,
    ("app.schemas.types", "NotificationType"): MessageType,
    ("app.schemas.message", "Notification"): Message,
    ("app.schemas.message", "CommingMessage"): IncomingMessage,
    ("app.schemas.message", "NotificationHistoryItem"): MessageHistoryItem,
    ("app.schemas.message", "NotificationClearScope"): MessageClearScope,
    ("app.schemas.message", "NotificationClearBefore"): MessageClearBefore,
    ("app.schemas.message", "NotificationClearData"): MessageClearData,
    ("app.schemas.message", "ChannelCapability"): ChannelCapability,
    ("app.schemas.message", "ChannelCapabilities"): ChannelCapabilities,
    ("app.schemas.message", "ChannelCapabilityManager"): ChannelCapabilityManager,
}


def test_canonical_modules_do_not_define_legacy_names():
    """canonical 模块自身不得保留旧名物理别名，旧名只能来自兼容映射。"""
    types_module = importlib.import_module("app.schemas.types")
    message_module = importlib.import_module("app.schemas.message")

    for legacy_name in ("MessageChannel", "NotificationType"):
        assert legacy_name not in types_module.__dict__
    for legacy_name in (
        "Notification",
        "CommingMessage",
        "NotificationHistoryItem",
        "NotificationClearScope",
        "NotificationClearBefore",
        "NotificationClearData",
    ):
        assert legacy_name not in message_module.__dict__


def test_manifest_registers_all_legacy_message_notification_symbols():
    """映射表必须登记全部旧名，且目标指向当前 canonical 符号。"""
    for (module_name, legacy_name), canonical in _EXPECTED_RESOLUTIONS.items():
        alias = SYMBOL_ALIASES[module_name][legacy_name]
        target = getattr(importlib.import_module(alias.target_module), alias.target_name)
        assert target is canonical, (module_name, legacy_name)


def test_legacy_symbol_imports_resolve_through_compat_hook():
    """插件旧导入路径应经兼容钩子解析到 canonical 对象。"""
    for (module_name, legacy_name), canonical in _EXPECTED_RESOLUTIONS.items():
        module = importlib.import_module(module_name)
        assert getattr(module, legacy_name) is canonical, (module_name, legacy_name)


def test_schemas_package_level_legacy_imports_resolve():
    """from app.schemas import 旧名 的插件写法应继续可用。"""
    schemas_package = importlib.import_module("app.schemas")

    assert schemas_package.MessageChannel is NotificationChannel
    assert schemas_package.NotificationType is MessageType
    assert schemas_package.Notification is Message
    assert schemas_package.CommingMessage is IncomingMessage
    assert schemas_package.NotificationHistoryItem is MessageHistoryItem
    assert schemas_package.ChannelCapabilityManager is ChannelCapabilityManager


def test_legacy_from_import_statement_works():
    """from ... import 语句形式的旧导入应正常执行。"""
    scope: dict = {}
    exec(
        "from app.schemas import Notification, MessageChannel, NotificationType\n"
        "from app.schemas.message import CommingMessage\n"
        "from app.schemas.types import MessageChannel as LegacyChannel\n",
        scope,
    )

    assert scope["Notification"] is Message
    assert scope["MessageChannel"] is NotificationChannel
    assert scope["NotificationType"] is MessageType
    assert scope["CommingMessage"] is IncomingMessage
    assert scope["LegacyChannel"] is NotificationChannel


def test_legacy_and_canonical_instances_interchangeable():
    """旧名构造的实例应与 canonical 类型互相兼容（同一类对象）。"""
    legacy_message = importlib.import_module("app.schemas").Notification(title="t")

    assert isinstance(legacy_message, Message)
    assert isinstance(
        Message(title="t"),
        importlib.import_module("app.schemas.message").Notification,
    )
