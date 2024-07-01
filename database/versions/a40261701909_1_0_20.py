"""1.0.20

Revision ID: a40261701909
Revises: ae9d8ed8df97
Create Date: 2024-05-22 19:16:21.374806

"""
import json
from pathlib import Path

from alembic import op

from app.core.config import Settings

# revision identifiers, used by Alembic.
revision = 'a40261701909'
down_revision = 'ae9d8ed8df97'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    升级目录配置
    """
    # 实例化配置
    _settings = Settings(
        _env_file=Settings().CONFIG_PATH / "app.env",
        _env_file_encoding="utf-8"
    )
    # 下载目录配置升级
    download_dirs = []
    if _settings.DOWNLOAD_MOVIE_PATH:
        download_dirs.append({
            "type": "download",
            "name": "电影目录",
            "path": _settings.DOWNLOAD_MOVIE_PATH,
            "media_type": "电影",
            "category": "",
            "auto_category": True if _settings.DOWNLOAD_CATEGORY else False,
            "priority": 1
        })
    if _settings.DOWNLOAD_TV_PATH:
        download_dirs.append({
            "type": "download",
            "name": "电视剧目录",
            "path": _settings.DOWNLOAD_TV_PATH,
            "media_type": "电视剧",
            "category": "",
            "auto_category": True if _settings.DOWNLOAD_CATEGORY else False,
            "priority": 2
        })
    if _settings.DOWNLOAD_PATH:
        download_dirs.append({
            "type": "download",
            "name": "下载目录",
            "path": _settings.DOWNLOAD_PATH,
            "media_type": "",
            "category": "",
            "auto_category": True if _settings.DOWNLOAD_CATEGORY else False,
            "priority": 4
        })

    # 插入数据库，报错的话则更新
    if download_dirs:
        download_dirs_value = json.dumps(download_dirs)
        try:
            op.execute(f"INSERT INTO systemconfig (key, value) VALUES ('DownloadDirectories', '{download_dirs_value}');")
        except Exception as e:
            op.execute(f"UPDATE systemconfig SET value = '{download_dirs_value}' WHERE key = 'DownloadDirectories';")

    # 媒体库目录配置升级
    library_dirs = []
    if _settings.LIBRARY_PATH:
        for library_path in _settings.LIBRARY_PATH.split(","):
            if _settings.LIBRARY_MOVIE_NAME:
                library_dirs.append({
                    "type": "library",
                    "name": "电影目录",
                    "path": str(Path(library_path) / _settings.LIBRARY_MOVIE_NAME),
                    "media_type": "电影",
                    "category": "",
                    "auto_category": True if _settings.LIBRARY_CATEGORY else False,
                    "scrape": True if _settings.SCRAP_METADATA else False,
                    "priority": 1
                })
            if _settings.LIBRARY_TV_NAME:
                library_dirs.append({
                    "type": "library",
                    "name": "电视剧目录",
                    "path": str(Path(library_path) / _settings.LIBRARY_TV_NAME),
                    "media_type": "电视剧",
                    "category": "",
                    "auto_category": True if _settings.LIBRARY_CATEGORY else False,
                    "scrape": True if _settings.SCRAP_METADATA else False,
                    "priority": 2
                })
            library_dirs.append({
                "type": "library",
                "name": "媒体库目录",
                "path": library_path,
                "media_type": "",
                "category": "",
                "auto_category": True if _settings.LIBRARY_CATEGORY else False,
                "scrape": True if _settings.SCRAP_METADATA else False,
                "priority": 4
            })
    # 插入数据库，报错的话则更新
    if library_dirs:
        library_dirs_value = json.dumps(library_dirs)
        try:
            op.execute(f"INSERT INTO systemconfig (key, value) VALUES ('LibraryDirectories', '{library_dirs_value}');")
        except Exception as e:
            op.execute(f"UPDATE systemconfig SET value = '{library_dirs_value}' WHERE key = 'LibraryDirectories';")


def downgrade() -> None:
    pass
