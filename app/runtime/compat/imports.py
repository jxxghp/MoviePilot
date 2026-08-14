import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import threading
from types import ModuleType
from typing import Dict, Optional

from app.runtime.compat.diagnostics import record_legacy_import
from app.runtime.compat.manifest import (
    MODULE_ALIASES,
    PACKAGE_ALIASES,
    PACKAGE_EXPORTS,
    VIRTUAL_PACKAGES,
    ModuleAlias,
)


_resolution_state = threading.local()


class LegacyAliasLoader(importlib.abc.Loader):
    """将旧模块键绑定到已按 canonical 名称加载的同一模块对象。"""

    _METADATA_NAMES = (
        "__name__",
        "__loader__",
        "__package__",
        "__spec__",
        "__path__",
        "__file__",
        "__cached__",
    )

    def __init__(self, legacy_name: str, alias: ModuleAlias):
        """保存当前旧路径规则，不在构造阶段导入目标模块。"""
        self.legacy_name = legacy_name
        self.alias = alias
        self._metadata: Dict[str, object] = {}

    def create_module(self, spec):
        """惰性导入 canonical 模块并复用其对象，禁止以旧名称二次执行源码。"""
        stack = list(getattr(_resolution_state, "stack", ()))
        if self.legacy_name in stack:
            chain = " -> ".join([*stack, self.legacy_name])
            raise ImportError(f"检测到兼容导入循环：{chain}")
        stack.append(self.legacy_name)
        _resolution_state.stack = stack
        try:
            module = importlib.import_module(self.alias.target)
        except ImportError as err:
            if hasattr(err, "add_note"):
                err.add_note(
                    f"兼容导入 {self.legacy_name} 指向 {self.alias.target} 时失败"
                )
            raise
        finally:
            stack.pop()
            _resolution_state.stack = stack
        self._metadata = {
            name: getattr(module, name)
            for name in self._METADATA_NAMES
            if hasattr(module, name)
        }
        return module

    def exec_module(self, module: ModuleType) -> None:
        """恢复 canonical 元数据并记录旧路径命中，不重复执行目标模块。"""
        target_module = sys.modules.get(self.alias.target)
        if module is not target_module:
            raise ImportError(
                f"兼容导入模块身份不一致：{self.legacy_name} -> {self.alias.target}"
            )
        for name, value in self._metadata.items():
            setattr(module, name, value)
        record_legacy_import(self.legacy_name)


class VirtualLegacyPackageLoader(importlib.abc.Loader):
    """创建无文件系统搜索路径的旧父包，只允许 manifest 白名单成员。"""

    def __init__(self, package_name: str):
        """保存待创建的旧父包名称。"""
        self.package_name = package_name

    def create_module(self, spec):
        """使用默认模块创建流程。"""
        return None

    def exec_module(self, module: ModuleType) -> None:
        """为合成包安装精确符号解析器和稳定的空搜索路径。"""
        exports = PACKAGE_EXPORTS.get(self.package_name, {})

        def resolve_export(name: str):
            """按 manifest 惰性解析旧包级公开符号。"""
            symbol = exports.get(name)
            if not symbol:
                raise AttributeError(
                    f"module {self.package_name!r} has no attribute {name!r}"
                )
            value = getattr(importlib.import_module(symbol.target_module), symbol.target_name)
            setattr(module, name, value)
            return value

        def list_exports():
            """返回合成包已声明的公开符号。"""
            return sorted(set(module.__dict__) | set(exports))

        module.__path__ = []
        module.__all__ = sorted(exports)
        module.__getattr__ = resolve_export
        module.__dir__ = list_exports
        if self.package_name in PACKAGE_ALIASES:
            record_legacy_import(self.package_name)


class BlockedLegacyModuleLoader(importlib.abc.Loader):
    """阻止合成旧包从其他 Finder 泄漏未登记的新内部模块。"""

    def __init__(self, module_name: str):
        """保存应拒绝的旧模块路径。"""
        self.module_name = module_name

    def create_module(self, spec):
        """使用默认模块创建流程，错误在执行阶段给出。"""
        return None

    def exec_module(self, module: ModuleType) -> None:
        """对未登记旧路径抛出标准 ModuleNotFoundError。"""
        raise ModuleNotFoundError(
            f"旧模块路径 {self.module_name} 未在兼容映射中登记",
            name=self.module_name,
        )


class LegacyImportFinder(importlib.abc.MetaPathFinder):
    """仅解析 manifest 声明的旧模块和合成父包。"""

    @staticmethod
    def _virtual_package_names():
        """计算显式虚拟根及其已登记模块所需的中间父包。"""
        package_names = set(VIRTUAL_PACKAGES)
        for legacy_name in [*MODULE_ALIASES, *PACKAGE_ALIASES, *PACKAGE_EXPORTS]:
            parts = legacy_name.split(".")
            for index in range(1, len(parts)):
                candidate = ".".join(parts[:index])
                if any(
                    candidate == root or candidate.startswith(f"{root}.")
                    for root in VIRTUAL_PACKAGES
                ):
                    package_names.add(candidate)
        return package_names

    def find_spec(self, fullname: str, path=None, target=None):
        """为精确旧模块返回 alias spec，并封锁虚拟包中的未知后代。"""
        alias = MODULE_ALIASES.get(fullname)
        if alias:
            loader = LegacyAliasLoader(fullname, alias)
            return importlib.util.spec_from_loader(
                fullname,
                loader,
                is_package=alias.is_package,
            )

        virtual_packages = self._virtual_package_names()
        if fullname in virtual_packages:
            loader = VirtualLegacyPackageLoader(fullname)
            spec = importlib.util.spec_from_loader(fullname, loader, is_package=True)
            if spec:
                spec.submodule_search_locations = []
            return spec

        if any(fullname.startswith(f"{root}.") for root in VIRTUAL_PACKAGES):
            return importlib.util.spec_from_loader(
                fullname,
                BlockedLegacyModuleLoader(fullname),
                is_package=False,
            )
        return None


def install_legacy_import_hook() -> LegacyImportFinder:
    """幂等安装旧导入 Finder，并确保它位于标准 PathFinder 之前。"""
    for finder in sys.meta_path:
        if isinstance(finder, LegacyImportFinder):
            return finder
    finder = LegacyImportFinder()
    index = next(
        (
            position
            for position, candidate in enumerate(sys.meta_path)
            if candidate is importlib.machinery.PathFinder
        ),
        len(sys.meta_path),
    )
    sys.meta_path.insert(index, finder)
    return finder


def uninstall_legacy_import_hook() -> None:
    """移除全部旧导入 Finder，仅供隔离测试恢复进程状态。"""
    sys.meta_path[:] = [
        finder for finder in sys.meta_path if not isinstance(finder, LegacyImportFinder)
    ]
