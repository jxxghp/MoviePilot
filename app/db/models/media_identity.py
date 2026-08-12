from sqlalchemy import CheckConstraint

from app.schemas.types import MediaSource


MEDIA_SOURCE_SQL_VALUES = ", ".join(
    f"'{media_source.value}'" for media_source in MediaSource
)
MEDIA_IDENTITY_CHECK_SQL = (
    "(media_source IS NULL AND media_id IS NULL) OR "
    "(media_source IS NOT NULL AND "
    f"media_source IN ({MEDIA_SOURCE_SQL_VALUES}) AND "
    "media_id IS NOT NULL AND trim(media_id) <> '' AND trim(media_id) <> '0')"
)


def media_identity_constraint(table_name: str) -> CheckConstraint:
    """构造通用媒体表使用的来源枚举与身份成对数据库约束。"""
    return CheckConstraint(
        MEDIA_IDENTITY_CHECK_SQL,
        name=f"ck_{table_name}_media_identity",
    )
