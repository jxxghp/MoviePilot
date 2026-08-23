"""插件只能 import SDK 与公开面。

插件是第三方代码，不该被宿主的内部分层约束，因此它们不进依赖矩阵；但「不受内部分层
约束」不等于「什么都能 import」——恰恰相反，正因为插件不在宿主的重构射程内，它能依赖
什么才必须是一条独立的、说得出边界的规则。宿主改一次内部路径就让一批插件失效，责任
在宿主没有给出口，不在插件写错了路径。

公开面按「宿主自己不许用的那一面」定义，两条各有独立判据：

- ``app.sdk``：唯一的插件门面。它对外承诺什么由 ``app/sdk/_exports.py`` 的快照登记，
  宿主实现层反过来被禁止依赖它（见 test_architecture_dependencies）。下划线开头的子模块
  是 SDK 自己的生成物与内部实现，不在承诺之内。
- ``app.schemas``：惰性兼容聚合入口。宿主被禁止使用这个聚合入口、必须走精确子模块
  （见 test_host_code_uses_precise_schema_modules），而插件反过来只许用聚合入口——
  子模块的划分是宿主的内部组织，聚合入口才是生成并版本化的公开面。

插件自己的包内导入照常，跨插件导入不在公开面内：另一个插件装没装、是什么版本，都不是
本插件能假定的。

判据的被测对象是本文件内的合成插件源码，不是磁盘上的文件。``app/plugins/`` 是运行期的
安装挂载点，不随仓入库任何扩展：拿它当扫描范围，在全新克隆上会扫到空集而恒真，在开发机
上又会去判第三方插件——两头都判错了对象。合成样本反过来把判据钉死：一份只用公开面的
源码必须零违规，每一类越界各有一份源码必须被抓到且给出对应改法，样本集本身非空由单独
一条断言守住。挂载点确实没有随仓入库的扩展，由 git 索引另立一条断言核对。
"""

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.sdk._exports import SDK_DECLARED_EXPORTS, SDK_REQUIRED_EXPORTS

PROJECT_ROOT = Path(__file__).parents[1]
# 扩展的安装挂载点，纯数据目录，不随仓入库任何扩展
PLUGIN_MOUNT_POINT = "app/plugins"
# 本门禁自身在 git 索引里的路径，用于确认读到的确实是本仓的索引
GATE_ENTRY = "tests/test_plugin_import_boundary.py"
# 插件所在的包，插件包内导入按它加插件包名判定
PLUGIN_PACKAGE = "app.plugins"
# 插件门面根包；下划线开头的子模块是 SDK 自己的生成物与内部实现，不对插件承诺
SDK_ROOT = "app.sdk"
# schema 惰性兼容聚合入口，插件只用它，不下探子模块
SCHEMA_FACADE = "app.schemas"
# 动态导入的入口函数名，常量参数与静态 import 同等看待
DYNAMIC_IMPORT_CALLS = frozenset({"import_module", "__import__"})


def sdk_symbol_index() -> dict[tuple[str, str], str]:
    """建立 canonical 符号到 SDK 取用位置的反查表。

    两个来源都收：别名推导覆盖有旧路径的符号，门面自报覆盖声明类这类新出口，合起来
    才能对绝大多数越界 import 直接给出改法。

    :return: ``{(来源模块, 来源符号): SDK 取用位置}``
    """
    index: dict[tuple[str, str], str] = {}
    for sdk_name, symbols in SDK_DECLARED_EXPORTS.items():
        for name, source in symbols.items():
            index[tuple(source)] = f"{sdk_name}.{name}"
    for sdk_name, symbols in SDK_REQUIRED_EXPORTS.items():
        for name, sources in symbols.items():
            for source in sources:
                index.setdefault(tuple(source), f"{sdk_name}.{name}")
    return index


def sdk_module_index() -> dict[str, set[str]]:
    """建立 canonical 模块到承载其符号的 SDK 门面模块的反查表。

    整模块导入给不出符号名，只能回答「这个模块的东西在哪几个门面里」。

    :return: ``{来源模块: {SDK 门面模块}}``
    """
    index: dict[str, set[str]] = {}
    for (source_module, _name), replacement in sdk_symbol_index().items():
        index.setdefault(source_module, set()).add(replacement.rpartition(".")[0])
    return index


SYMBOL_INDEX = sdk_symbol_index()
MODULE_INDEX = sdk_module_index()

