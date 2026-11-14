"""
插件媒体来源分类扩展协议的稳定公开入口。
"""

from copy import deepcopy
from typing import Final, TypeVar, cast

from app.application.chain.context import get_chain_runtime_context
from app.application.classification.execution import ClassificationSubject
from app.schemas.category import (
    ClassificationEnrichmentMatch,
    ClassificationEnrichmentRequest,
    ClassificationEnrichmentResponse,
    ClassificationFactValue,
    ClassificationFieldDefinition,
)
from app.schemas.event import MediaSourceInfo

# 宿主支持的插件媒体来源分类扩展协议版本
MEDIA_SOURCE_CLASSIFICATION_PROTOCOL_VERSION: Final[int] = 2

_ClassificationSubjectT = TypeVar(
    "_ClassificationSubjectT",
    bound=ClassificationSubject,
)


def classify_media(media: _ClassificationSubjectT) -> _ClassificationSubjectT:
    """
    使用当前活动策略对媒体副本进行分类，不修改调用方对象。

    :param media: 待分类的媒体对象，须符合宿主分类服务的输入契约
    :return: 分类后的同类对象；宿主尚未装配分类服务时返回输入的深拷贝
    """
    try:
        service = get_chain_runtime_context().classification_service
    except RuntimeError:
        return deepcopy(media)
    if service is None:
        return deepcopy(media)
    return cast(
        _ClassificationSubjectT,
        service.finalize(media, refresh=True),
    )


__all__ = [
    "ClassificationEnrichmentMatch",
    "ClassificationEnrichmentRequest",
    "ClassificationEnrichmentResponse",
    "ClassificationFactValue",
    "ClassificationFieldDefinition",
    "MEDIA_SOURCE_CLASSIFICATION_PROTOCOL_VERSION",
    "MediaSourceInfo",
    "classify_media",
]
