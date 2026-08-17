"""
ORM 声明式写法的 2.0 迁移不变量。

基类由 1.x 的 @as_declarative() 迁移到 2.0 的 DeclarativeBase，列声明由 Column()
迁移到 mapped_column()。两者产出的表定义必须完全等价——迁移只改写法，不改
schema，否则会与既有数据库和 alembic 迁移链产生偏差。

仓内模型已全部迁移到 Mapped[] 注解，__allow_unmapped__ 已随之移除；这些测试守住
「标志不在、且仓内没有任何需要它的写法」这一组不变量，三条断言互为前提：标志一旦
被加回来，下面两条 AST 守卫就会失去意义（legacy 写法将不再报错，只会静默通过）。
"""
import ast
import re
from pathlib import Path

import pytest
from sqlalchemy.orm import DeclarativeBase

from app.db import Base
from app.db.models import load_all_models


load_all_models()
from app.db.base import get_id_column

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PACKAGE = PROJECT_ROOT / "app" / "db"

# 匹配 Mapped[...]、orm.Mapped[...] 等带限定前缀的写法；不带下标的裸 Mapped 不算
MAPPED_ANNOTATION = re.compile(r"^(?:[\w.]+\.)?Mapped\[")

SQLALCHEMY = "sqlalchemy"


def _sqlalchemy_column_names(tree: ast.Module):
    """
    解析该模块的 import，产出「在本文件中指向 sqlalchemy.Column 的全部名字」。

    照字面量 "Column" 硬匹配会两头出错：一头漏掉 ``from sqlalchemy import Column as Col``
    这类别名，另一头误伤同名的无关符号（rich.table.Column 就叫这个名字，而 rich 是本仓
    依赖）。按 import 绑定判定，两个方向都准，代价只是多解析一遍 import。

    注意 ``sqlalchemy.Column`` 与 ``sqlalchemy.orm.mapped_column`` 是两个东西，这里
    只认前者：mapped_column 名字里虽然也有 column，但它是 2.0 的正确写法，不该被拦。
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # from sqlalchemy import Column [as Col] / from sqlalchemy.sql import Column
            module = node.module or ""
            if module == SQLALCHEMY or module.startswith(f"{SQLALCHEMY}."):
                names.update(alias.asname or alias.name
                             for alias in node.names if alias.name == "Column")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != SQLALCHEMY and not alias.name.startswith(f"{SQLALCHEMY}."):
                    continue
                # import sqlalchemy as sa 绑定 sa；import sqlalchemy[.orm] 绑定顶层包名
                bound = alias.asname or alias.name.split(".")[0]
                names.add(f"{bound}.Column")
    return names


def _class_level_column_assignments(py_file: Path):
    """
    产出该文件中全部「类级 Column(...) 赋值」，形如 (类名, 属性名, 行号)。

    取 ClassDef 直接子语句中的 Assign 与 AnnAssign：前者是 1.x 的典型写法
    ``foo = Column(String)``（完全没有注解，因此上面那条注解守卫对它无感）；后者兜住
    ``foo: Mapped[str] = Column(String)`` 这种注解已迁完、构造还留在 1.x 的半吊子状态。
    赋值右侧用 ast.walk 递归找 Call，包一层（如 ``deferred(Column(...))``）也跑不掉。
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    column_names = _sqlalchemy_column_names(tree)
    if not column_names:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or stmt.value is None:
                continue
            if not any(isinstance(sub, ast.Call) and ast.unparse(sub.func) in column_names
                       for sub in ast.walk(stmt.value)):
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            yield (node.name, ", ".join(ast.unparse(t) for t in targets), stmt.lineno)


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


def test_allow_unmapped_is_not_set():
    """
    __allow_unmapped__ 必须保持缺席。

    它此前唯一的存在理由是「仓外插件可能继承本 Base 自定义 legacy 注解模型」；插件
    生态确定迭代后这条理由已不成立，标志随之移除。这里断言它不存在而不是删掉用例：
    这个标志的危害在于**静默**——加回来之后，2.0 声明式系统不再拒绝未包裹在
    Mapped[] 中的类级注解，本文件另外两条 AST 守卫拦下的 legacy 写法就会一路通过
    映射，直到运行期以「列不存在」的形式暴露。没有这条断言，谁把它加回来都没人知道。

    不用 getattr(..., False) is False：那样无法区分「没有这个属性」和
    「显式设成了 False」，而后者同样是把这个开关重新引入了代码。
    """
    assert not hasattr(Base, "__allow_unmapped__")


