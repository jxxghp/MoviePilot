"""3.0.5
允许插件扩展媒体来源

Revision ID: b3d7e9f1a2c4
Revises: e3d9f4b7c806
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "b3d7e9f1a2c4"
down_revision = "e3d9f4b7c806"
branch_labels = None
depends_on = None


MEDIA_TABLES = (
    "subscribe",
    "subscribehistory",
    "downloadhistory",
    "transferhistory",
    "downloadfailure",
    "mediaserveritem",
)
EXTENSIBLE_IDENTITY_CHECK_SQL = (
    "(media_source IS NULL AND media_id IS NULL) OR "
    "(media_source IS NOT NULL AND "
    "trim(media_source) <> '' AND media_source = lower(trim(media_source)) AND "
    "length(media_source) <= 64 AND media_source NOT LIKE '%:%' AND "
    "media_source NOT LIKE '% %' AND "
    "media_id IS NOT NULL AND trim(media_id) <> '' AND trim(media_id) <> '0')"
)
BUILTIN_IDENTITY_CHECK_SQL = (
    "(media_source IS NULL AND media_id IS NULL) OR "
    "(media_source IS NOT NULL AND media_source IN ("
    "'anilist', 'bangumi', 'bilibili', 'douban', 'doubanmusic', 'imdb', "
    "'mangguodiscover', 'migu', 'musicbrainz', 'tencentvideodiscover', "
    "'theaudiodb', 'themoviedb', 'tvdb') AND "
    "media_id IS NOT NULL AND trim(media_id) <> '' AND trim(media_id) <> '0')"
)


def _inspector() -> sa.Inspector:
    """返回使用当前迁移连接的数据库检查器。"""
    return sa.inspect(op.get_bind())


def _replace_constraints(check_sql: str) -> None:
    """在现有媒体表上以批处理方式替换统一身份约束。"""
    table_names = set(_inspector().get_table_names())
    for table_name in MEDIA_TABLES:
        if table_name not in table_names:
            continue
        constraint_name = f"ck_{table_name}_media_identity"
        existing = {
            constraint.get("name")
            for constraint in _inspector().get_check_constraints(table_name)
        }
        with op.batch_alter_table(table_name) as batch_op:
            if constraint_name in existing:
                batch_op.drop_constraint(constraint_name, type_="check")
            batch_op.create_check_constraint(constraint_name, check_sql)


def upgrade() -> None:
    """把固定内置来源白名单替换为允许插件来源的格式约束。"""
    _replace_constraints(EXTENSIBLE_IDENTITY_CHECK_SQL)


def downgrade() -> None:
    """恢复只允许当前内置来源的旧约束。"""
    _replace_constraints(BUILTIN_IDENTITY_CHECK_SQL)
