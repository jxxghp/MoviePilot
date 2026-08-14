"""
ORM 声明式写法的 2.0 迁移不变量。

基类由 1.x 的 @as_declarative() 迁移到 2.0 的 DeclarativeBase，列声明由 Column()
迁移到 mapped_column()。两者产出的表定义必须完全等价——迁移只改写法，不改
schema，否则会与既有数据库和 alembic 迁移链产生偏差。

仓内模型已全部迁移到 Mapped[] 注解，但 __allow_unmapped__ 仍需保留以兼容仓外插件
自定义的 legacy 模型；这些测试守住这个标志，避免它被当作迁移残留误删。
"""
import ast
import re
from pathlib import Path

import pytest
from sqlalchemy.orm import DeclarativeBase

import app.db.models  # noqa: F401  确保全部模型完成注册
from app.db import Base
from app.db.base import get_id_column

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PACKAGE = PROJECT_ROOT / "app" / "db"

# 匹配 Mapped[...]、orm.Mapped[...] 等带限定前缀的写法；不带下标的裸 Mapped 不算
MAPPED_ANNOTATION = re.compile(r"^(?:[\w.]+\.)?Mapped\[")


def _class_level_annotations(py_file: Path):
    """
    产出该文件中全部类级注解，形如 (类名, 属性名, 注解源码, 行号)。

    只取 ClassDef 直接子语句中的 AnnAssign：函数体内的局部注解、模块级注解都不算
    类级注解；``if TYPE_CHECKING:`` 块里的注解运行期根本不存在，声明式系统也看不到，
    同样不在此列。
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign):
                yield (node.name, ast.unparse(stmt.target),
                       ast.unparse(stmt.annotation), stmt.lineno)


def test_base_uses_declarative_base():
    """
    基类必须是 2.0 的 DeclarativeBase，而不是 1.x 的 as_declarative 产物。
    """
    assert issubclass(Base, DeclarativeBase)


def test_allow_unmapped_is_enabled():
    """
    __allow_unmapped__ 必须保持开启，即便仓内模型已全部用上 Mapped[] 注解。

    它现在守的不是仓内模型，而是仓外插件：插件可以继承本 Base 自定义模型，其中不乏
    仍是 legacy 注解风格的。一旦这个标志被当作迁移残留删掉，2.0 的声明式系统会尝试
    解释那些未包裹在 Mapped[] 中的类级注解并直接报错——插件在 import 期就崩，
    而仓内测试全绿，问题只会在装了插件的用户那里爆出来。
    """
    assert getattr(Base, "__allow_unmapped__", False) is True


def test_no_unmapped_class_level_annotations_in_db_package():
    """
    app/db 内不存在非 Mapped[] 的类级注解——即 __allow_unmapped__ 的保留理由确实是
    「兼容仓外插件」，而不是「仓内还有模型离不开它」。

    上一条用例只断言这个标志开着，守不住它的**理由**。而理由恰恰是会腐烂的那部分：
    base.py 的 docstring 上一版还写着「现有 22 个模型仍是 legacy Column() 写法」，
    在 329 列全部迁移完之后仍原样留了很久，主动误导读者。这条用例把那句话钉成可执行
    断言——只要仓内重新出现 legacy 注解，说明「仅为仓外兼容」的说法不再成立，红给你看。

    扫全部类而非只扫 Base 子类：判定 Base 子类要么靠运行期 Base.__subclasses__()，
    要么靠 AST 解析基类名。前者会漏掉「新增了模型文件但还没接进 app/db/models/__init__.py」
    的情况——恰恰是最可能带进 legacy 注解的场景；后者一遇 mixin 或跨文件继承就不准。
    纯静态扫全部类没有这个盲区，而且当下不需要任何白名单：DbOper 这类非 ORM 类本身
    就没有类级注解，天然不受影响。

    变红时怎么办（二选一，别直接把用例删了）：
    1. 常见情况——新模型忘了用 2.0 写法，把它改成 mapped_column() + Mapped[] 即可；
    2. 若确实需要一条非映射的类级注解（例如 ClassVar），那么「仓内不再需要
       __allow_unmapped__」这个前提就变了，请连同 base.py 中 Base 的 docstring
       一起订正，别让注释和代码再次走散。
    """
    offenders = [
        f"{py_file.relative_to(PROJECT_ROOT)}:{lineno} {cls_name}.{attr} -> {annotation}"
        for py_file in sorted(DB_PACKAGE.rglob("*.py"))
        for cls_name, attr, annotation, lineno in _class_level_annotations(py_file)
        if not MAPPED_ANNOTATION.match(annotation)
    ]
    assert not offenders, (
        "app/db 内出现了非 Mapped[] 的类级注解，__allow_unmapped__ 已不只是为仓外插件保留：\n"
        + "\n".join(f"  {item}" for item in offenders)
        + "\n请改用 mapped_column() + Mapped[]；若这条注解确实不该被映射，"
          "则需同步订正 app/db/base.py 中 Base 的 docstring。"
    )


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