# 只用公开面写成的插件包名，样本里的自引用绝对导入按它判定
COMPLIANT_PACKAGE = "compliantplugin"
# 只用公开面写成的插件源码：SDK 门面、schema 聚合入口、本插件包内导入各写到一次，
# 静态与动态两种导入形态也各写到一次
COMPLIANT_SOURCES: dict[str, str] = {
    "__init__.py": '''
from importlib import import_module
from typing import List, Optional

from app.schemas import FileItem
from app.sdk.declarations import ServiceInstanceDeclaration
from app.sdk.extension import _PluginBase
from app.sdk.logging import logger

from .storage import SampleStorage

STORAGE_TYPE = "sample"


class CompliantPlugin(_PluginBase):
    plugin_name = "CompliantPlugin"

    def init_plugin(self, config=None):
        logger.info("载入合规样本插件")
        self._services = import_module("app.sdk.services")

    def get_state(self) -> bool:
        return True

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        return [
            ServiceInstanceDeclaration(
                capability="storage",
                type=STORAGE_TYPE,
                name="样本存储",
                impl=SampleStorage,
            )
        ]

    def probe(self, item: FileItem) -> bool:
        return bool(item.path)
''',
    "storage.py": '''
from typing import Optional

import app.sdk.storage
from app.plugins.compliantplugin.naming import STORAGE_NAME
from app.schemas import FileItem


class SampleStorage(app.sdk.storage.StorageBase):
    schema = None
    transtype = {}

    def check(self) -> bool:
        return True

    def label(self) -> str:
        return STORAGE_NAME

    def head(self) -> Optional[FileItem]:
        return None
''',
    "naming.py": '''
STORAGE_NAME = "样本存储"
''',
}


@dataclass(frozen=True)
class ViolationSample:
    """一份刻意越界的插件源码样本，连同它应当被判成什么。

    :param package: 发起导入的插件包名
    :param source: 插件源码
    :param expected: ``(模块路径, 符号名, 改法应当包含的文字)`` 列表
    """

    package: str
    source: str
    expected: tuple[tuple[str, str, str], ...]


# 每一类越界各一份样本；改法文本按 remedy() 的分支逐类核对，抓到但给错改法同样算失守
VIOLATION_SAMPLES: dict[str, ViolationSample] = {
    "宿主内部路径": ViolationSample(
        package="hostreacher",
        source='''
from app.db.models.user import User
from app.runtime.config import Settings
from app.sdk.extension import _PluginBase


class HostReacher(_PluginBase):
    plugin_name = "HostReacher"

    def init_plugin(self, config=None):
        self._settings = Settings()
        self._user_model = User
''',
        expected=(
            ("app.db.models.user", "User", "SDK 未提供该出口"),
            ("app.runtime.config", "Settings", "改用 app.sdk.config.Settings"),
        ),
    ),
    "schema 子模块": ViolationSample(
        package="schemadiver",
        source='''
import app.schemas.file
from app.schemas.types import MediaType


def parse(kind: str) -> MediaType:
    return MediaType(kind)
''',
        expected=(
            ("app.schemas.file", "", "改从 app.schemas 聚合入口取"),
            ("app.schemas.types", "MediaType", "改从 app.schemas 聚合入口取"),
        ),
    ),
    "跨插件导入": ViolationSample(
        package="borrower",
        source='''
from app.plugins.otherplugin import OtherHelper
from app.plugins.otherplugin.client import OtherClient


def borrow() -> OtherHelper:
    return OtherHelper(OtherClient())
''',
        expected=(
            ("app.plugins.otherplugin", "OtherHelper", "跨插件导入不在公开面内"),
            ("app.plugins.otherplugin.client", "OtherClient", "跨插件导入不在公开面内"),
        ),
    ),
    "SDK 内部模块": ViolationSample(
        package="sdkdiver",
        source='''
import app.sdk._legacy.storage
from app.sdk._exports import SDK_DECLARED_EXPORTS


def declared() -> dict:
    return SDK_DECLARED_EXPORTS
''',
        expected=(
            ("app.sdk._exports", "SDK_DECLARED_EXPORTS", "SDK 未提供该出口"),
            ("app.sdk._legacy.storage", "", "SDK 未提供该出口"),
        ),
    ),
    "动态导入": ViolationSample(
        package="latebinder",
        source='''
from importlib import import_module


def late_bind():
    events = import_module("app.runtime.events")
    models = __import__("app.db.models")
    return events, models
''',
        expected=(
            ("app.db.models", "", "SDK 未提供该出口"),
            ("app.runtime.events", "", "改从 app.sdk.events 取用"),
        ),
    ),
}


