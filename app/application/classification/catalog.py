"""合并标准分类字段与当前注册媒体来源的能力目录。"""

from collections.abc import Iterable

from app.domain.classification.fields import merge_field_definitions
from app.schemas.category import ClassificationFieldDefinition


def build_classification_field_catalog(
    extra_fields: Iterable[ClassificationFieldDefinition] = (),
) -> tuple[ClassificationFieldDefinition, ...]:
    """构造前端字段目录；动态来源未声明能力时保持键缺失。"""
    return tuple(
        definition.model_copy(deep=True)
        for definition in merge_field_definitions(extra_fields)
    )
