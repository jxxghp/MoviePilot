"""SDK 公开签名上出现、而 ``app.schemas`` 聚合入口取不到的枚举取值域。

插件的合法 import 面只有 ``app.sdk`` 与 ``app.schemas`` 聚合入口，``app.schemas.types``
子模块被导入边界门禁拦下。聚合入口的清单由 ``scripts/schema/exports.py`` 按 ``SCHEMA_MODULES``
生成，那份清单不含 ``types``——它是模型层的公共取值域而不是 schema 数据模型，且该模块没有
``__all__``，收进去会连 ``re``、``Enum``、``Optional`` 这些模块内的导入名一并放出。因此这些
枚举由 SDK 门面承担出口。

本模块只装 SDK 公开签名闭包里取不到的那几个，不是 ``app.schemas.types`` 的整体镜像：
``MediaType``、``MessageType``、``NotificationChannel`` 等已被某个 schema 子模块转出、
聚合入口取得到，在此重复一遍等于给同一个对象立两个取用位置。清单由
``tests/test_sdk_type_closure.py`` 按签名实算，多一个少一个都会报红。
"""

from app.schemas.types import (
    ChainEventType,
    EventType,
    MediaImageType,
    SystemConfigKey,
    TorrentStatus,
)


__all__ = [
    "ChainEventType",
    "EventType",
    "MediaImageType",
    "SystemConfigKey",
    "TorrentStatus",
]
