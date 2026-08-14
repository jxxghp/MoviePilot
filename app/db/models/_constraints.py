from sqlalchemy import CheckConstraint

MEDIA_IDENTITY_CHECK_SQL = (
    "(media_source IS NULL AND media_id IS NULL) OR "
    "(media_source IS NOT NULL AND "
    "trim(media_source) <> '' AND media_source = lower(trim(media_source)) AND "
    "length(media_source) <= 64 AND media_source NOT LIKE '%:%' AND "
    "media_source NOT LIKE '% %' AND "
    "media_id IS NOT NULL AND trim(media_id) <> '' AND trim(media_id) <> '0')"
)


def media_identity_constraint(table_name: str) -> CheckConstraint:
    """构造允许插件扩展来源且保证身份成对的数据库约束。"""
    return CheckConstraint(
        MEDIA_IDENTITY_CHECK_SQL,
        name=f"ck_{table_name}_media_identity",
    )