def imported_paths(tree: ast.Module) -> list[tuple[int, str, str]]:
    """提取一个插件源码文件里的绝对 app 导入。

    相对导入指向插件自己的包，按语法即可确认，不必解析。取到符号名的记符号名：越界
    提示要指得出「应当改用 SDK 的什么」，只有符号粒度答得上来。

    :param tree: 源码语法树
    :return: ``(行号, 模块路径, 符号名)`` 列表，整模块导入的符号名为空串
    """
    collected: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            collected.extend(
                (node.lineno, alias.name, "")
                for alias in node.names
                if alias.name == "app" or alias.name.startswith("app.")
            )
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if not (node.module == "app" or node.module.startswith("app.")):
                continue
            collected.extend(
                (node.lineno, node.module, "" if alias.name == "*" else alias.name)
                for alias in node.names
            )
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in DYNAMIC_IMPORT_CALLS or not node.args:
                continue
            argument = node.args[0]
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                continue
            if argument.value == "app" or argument.value.startswith("app."):
                collected.append((node.lineno, argument.value, ""))
    return collected


def is_public_surface(module_name: str, package: str) -> bool:
    """判断一个被导入的模块是否落在插件可依赖的公开面内。

    :param module_name: 被导入的模块路径
    :param package: 发起导入的插件包名
    :return: 落在公开面或本插件包内时为 True
    """
    if module_name == SDK_ROOT or module_name.startswith(f"{SDK_ROOT}."):
        return not any(
            segment.startswith("_")
            for segment in module_name[len(SDK_ROOT) + 1:].split(".")
            if segment
        )
    if module_name == SCHEMA_FACADE:
        return True
    return module_name == f"{PLUGIN_PACKAGE}.{package}" or module_name.startswith(
        f"{PLUGIN_PACKAGE}.{package}."
    )


def remedy(module_name: str, symbol_name: str) -> str:
    """给出这条越界导入应当改用的取用位置。

    :param module_name: 越界导入的模块路径
    :param symbol_name: 被导入的符号名，整模块导入时为空串
    :return: 面向作者的改法说明
    """
    replacement = SYMBOL_INDEX.get((module_name, symbol_name))
    if replacement:
        return f"改用 {replacement}"
    if module_name.startswith(f"{SCHEMA_FACADE}."):
        return (
            f"schema 子模块的划分是宿主的内部组织，"
            f"改从 {SCHEMA_FACADE} 聚合入口取 {symbol_name or module_name}"
        )
    if module_name.startswith(f"{PLUGIN_PACKAGE}."):
        return "跨插件导入不在公开面内：另一个插件装没装、是什么版本都不是本插件能假定的"
    facades = MODULE_INDEX.get(module_name)
    if facades:
        return f"改从 {'、'.join(sorted(facades))} 取用"
    return "SDK 未提供该出口；先在 app/sdk 下补一个门面并刷新导出快照，不要放宽本边界"


def source_imports(filename: str, source: str) -> list[tuple[int, str, str]]:
    """解析一份插件源码，取出其中的绝对 app 导入。

    :param filename: 源码文件名，供语法错误定位
    :param source: 插件源码
    :return: ``(行号, 模块路径, 符号名)`` 列表
    """
    return imported_paths(ast.parse(source, filename=filename))


def source_violations(
    package: str, filename: str, source: str
) -> list[tuple[str, str, str]]:
    """扫描一份插件源码，逐条给出越界导入及其改法。

    :param package: 发起导入的插件包名
    :param filename: 源码文件名，供语法错误定位
    :param source: 插件源码
    :return: 按模块路径与符号名排序的 ``(模块路径, 符号名, 改法)`` 列表
    """
    return sorted(
        (module_name, symbol_name, remedy(module_name, symbol_name))
        for _lineno, module_name, symbol_name in source_imports(filename, source)
        if not is_public_surface(module_name, package)
    )


