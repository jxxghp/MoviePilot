"""建表约束的共享片段——本模块不声明任何表，只被同包的模型模块拼进 ``__table_args__``。

以下划线开头且不进 ``models/__init__.py`` 的再导出，是为了与同目录「一实体一文件」的
模块区分开：叫 ``media_identity.py`` 时它看着就像一张 MediaIdentity 表。

注意：alembic 迁移脚本必须自带 SQL 常量的副本而不是 import 本模块——迁移是历史快照，
跟着当前代码一起演进会让旧库重放出新约束。
"""
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