def test_no_unmapped_class_level_annotations_in_db_package():
    """
    app/db 内不存在非 Mapped[] 的类级注解——这是移除 __allow_unmapped__ 的前提。

    上一条用例断言标志不在，本条断言仓内确实不需要它。两者缺一不可：只断言标志不在，
    则某天有人补进一条 legacy 注解、发现 import 就炸、顺手把标志加回来，上一条用例
    会跟着被改绿；只断言注解形状，则标志被悄悄加回来时没有任何用例会响。

    这条用例还把一句会腐烂的注释钉成了可执行断言：base.py 的 docstring 上一版写着
    「现有 22 个模型仍是 legacy Column() 写法」，在 329 列全部迁移完之后仍原样留了
    很久，主动误导读者。

    扫全部类而非只扫 Base 子类：判定 Base 子类要么靠运行期 Base.__subclasses__()，
    要么靠 AST 解析基类名。前者会漏掉「新增了模型文件但还没接进 app/db/models/__init__.py」
    的情况——恰恰是最可能带进 legacy 注解的场景；后者一遇 mixin 或跨文件继承就不准。
    纯静态扫全部类没有这个盲区，而且当下不需要任何白名单：DbOper 这类非 ORM 类本身
    就没有类级注解，天然不受影响。

    变红时怎么办（二选一，别直接把用例删了）：
    1. 常见情况——新模型忘了用 2.0 写法，把它改成 mapped_column() + Mapped[] 即可；
    2. 若确实需要一条非映射的类级属性，用 ClassVar 显式声明——2.0 的声明式系统会
       跳过 ClassVar，这条路不需要 __allow_unmapped__。本守卫比 SQLAlchemy 更严，
       当下 app/db 里没有这种属性，所以不预留白名单；真要引入时请显式放宽本守卫
       （在 MAPPED_ANNOTATION 之外放行 ClassVar），而不是把那个标志加回来。
    """
    offenders = [
        f"{py_file.relative_to(PROJECT_ROOT)}:{lineno} {cls_name}.{attr} -> {annotation}"
        for py_file in sorted(DB_PACKAGE.rglob("*.py"))
        for cls_name, attr, annotation, lineno in _class_level_annotations(py_file)
        if not MAPPED_ANNOTATION.match(annotation)
    ]
    assert not offenders, (
        "app/db 内出现了非 Mapped[] 的类级注解，而 __allow_unmapped__ 已移除，"
        "声明式系统会直接拒绝它们：\n"
        + "\n".join(f"  {item}" for item in offenders)
        + "\n请改用 mapped_column() + Mapped[]；若这条注解确实不该被映射，"
          "用 ClassVar 声明并显式放宽本守卫，不要把 __allow_unmapped__ 加回来。"
    )


def test_no_legacy_column_assignments_in_db_package():
    """
    app/db 内不存在 1.x 的 Column() 列声明——一律 mapped_column() + Mapped[]。

    这条补的是上一条注解守卫的缝：它只校验「已有的注解是不是 Mapped[]」，对**完全没有
    类级注解**的裸 ``foo = Column(String)`` 无感。上游新增的 app/db/models/agenttaskrun.py
    整整 254 行、零注解、全是 Column()，就是这么从守卫底下溜过去的——直到 pyright app/db
    从 0 退回 3 errors 才被发现。这一批工作的卖点是「app/db 全量迁到 SQLAlchemy 2.0」，
    守卫拦不住 1.x 写法回流，卖点就只是一次性的。

    与上一条分开报而不是合并：两者失败原因不同（注解形状 vs 列构造 API），修法也不同，
    合成一条只会让 offender 列表里混着两种毛病、报错文案被迫说得含糊。

    Column 按 import 绑定识别而非按名字字面量，别名（``from sqlalchemy import Column as Col``）
    与限定写法（``sa.Column``）都算数，详见 _sqlalchemy_column_names。

    变红时怎么办：把 ``foo = Column(String)`` 改成
    ``foo: Mapped[str] = mapped_column(String)``（Optional 列写 Mapped[Optional[str]]），
    主键用 get_id_column()。注意 __allow_unmapped__ 已经移除，没有任何东西替你兜底：
    仓内已全量 2.0，见上一条用例。
    """
    offenders = [
        f"{py_file.relative_to(PROJECT_ROOT)}:{lineno} {cls_name}.{attr}"
        for py_file in sorted(DB_PACKAGE.rglob("*.py"))
        for cls_name, attr, lineno in _class_level_column_assignments(py_file)
    ]
    assert not offenders, (
        "app/db 内出现了 1.x 的 Column() 列声明，2.0 迁移被回流破坏：\n"
        + "\n".join(f"  {item}" for item in offenders)
        + "\n请改用 mapped_column() + Mapped[] 注解（主键用 get_id_column()）。"
          "仓内已全量迁至 2.0，__allow_unmapped__ 已移除，没有兜底可依赖。"
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