def tracked_paths(pathspec: str) -> list[str] | None:
    """按 git 索引列出一个路径下被跟踪的文件。

    :param pathspec: git 路径限定符
    :return: 相对仓库根的路径列表；git 不可用或不在仓内时为 None
    """
    try:
        completed = subprocess.run(
            ("git", "ls-files", "-z", "--", pathspec),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    return [entry for entry in completed.stdout.split("\0") if entry]


def test_a_plugin_that_only_uses_the_public_surface_has_no_violations():
    """只用 SDK、schema 聚合入口与本插件包写成的插件，逐个文件都判不出违规。"""
    violations = {
        filename: source_violations(COMPLIANT_PACKAGE, filename, source)
        for filename, source in COMPLIANT_SOURCES.items()
    }

    assert all(not found for found in violations.values()), (
        "合规样本被判出违规，判据把公开面之内的写法也拦了：\n"
        + "\n".join(
            f"{filename}: {found}" for filename, found in violations.items() if found
        )
    )


def test_the_compliant_sample_exercises_every_allowed_shape():
    """合规样本必须把三种允许形态各写到一次。

    只写了本插件包内导入的样本同样零违规，却什么都没验证——上一条的零违规要说明
    「公开面之内的写法不会被误伤」，前提是这三种形态都在样本里出现过。
    """
    modules = {
        module_name
        for filename, source in COMPLIANT_SOURCES.items()
        for _lineno, module_name, _symbol in source_imports(filename, source)
    }

    assert any(
        module == SDK_ROOT or module.startswith(f"{SDK_ROOT}.") for module in modules
    ), f"合规样本没有一条 SDK 门面导入，当前只有 {sorted(modules)}"
    assert SCHEMA_FACADE in modules, (
        f"合规样本没有用到 schema 聚合入口，当前只有 {sorted(modules)}"
    )
    assert any(
        module.startswith(f"{PLUGIN_PACKAGE}.{COMPLIANT_PACKAGE}") for module in modules
    ), f"合规样本没有一条本插件包内的绝对导入，当前只有 {sorted(modules)}"


@pytest.mark.parametrize("category", sorted(VIOLATION_SAMPLES))
def test_each_violation_class_is_caught_with_the_right_remedy(category: str):
    """每一类越界导入都必须被抓到，且改法说明指向这一类应当的出口。

    抓到而给错改法与没抓到同样是失守：门禁的产出是给插件作者看的改法，指错了地方
    作者只会去放宽边界。
    """
    sample = VIOLATION_SAMPLES[category]

    found = source_violations(sample.package, f"{sample.package}.py", sample.source)

    assert [(module, symbol) for module, symbol, _text in found] == [
        (module, symbol) for module, symbol, _fragment in sample.expected
    ], f"「{category}」样本判出的越界条目与预期不符：{found}"
    for (module, symbol, text), (_module, _symbol, fragment) in zip(
        found, sample.expected
    ):
        assert fragment in text, (
            f"「{category}」样本里 {module}.{symbol} 的改法是 {text!r}，"
            f"未指向应有的出口 {fragment!r}"
        )


def test_the_sample_set_is_not_vacuous():
    """样本集非空，且每份违规样本都至少期待一条违规。

    样本被清空或期望被改成空列表时，上面几条会一条不落地通过——一条不判任何东西的
    规则和没有规则是一回事。
    """
    assert COMPLIANT_SOURCES, "合规样本为空，零违规不说明任何事"
    assert VIOLATION_SAMPLES, "违规样本为空，门禁没有被任何输入检验过"
    empty = sorted(
        category
        for category, sample in VIOLATION_SAMPLES.items()
        if not sample.expected
    )
    assert empty == [], f"以下违规样本没有期待任何一条违规：{empty}"


def test_the_mount_point_carries_no_tracked_extension():
    """``app/plugins/`` 挂载点下不得有任何随仓入库的文件。

    挂载点是运行期的安装目录，容器可以把宿主目录整个卷挂上来盖掉它。随仓入库的扩展
    在部署形态下当场消失，测试却仍在开发机上绿着；本条把这种落差挡在入库那一刻。
    """
    tracked = tracked_paths(PLUGIN_MOUNT_POINT)
    gate = tracked_paths(GATE_ENTRY)
    if tracked is None or gate != [GATE_ENTRY]:
        pytest.skip("git 索引不可用，判不了挂载点下有没有随仓入库的文件")

    assert tracked == [], (
        f"{PLUGIN_MOUNT_POINT}/ 下有 {len(tracked)} 个文件随仓入库：{tracked}\n"
        "挂载点是纯数据目录，扩展应发布到插件仓由用户安装，不放进本仓。"
    )
