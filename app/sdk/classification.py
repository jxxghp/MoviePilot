"""插件媒体来源分类扩展协议的稳定公开入口。"""

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

MEDIA_SOURCE_CLASSIFICATION_PROTOCOL_VERSION: Final[int] = 2
"""宿主支持的插件媒体来源分类扩展协议版本。"""

_ClassificationSubjectT = TypeVar(
    "_ClassificationSubjectT",
    bound=ClassificationSubject,
)


def classify_media(media: _ClassificationSubjectT) -> _ClassificationSubjectT:
    """使用当前活动策略分类媒体副本；宿主尚未装配时返回隔离副本。"""
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
