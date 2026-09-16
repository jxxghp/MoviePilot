"""分类条件树的共享纯函数。"""

from __future__ import annotations

from app.schemas.category import (
    ClassificationCondition,
    ClassificationConditionGroup,
    ClassificationConditionNode,
)


def condition_field_ids(node: ClassificationConditionNode) -> tuple[str, ...]:
    """按条件树声明顺序递归返回全部叶子字段 ID。"""
    if isinstance(node, ClassificationCondition):
        return (node.field,)
    if not isinstance(node, ClassificationConditionGroup):
        return ()
    children: tuple[ClassificationConditionNode, ...]
    if node.all is not None:
        children = tuple(node.all)
    elif node.any is not None:
        children = tuple(node.any)
    elif node.not_ is not None:
        children = (node.not_,)
    else:
        children = ()
    return tuple(
        field_id
        for child in children
        for field_id in condition_field_ids(child)
    )
