"""合并并分层输出标准分类字段与来源扩展字段目录。"""

from collections.abc import Iterable

from app.domain.classification.fields import merge_field_definitions
from app.schemas.category import ClassificationFieldDefinition


def build_classification_field_catalog(
    extra_fields: Iterable[ClassificationFieldDefinition] = (),
) -> tuple[ClassificationFieldDefinition, ...]:
    """构造可用于新增条件的字段目录；退役字段不会混入选择器。"""
    return tuple(
        definition.model_copy(deep=True)
        for definition in merge_field_definitions(extra_fields)
        if definition.selectable
    )


def build_retired_classification_field_catalog(
    extra_fields: Iterable[ClassificationFieldDefinition] = (),
) -> tuple[ClassificationFieldDefinition, ...]:
    """返回仅供已有规则解析和迁移提示使用的退役字段目录。"""
    return tuple(
        definition.model_copy(deep=True)
        for definition in merge_field_definitions(extra_fields)
        if not definition.selectable
    )
