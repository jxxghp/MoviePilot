"""插件只能 import SDK 与公开面。

插件是第三方代码，不该被宿主的内部分层约束，因此它们不进依赖矩阵；但「不受内部分层
约束」不等于「什么都能 import」——恰恰相反，正因为插件不在宿主的重构射程内，它能依赖
什么才必须是一条独立的、说得出边界的规则。宿主改一次内部路径就让一批插件失效，责任
在宿主没有给出口，不在插件写错了路径。

公开面按「宿主自己不许用的那一面」定义，两条各有独立判据：

- ``app.sdk``：唯一的插件门面。它对外承诺什么由 ``app/sdk/_exports.py`` 的快照登记，
  宿主实现层反过来被禁止依赖它（见 test_architecture_dependencies）。
- ``app.schemas``：惰性兼容聚合入口。宿主被禁止使用这个聚合入口、必须走精确子模块
  （见 test_host_code_uses_precise_schema_modules），而插件反过来只许用聚合入口——
  子模块的划分是宿主的内部组织，聚合入口才是生成并版本化的公开面。

插件自己的包内导入照常，跨插件导入不在公开面内：另一个插件装没装、是什么版本，都不是
本插件能假定的。

门禁的对象是随仓入库的参考实现。``app/plugins/`` 同时是运行期的插件安装目录，装在那里的
第三方插件不是本仓的代码，且多数是靠兼容层工作的 v2 插件——拿 v3 SDK 去判它们越界，既管
不着又判错了对象，还会让这条规则随开发机上装了什么插件而红。扫描范围因此按
``REFERENCE_PLUGINS`` 显式登记：参考实现是给社区照抄的范例，「只用 SDK 也写得出来」由它们
担保。清单与 git 索引的一致性另立一条断言，入库却漏登记的插件不会静默失去保护。
"""

import ast
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from app.sdk._exports import SDK_DECLARED_EXPORTS, SDK_REQUIRED_EXPORTS

PROJECT_ROOT = Path(__file__).parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "app" / "plugins"
# 插件所在的包，插件包内导入按它加插件包名判定
PLUGIN_PACKAGE = "app.plugins"
# 随仓入库的参考实现，门禁的扫描范围；新增参考实现须同时登记在此
REFERENCE_PLUGINS = frozenset({"githubsso", "p123disk", "servicehealth"})
# 宿主提供的扩展基类所在处，不是插件，不参与判定
HOST_BASE_ENTRY = "app/plugins/__init__.py"
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


def reference_plugin_files(package: str) -> list[Path]:
    """列出一个参考实现的源码文件。

    单文件插件与包形态插件都收，前者的包名即去掉后缀的文件名。

    :param package: 参考实现的插件包名
    :return: 源码路径列表，插件不在磁盘上时为空
    """
    directory = PLUGIN_ROOT / package
    if directory.is_dir():
        return list(directory.rglob("*.py"))
    module = PLUGIN_ROOT / f"{package}.py"
    return [module] if module.is_file() else []


def reference_plugin_sources() -> list[tuple[str, Path]]:
    """列出随仓入库的参考实现的源码文件。

    :return: ``(插件名, 源码路径)`` 列表
    """
    return sorted(
        (package, path)
        for package in REFERENCE_PLUGINS
        for path in reference_plugin_files(package)
    )


def tracked_plugin_packages() -> frozenset[str] | None:
    """按 git 索引列出随仓入库的插件包名。

    索引里必须认得出宿主扩展基类那一项，认不出说明看的不是本仓的索引，判不了谁随仓入库。

    :return: 插件包名集合；git 不可用、不在仓内或索引对不上本仓时为 None
    """
    try:
        completed = subprocess.run(
            ("git", "ls-files", "-z", "--", "app/plugins"),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    entries = [entry for entry in completed.stdout.split("\0") if entry]
    if HOST_BASE_ENTRY not in entries:
        return None
    return frozenset(
        PurePosixPath(entry).parts[2].removesuffix(".py")
        for entry in entries
        if entry != HOST_BASE_ENTRY
    )


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


def test_plugins_only_import_the_sdk_and_public_surface():
    """参考实现的 app 内导入必须全部落在 SDK、schema 聚合入口或本插件包内。"""
    violations: list[str] = []
    for package, path in reference_plugin_sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT)
        violations.extend(
            f"{relative}:{lineno} 导入 "
            f"{'.'.join(part for part in (module_name, symbol_name) if part)}；"
            f"{remedy(module_name, symbol_name)}"
            for lineno, module_name, symbol_name in imported_paths(tree)
            if not is_public_surface(module_name, package)
        )

    assert not violations, "\n".join(
        [
            "[插件越界] 以下插件导入了 SDK 与公开面之外的宿主路径：",
            *violations,
            "插件可依赖的只有 app.sdk 与 app.schemas 聚合入口，以及本插件包自身。",
        ]
    )


def test_the_boundary_gate_covers_the_reference_implementations():
    """三个原生参考实现必须在本门禁的扫描范围内。

    参考实现被搬走、改名，或登记清单被清空时上一条会无声通过——一条不扫任何文件的规则
    和没有规则是一回事。参考实现是「只用 SDK 也写得出来」的验收标准，它们在场即门禁在跑。

    三个各压一族扩展点：``githubsso`` 声明登录认证族的服务实例类型，``p123disk`` 声明
    存储族的服务实例类型，``servicehealth`` 声明智能体工具。三者要用的 SDK 出口互不重叠
    ——登录票据与服务实例发现、存储基类、智能体工具基类与标签——一族缺席即那几个出口
    「只用 SDK 也写得出来」无人担保。
    """
    scanned = {package for package, _path in reference_plugin_sources()}

    assert {"githubsso", "p123disk", "servicehealth"} <= scanned, (
        f"参考实现不在扫描范围内，当前只扫到 {sorted(scanned)}"
    )


def test_every_in_repo_plugin_is_registered_as_a_reference_implementation():
    """随仓入库的插件必须逐个登记进扫描清单。

    显式清单换来了「不受运行期装了什么插件影响」，代价是新增参考实现要多改一处；漏改
    的后果不是报错而是这个插件不受门禁保护，本条把这种漏改变成红。
    """
    tracked = tracked_plugin_packages()
    if tracked is None:
        pytest.skip("git 索引不可用，判不了哪些插件随仓入库")

    assert tracked == REFERENCE_PLUGINS, (
        "扫描清单与随仓入库的插件对不上："
        f"入库但漏登记 {sorted(tracked - REFERENCE_PLUGINS)}，"
        f"登记了但不在库里 {sorted(REFERENCE_PLUGINS - tracked)}；"
        "漏登记的插件不受导入边界门禁保护，请把它补进 REFERENCE_PLUGINS。"
    )
