"""插件媒体数据源声明的契约校验。"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.declaration import (
    declaration_media_source_identity,
    declaration_media_types,
)
from app.schemas.media import normalize_media_source


def media_source_declaration_violation(declaration: Any) -> Optional[str]:
    """
    校验媒体数据源声明是否满足登记契约

    契约要求声明非空的展示名称 name、能被 ``MediaSource`` 解析的数据源标识
    media_source（内置常量或形如 ``[a-z][a-z0-9._-]{0,63}`` 的插件扩展标识）；
    声明了 media_types 时须是字符串序列。三项中任一不满足都拒绝登记，不留到
    调用时才失败。识别、搜索与刮削的实际实现由 ``provides_modules()``/
    ``get_module()`` 按契约名分发，本声明只承载数据源自身的展示信息。

    :param declaration: `MediaSourceDeclaration` 实例，或插件直接交出的描述字典
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        media_source, name = declaration_media_source_identity(declaration)
        media_types = declaration_media_types(declaration)
    except Exception as error:
        return f"读取媒体数据源声明出错：{error}"
    if not name:
        return "未声明非空的数据源展示名称 name"
    if not media_source:
        return "未声明非空的数据源标识 media_source"
    if normalize_media_source(media_source) is None:
        return f"数据源标识 {media_source!r} 不是合法的 MediaSource 标识"
    if media_types is not None and not all(isinstance(item, str) for item in media_types):
        return "media_types 必须是字符串序列"
    return None
