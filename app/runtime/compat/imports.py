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
    SYMBOL_ALIASES,
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


class LegacySymbolOverlayLoader(importlib.abc.Loader):
    """在标准物理模块执行后叠加旧符号的惰性解析，不修改 canonical 源码。

    ``original_loader`` 为 ``None`` 表示目标是命名空间包（PathFinder 对这类包只给出
    搜索路径、不给 Loader），此时没有源码要执行，只叠加符号解析。
    """

    _STATE_KEY = "__legacy_symbol_overlay_state__"

    def __init__(
            self,
            module_name: str,
            original_loader: Optional[importlib.abc.Loader],
    ):
        """保存物理模块名称和 PathFinder 已选择的原始 Loader。"""
        self.module_name = module_name
        self.original_loader = original_loader

    def __getattr__(self, name: str):
        """把资源读取等非核心 Loader 能力转交给原始 Loader。"""
        if self.original_loader is None:
            raise AttributeError(name)
        return getattr(self.original_loader, name)

    def create_module(self, spec):
        """沿用原始 Loader 的模块创建逻辑。"""
        creator = getattr(self.original_loader, "create_module", None)
        return creator(spec) if creator else None

    @classmethod
    def _restore_previous_overlay(cls, module: ModuleType) -> None:
        """reload 前恢复物理模块原有的动态属性和 __all__ 状态。"""
        state = module.__dict__.pop(cls._STATE_KEY, None)
        if not state:
            return
        for name in ("__getattr__", "__dir__"):
            previous = state.get(name)
            if previous is None:
                module.__dict__.pop(name, None)
            else:
                module.__dict__[name] = previous
        if state.get("had_all"):
            module.__dict__["__all__"] = state.get("all")
        else:
            module.__dict__.pop("__all__", None)

    def exec_module(self, module: ModuleType) -> None:
        """执行真实模块后安装只对已登记旧符号生效的 __getattr__。"""
        self._restore_previous_overlay(module)
        if self.original_loader is not None:
            executor = getattr(self.original_loader, "exec_module", None)
            if not executor:
                raise ImportError(f"模块 {self.module_name} 的原始 Loader 不支持 exec_module")
            executor(module)

        exports = SYMBOL_ALIASES[self.module_name]
        previous_getattr = module.__dict__.get("__getattr__")
        previous_dir = module.__dict__.get("__dir__")
        had_all = "__all__" in module.__dict__
        previous_all = module.__dict__.get("__all__")

        def resolve_export(name: str):
            """惰性解析物理模块中已经迁走的旧符号。"""
            symbol = exports.get(name)
            if symbol:
                record_legacy_import(f"{self.module_name}.{name}")
                target = importlib.import_module(symbol.target_module)
                return getattr(target, symbol.target_name)
            if previous_getattr:
                return previous_getattr(name)
            raise AttributeError(
                f"module {self.module_name!r} has no attribute {name!r}"
            )

        def list_exports():
            """返回物理模块原有名称与兼容符号的并集。"""
            names = set(module.__dict__) | set(exports)
            if previous_dir:
                names.update(previous_dir())
            return sorted(names)

        module.__getattr__ = resolve_export
        module.__dir__ = list_exports
        # 兼容符号不并入 __all__：避免 `from <module> import *` 在包初始化期
        # 急切解析旧符号、反向拉起应用层模块形成循环导入；显式导入与属性
        # 访问仍由上方 __getattr__ 惰性解析兜底
        public_names = {
            name for name in module.__dict__ if not name.startswith("_")
        }
        declared_exports = set(previous_all or ()) if had_all else public_names
        module.__all__ = sorted(declared_exports)
        module.__dict__[self._STATE_KEY] = {
            "__getattr__": previous_getattr,
            "__dir__": previous_dir,
            "had_all": had_all,
            "all": previous_all,
        }


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

        if fullname in SYMBOL_ALIASES:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            # 命名空间包的 spec 没有 Loader，只有搜索路径；两种形态都要挂上叠加层，
            # 否则挂载点形态的旧包（app.plugins）拿不到已迁走的符号
            if spec and (spec.loader or spec.submodule_search_locations is not None):
                spec.loader = LegacySymbolOverlayLoader(fullname, spec.loader)
                return spec

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
