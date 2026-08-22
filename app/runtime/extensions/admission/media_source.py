"""插件媒体数据源声明的契约校验。

一个媒体数据源是展示信息与实现的合体，缺任一半都不构成可用的来源：只有展示信息的
来源在界面上选得到、调用却落空，只有实现的来源能被调用却进不了来源列表。两半分开
声明时各自都独立合法，校验拦不住任何一种残缺，问题要到用户使用时才暴露；因此两半
在同一条声明里一并校验，任一缺失即拒绝登记。
"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.contract.declaration import (
    declaration_media_source_identity,
    declaration_media_source_methods,
    declaration_media_types,
)
from app.runtime.extensions.admission.module import method_table_violation
from app.schemas.media import normalize_media_source


def media_source_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验媒体数据源声明是否满足登记契约

    契约要求声明非空的展示名称 name、能被 ``MediaSource`` 解析的数据源标识
    media_source，以及承载本来源实现的非空方法表 methods；声明了 media_types 时须是
    字符串序列。任一不满足都拒绝登记，不留到用户在界面上选中该来源时才失败。

    :param declaration: `MediaSourceDeclaration` 实例，或插件直接交出的描述字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        media_source, name = declaration_media_source_identity(declaration)
        media_types = declaration_media_types(declaration)
        methods = declaration_media_source_methods(declaration)
    except Exception as error:
        return f"读取媒体数据源声明出错：{error}"
    if not name:
        return (
            "未声明非空的数据源展示名称 name：来源列表按 name 呈现，缺了它这条声明"
            "带的实现进不了列表，用户在界面上选不到它"
        )
    if not media_source:
        return (
            "未声明非空的数据源标识 media_source：宿主按该标识把调用路由到本来源的"
            "实现，缺了它这条声明既无从登记也无从分发"
        )
    if normalize_media_source(media_source) is None:
        return f"数据源标识 {media_source!r} 不是合法的 MediaSource 标识"
    if media_types is not None and not all(isinstance(item, str) for item in media_types):
        return "media_types 必须是字符串序列"
    if not methods:
        return (
            f"数据源 {media_source!r} 只声明了展示信息，未随声明交出实现："
            "methods 缺失或为空映射。识别、搜索、图片与 NFO 刮削的实现须与展示信息"
            "写在同一条声明里，否则来源列表会显示它、用户选中后却无实现可调用"
        )
    if violation := method_table_violation(methods):
        return f"数据源 {media_source!r} 的实现不合契约：{violation}"
    return None
