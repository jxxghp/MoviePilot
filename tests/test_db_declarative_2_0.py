"""
ORM 声明式写法的 2.0 迁移不变量。

基类由 1.x 的 @as_declarative() 迁移到 2.0 的 DeclarativeBase，列声明由 Column()
迁移到 mapped_column()。两者产出的表定义必须完全等价——迁移只改写法，不改
schema，否则会与既有数据库和 alembic 迁移链产生偏差。

现有模型仍使用 legacy 的「无 Mapped[] 注解」风格，靠 __allow_unmapped__ 让
声明式系统忽略这类注解；这些测试同时守住这个前提，避免它被误删后模型集体报错。
"""
from pathlib import Path

import pytest
from sqlalchemy.orm import DeclarativeBase

import app.db.models  # noqa: F401  确保全部模型完成注册
from app.db import Base
from app.db.base import get_id_column


def test_base_uses_declarative_base():
    """
    基类必须是 2.0 的 DeclarativeBase，而不是 1.x 的 as_declarative 产物。
    """
    assert issubclass(Base, DeclarativeBase)


def test_allow_unmapped_is_enabled():
    """
    模型仍是 legacy 注解风格，__allow_unmapped__ 必须开启。

    一旦被移除，2.0 的声明式系统会尝试解释未包裹在 Mapped[] 中的类级注解并直接
    报错——表现为全部模型导入失败，属于启动期崩溃。
    """
    assert getattr(Base, "__allow_unmapped__", False) is True


def test_id_column_factory_returns_mapped_column():
    """
    主键工厂必须产出 mapped_column，否则主键仍是 legacy 构造。
    """
    column = get_id_column()
    # mapped_column() 返回 MappedColumn，Column() 返回 Column
    assert type(column).__name__ == "MappedColumn"


def test_all_models_registered_and_mapped():
    """
    全部模型都应完成映射并拥有主键——迁移过程中最容易出现的失败是某个模型
    因导入缺失而未注册，此时它不会报错，只是悄悄从 metadata 里消失。
    """
    tables = Base.metadata.tables
    assert len(tables) >= 20, f"注册的表过少（{len(tables)}），可能有模型未完成导入"
    without_pk = [name for name, table in tables.items() if not table.primary_key.columns]
    assert not without_pk, f"以下表缺少主键: {without_pk}"


@pytest.mark.parametrize("table_name", ["transferhistory", "downloadhistory", "subscribe"])
def test_core_tables_keep_column_definitions(table_name):
    """
    核心业务表的列定义必须完整。抽查而非全量比对：全量等价性已在迁移时以
    schema 快照逐列核对过，这里只防止后续改动悄悄丢列。
    """
    table = Base.metadata.tables[table_name]
    assert len(table.columns) > 5
    assert table.primary_key.columns, f"{table_name} 缺少主键"
    # 主键应为自增整型 id
    primary_key = list(table.primary_key.columns)[0]
    assert primary_key.name == "id"
    assert "INTEGER" in str(primary_key.type).upper()


def test_no_legacy_declarative_api_in_base():
    """
    base 模块不应再引用 1.x 的 as_declarative，避免两种风格并存。
    """
    source = Path("app/db/base.py").read_text(encoding="utf-8")
    assert "as_declarative" not in source
