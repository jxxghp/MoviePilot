"""2.2.18
增加音乐音质订阅条件、洗版状态和整理历史参数

Revision ID: e8b1c4d7a2f9
Revises: d4f6a8c2e1b7
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "e8b1c4d7a2f9"
down_revision = "d4f6a8c2e1b7"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    """检查数据表是否已存在指定字段。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _add_columns(table_name: str, columns: list[sa.Column]) -> None:
    """为指定数据表幂等增加字段。"""
    for column in columns:
        if not _has_column(table_name, column.name):
            op.add_column(table_name, column)


def upgrade() -> None:
    """增加音乐音质筛选、洗版快照和整理历史字段。"""
    def subscribe_filter_columns() -> list[sa.Column]:
        """构造可分别绑定到订阅表和历史表的筛选字段。"""
        return [
            sa.Column("audio_quality", sa.String(), nullable=True),
            sa.Column("audio_format", sa.String(), nullable=True),
            sa.Column("min_bitrate", sa.Integer(), nullable=True),
            sa.Column("min_bit_depth", sa.Integer(), nullable=True),
            sa.Column("min_sample_rate", sa.Integer(), nullable=True),
        ]

    _add_columns("subscribe", [*subscribe_filter_columns(),
                                sa.Column("current_audio_format", sa.String(), nullable=True),
                                sa.Column("current_bitrate", sa.Integer(), nullable=True),
                                sa.Column("current_bit_depth", sa.Integer(), nullable=True),
                                sa.Column("current_sample_rate", sa.Integer(), nullable=True)])
    _add_columns("subscribehistory", [*subscribe_filter_columns(),
                                       sa.Column("current_priority", sa.Integer(), nullable=True),
                                       sa.Column("current_audio_format", sa.String(), nullable=True),
                                       sa.Column("current_bitrate", sa.Integer(), nullable=True),
                                       sa.Column("current_bit_depth", sa.Integer(), nullable=True),
                                       sa.Column("current_sample_rate", sa.Integer(), nullable=True)])
    _add_columns("transferhistory", [
        sa.Column("audio_format", sa.String(), nullable=True),
        sa.Column("audio_lossless", sa.Boolean(), nullable=True),
        sa.Column("bit_depth", sa.Integer(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("bitrate", sa.Integer(), nullable=True),
    ])

    # 只升级系统旧默认模板；用户编辑过的模板保持原样。
    legacy_organize = """
{
    'title': '{{ title_year }}'
            '{% if season_episode %} {{ season_episode }}{% endif %} 已入库',
    'text': '{% if vote_average %}评分：{{ vote_average }}，{% endif %}'
            '类型：{{ type }}'
            '{% if category %}，类别：{{ category }}{% endif %}'
            '{% if resource_term %}，质量：{{ resource_term }}{% endif %}，'
            '共{{ file_count }}个文件，大小：{{ total_size }}'
            '{% if err_msg %}，以下文件处理失败：{{ err_msg }}{% endif %}'
}"""
    legacy_download = """
{
    'title': '{{ title_year }}'
            '{% if download_episodes %} {{ season_fmt }} {{ download_episodes }}{% else %}{{ season_episode }}{% endif %} 开始下载',
    'text': '{% if site_name %}站点：{{ site_name }}{% endif %}'
            '{% if resource_term %}\\n质量：{{ resource_term }}{% endif %}'
            '{% if size %}\\n大小：{{ size }}{% endif %}'
            '{% if torrent_title %}\\n种子：{{ torrent_title }}{% endif %}'
            '{% if pubdate %}\\n发布时间：{{ pubdate }}{% endif %}'
            '{% if freedate %}\\n免费时间：{{ freedate }}{% endif %}'
            '{% if seeders %}\\n做种数：{{ seeders }}{% endif %}'
            '{% if volume_factor %}\\n促销：{{ volume_factor }}{% endif %}'
            '{% if hit_and_run %}\\nHit&Run：{{ hit_and_run }}{% endif %}'
            '{% if labels %}\\n标签：{{ labels }}{% endif %}'
            '{% if description %}\\n描述：{{ description }}{% endif %}'
}"""
    music_organize = """
{
    'title': '{{ title_year }}{% if track_number %} #{{ track_number }}{% endif %} 已入库',
    'text': '类型：{{ type }}{% if category %}，类别：{{ category }}{% endif %}'
            '{% if type == "音乐" and artist %}\\n艺术家：{{ artist }}{% endif %}'
            '{% if type == "音乐" and album %}\\n专辑：{{ album }}{% endif %}'
            '{% if type == "音乐" and audio_specs %}\\n音质：{{ audio_specs }}{% endif %}'
            '{% if resource_term %}，质量：{{ resource_term }}{% endif %}'
            '，共{{ file_count }}个文件，大小：{{ total_size }}'
            '{% if err_msg %}，以下文件处理失败：{{ err_msg }}{% endif %}'
}"""
    music_download = """
{
    'title': '{{ title_year }}{% if track_number %} #{{ track_number }}{% endif %}'
            '{% if download_episodes %} {{ season_fmt }} {{ download_episodes }}{% else %}{{ season_episode }}{% endif %} 开始下载',
    'text': '{% if site_name %}站点：{{ site_name }}{% endif %}'
            '{% if type == "音乐" and artist %}\\n艺术家：{{ artist }}{% endif %}'
            '{% if type == "音乐" and album %}\\n专辑：{{ album }}{% endif %}'
            '{% if type == "音乐" and audio_specs %}\\n音质：{{ audio_specs }}{% endif %}'
            '{% if resource_term %}\\n质量：{{ resource_term }}{% endif %}'
            '{% if size %}\\n大小：{{ size }}{% endif %}'
            '{% if torrent_title %}\\n种子：{{ torrent_title }}{% endif %}'
            '{% if pubdate %}\\n发布时间：{{ pubdate }}{% endif %}'
            '{% if freedate %}\\n免费时间：{{ freedate }}{% endif %}'
            '{% if seeders %}\\n做种数：{{ seeders }}{% endif %}'
            '{% if volume_factor %}\\n促销：{{ volume_factor }}{% endif %}'
            '{% if hit_and_run %}\\nHit&Run：{{ hit_and_run }}{% endif %}'
            '{% if labels %}\\n标签：{{ labels }}{% endif %}'
            '{% if description %}\\n描述：{{ description }}{% endif %}'
}"""
    systemconfig = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    connection = op.get_bind()
    config_key = "NotificationTemplates"
    row = connection.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == config_key)
    ).first()
    templates = dict(row[0] or {}) if row else {}
    changed = False
    for template_key, legacy, replacement in (
        ("organizeSuccess", legacy_organize, music_organize),
        ("downloadAdded", legacy_download, music_download),
    ):
        if str(templates.get(template_key) or "").strip() == legacy.strip():
            templates[template_key] = replacement
            changed = True
    if changed:
        connection.execute(
            systemconfig.update().where(systemconfig.c.key == config_key).values(
                value=templates
            )
        )


def downgrade() -> None:
    """移除音乐音质相关字段。"""
    table_columns = {
        "subscribe": [
            "current_sample_rate", "current_bit_depth", "current_bitrate", "current_audio_format",
            "min_sample_rate", "min_bit_depth", "min_bitrate", "audio_format", "audio_quality",
        ],
        "subscribehistory": [
            "current_sample_rate", "current_bit_depth", "current_bitrate", "current_audio_format",
            "current_priority", "min_sample_rate", "min_bit_depth", "min_bitrate", "audio_format",
            "audio_quality",
        ],
        "transferhistory": ["bitrate", "sample_rate", "bit_depth", "audio_lossless", "audio_format"],
    }
    for table_name, columns in table_columns.items():
        for column_name in columns:
            if _has_column(table_name, column_name):
                op.drop_column(table_name, column_name)
