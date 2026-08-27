"""只读查询 Oper 共享的持久化原语。

本模块只承载跨表完全相同的查询表示规则和分页执行步骤。每个具体 Oper 仍负责
声明本表模型、筛选字段、SQLAlchemy 条件、count/page 语句以及排序，避免这里退化
成任意表的 Repository。
"""

from collections.abc import Iterable
from enum import Enum
from typing import Any, TypeVar

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.base import Base
from app.schemas.query import QueryPageRequest, QuerySortDirection
from app.schemas.types import MEDIA_SOURCE_IDENTIFIER_PATTERN, MUSIC_ENTITY_RECORDING

ModelT = TypeVar("ModelT", bound=Base)


def enum_value(value: Any) -> Any:
    """返回筛选枚举对应的数据库值。"""
    return value.value if isinstance(value, Enum) else value


def enum_values(values: Iterable[Any]) -> tuple[Any, ...]:
    """去除空筛选值、归一枚举，并保留调用方顺序。"""
    normalized: list[Any] = []
    for value in values:
        value = enum_value(value)
        if value in (None, ""):
            continue
        normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def literal_contains(column: Any, value: str) -> Any:
    """构造不区分大小写且不解释 ``%``、``_`` 通配符的字面包含条件。"""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def media_identity_conditions(model: Any, query: Any) -> list[Any]:
    """按媒体来源与原生 ID 的成对合同构造条件，并对非法身份 fail-closed。"""
    media_source = query.media_source
    media_id = query.media_id
    if (media_source is None) != (media_id is None):
        raise ValueError("media_source 和 media_id 必须同时提供")
    if media_source is None:
        return []
    normalized_id = str(media_id).strip()
    if not normalized_id or normalized_id == "0":
        raise ValueError("media_id 必须是非零的来源原生 ID")
    return [
        model.media_source == enum_value(media_source),
        model.media_id == normalized_id,
    ]


def required_media_identity_conditions(model: Any) -> list[Any]:
    """构造只保留可解析来源与非空、非零原生 ID 的条件。"""
    return [
        model.media_source.is_not(None),
        func.trim(model.media_source) != "",
        func.lower(func.trim(model.media_source)).regexp_match(
            MEDIA_SOURCE_IDENTIFIER_PATTERN
        ),
        model.media_id.is_not(None),
        func.trim(model.media_id) != "",
        func.trim(model.media_id) != "0",
    ]


def music_type_condition(column: Any, music_type: str | None) -> Any | None:
    """兼容未标注音乐类型的历史单曲记录。"""
    music_type = enum_value(music_type)
    if not music_type:
        return None
    if music_type == MUSIC_ENTITY_RECORDING:
        return or_(column == music_type, column.is_(None))
    return column == music_type


def execute_page(
    session: Session,
    count_statement: Any,
    page_statement: Any,
    page: QueryPageRequest,
) -> tuple[list[ModelT], int]:
    """在同一同步 Session 内先统计再读取一页已构造的查询语句。"""
    total = int(session.execute(count_statement).scalar_one() or 0)
    records = list(
        session.execute(page_statement.offset((page.page - 1) * page.count).limit(page.count)).scalars().all()
    )
    return records, total


def descending(page: QueryPageRequest) -> bool:
    """返回公开分页排序是否为降序，集中处理枚举表示。"""
    return page.sort.direction == QuerySortDirection.DESC


__all__ = [
    "descending",
    "enum_value",
    "enum_values",
    "execute_page",
    "literal_contains",
    "media_identity_conditions",
    "music_type_condition",
    "required_media_identity_conditions",
]
