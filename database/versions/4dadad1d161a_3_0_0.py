"""3.0.0
V3 大版本初始化默认通知模板

Revision ID: 4dadad1d161a
Revises: e8b1c4d7a2f9
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa
from app.runtime.log import logger

# revision identifiers, used by Alembic.
revision = "4dadad1d161a"
down_revision = "e8b1c4d7a2f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # V3 为大版本升级，通知模板直接覆盖用户旧设置，且迁移只执行一次；
    # 默认模板同时兼容影视与音乐（音乐的下载、入库通知补齐艺术家/专辑/音质信息）。
    # 覆盖前先将用户现有模板完整输出到日志，作为备份供用户恢复参考。
    systemconfig = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    connection = op.get_bind()
    key = "NotificationTemplates"
    row = connection.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == key)
    ).first()
    old_value = row[0] if row else None
    if old_value:
        logger.info(f"即将使用 V3 默认通知模板覆盖用户现有通知模板，现有模板内容备份如下：\n{old_value}")
    value = {
        "organizeSuccess": """
{
    'title': '{{ title_year }}{% if track_number %} #{{ track_number }}{% endif %}'
            '{% if season_episode %} {{ season_episode }}{% endif %} 已入库',
    'text': '类型：{{ type }}{% if category %}，类别：{{ category }}{% endif %}'
            '{% if type == "音乐" and artist %}\\n艺术家：{{ artist }}{% endif %}'
            '{% if type == "音乐" and album %}\\n专辑：{{ album }}{% endif %}'
            '{% if type == "音乐" and audio_specs %}\\n音质：{{ audio_specs }}{% endif %}'
            '{% if resource_term %}，质量：{{ resource_term }}{% endif %}'
            '，共{{ file_count }}个文件，大小：{{ total_size }}'
            '{% if err_msg %}，以下文件处理失败：{{ err_msg }}{% endif %}'
}""",
        "downloadAdded": """
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
}""",
        "subscribeAdded": "{'title': '{{ title_year }}{% if season_fmt %} {{ season_fmt }}{% endif %} 已添加订阅'}",
        "subscribeComplete": """
{
    'title': '{{ title_year }}'
            '{% if season_fmt %} {{ season_fmt }}{% endif %} 已完成{{ msgstr }}',
    'text': '{% if vote_average %}评分：{{ vote_average }}{% endif %}'
            '{% if username %}，来自用户：{{ username }}{% endif %}'
            '{% if actors %}\\n演员：{{ actors }}{% endif %}'
            '{% if overview %}\\n简介：{{ overview }}{% endif %}'
}"""
    }
    if row and row[0] != value:
        connection.execute(
            systemconfig.update().where(systemconfig.c.key == key).values(
                value=value
            )
        )
    elif not row:
        connection.execute(systemconfig.insert().values(key=key, value=value))


def downgrade() -> None:
    pass
