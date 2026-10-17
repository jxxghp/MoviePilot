"""
读取版本说明，并根据本地 SQLite 数据库与已注册模型自动生成迁移脚本。

本入口调用 Alembic 生成版本文件，不执行生成的升级或降级操作。
"""

import importlib
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.command import revision as alembic_revision

from app.runtime.config import settings

# 导入模块，避免建表缺失
for module in Path(__file__).with_name("models").glob("*.py"):
    importlib.import_module(f"app.db.models.{module.stem}")

db_version = input("请输入版本号：")
db_location = settings.CONFIG_PATH / 'user.db'
script_location = settings.ROOT_PATH / 'database'
alembic_cfg = AlembicConfig()
alembic_cfg.set_main_option('script_location', str(script_location))
alembic_cfg.set_main_option('sqlalchemy.url', f"sqlite:///{db_location}")
alembic_revision(alembic_cfg, db_version, True)
