"""2.0.6

Revision ID: a295e41830a6
Revises: ecf3c693fdf3
Create Date: 2024-11-14 12:49:13.838120

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey

# revision identifiers, used by Alembic.
revision = 'a295e41830a6'
down_revision = 'ecf3c693fdf3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    # 初始化AList存储
    _systemconfig = SystemConfigOper()
    _storages = _systemconfig.get(SystemConfigKey.Storages)
    if _storages:
        if "alist" not in [storage["type"] for storage in _storages]:
            _storages.append({
                "type": "alist",
                "name": "AList",
                "config": {}
            })
            _systemconfig.set(SystemConfigKey.Storages, _storages)

    # ### end Alembic commands ###


def downgrade() -> None:
    pass
