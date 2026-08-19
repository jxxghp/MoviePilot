"""插件 requirements 聚合和 Python 依赖安装适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Any, Optional

from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, Specifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from app.runtime.config import settings
from app.runtime.log import logger

# 精确锁定版本的比较运算符，同一包出现两个不同锁定值即为不相容
_PINNING_OPERATORS = frozenset({"==", "==="})


@dataclass(frozen=True, slots=True)
class PluginVersionDependencyConflict:
    """同一依赖包在插件两个版本间的不相容约束。"""

    package: str  # 标准化后的包名
    existing_specifier: str  # 已装版本声明的版本约束
    new_specifier: str  # 待装版本声明的版本约束


class PluginDependencyInstaller:
    """独立负责插件依赖扫描、约束合并和 pip 安装。"""

    def __init__(
        self,
        helper: Any = None,
        *,
        installed_plugins_provider: Optional[Callable[[], list[str]]] = None,
        plugin_dir: Optional[Path] = None,
    ) -> None:
        """保存 pip 端口和启动层提供的已安装插件读取器。"""
        if helper is None:
            from app.adapters.external.market import PluginHelper

            helper = PluginHelper()
        self._helper = helper
        self._installed_plugins_provider = installed_plugins_provider or (lambda: [])
        self._plugin_dir = plugin_dir or (
            Path(settings.ROOT_PATH) / "app" / "plugins"
        )

    @staticmethod
    def _standardize(name: str) -> str:
        """按 PEP 503 兼容规则标准化依赖包名。"""
        return (name or "").lower().replace("-", "_").replace(".", "_")

    @classmethod
    def _installed_packages(cls) -> dict[str, Version]:
        """读取当前 Python 环境中可解析版本的已安装包。"""
        installed: dict[str, Version] = {}
        try:
            for distribution in distributions():
                name = distribution.metadata.get("Name")
                version = distribution.metadata.get("Version") or getattr(
                    distribution,
                    "version",
                    None,
                )
                if not name or not version:
                    continue
                package_name = cls._standardize(name)
                try:
                    parsed = Version(version)
                except InvalidVersion:
                    logger.debug(
                        f"无法解析已安装包 '{package_name}' 的版本：{version}"
                    )
                    continue
                if package_name not in installed or parsed > installed[package_name]:
                    installed[package_name] = parsed
        except Exception as err:
            logger.error(f"获取已安装的包时发生错误：{err}")
        return installed

    @classmethod
    def _parse_requirements(cls, requirements_file: Path) -> dict[str, list[str]]:
        """解析一个 requirements 文件中的包名和版本约束。"""
        dependencies: dict[str, list[str]] = {}
        try:
            for line in requirements_file.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    requirement = Requirement(line)
                except Exception as err:
                    logger.debug(f"无法解析依赖项 '{line}'：{err}")
                    continue
                package_name = cls._standardize(requirement.name)
                dependencies.setdefault(package_name, []).append(
                    str(requirement.specifier)
                )
        except Exception as err:
            logger.error(f"解析 requirements.txt 时发生错误：{err}")
        return dependencies

    @classmethod
    def _merge(cls, dependencies: dict[str, set[str]]) -> dict[str, str]:
        """求同一包多来源约束的交集，保留冲突约束供 pip 处理。"""
        merged: dict[str, str] = {}
        for package_name, specifiers in dependencies.items():
            spec_set = SpecifierSet()
            for specifier in specifiers:
                if not specifier:
                    continue
                try:
                    spec_set &= SpecifierSet(specifier)
                except InvalidSpecifier as err:
                    logger.error(f"发生版本约束冲突：{err}")
            merged[package_name] = str(spec_set) if spec_set else ""
        return merged

    @staticmethod
    def _source_dirs(plugin_dir: Path) -> list[Path]:
        """列出一个插件目录下承载源码的目录。

        插件源码按版本分目录后，requirements.txt 与 wheels 随源码下沉一层；
        存量平铺布局尚未迁移时它们仍在插件目录本身，两处都纳入扫描。

        :param plugin_dir: 插件目录
        :return: 插件目录及其一级子目录
        """
        result = [plugin_dir]
        try:
            result.extend(entry for entry in sorted(plugin_dir.iterdir()) if entry.is_dir())
        except (FileNotFoundError, OSError):
            pass
        return result

    def _plugin_dependencies(self) -> dict[str, str]:
        """扫描已安装插件的 requirements 并合并版本约束。"""
        dependencies: dict[str, set[str]] = {}
        installed_plugins = {
            plugin_id.lower()
            for plugin_id in self._installed_plugins_provider() or []
        }
        try:
            plugin_dirs = list(self._plugin_dir.iterdir())
        except (FileNotFoundError, OSError):
            return {}
        for plugin_dir in plugin_dirs:
            if not plugin_dir.is_dir():
                continue
            if plugin_dir.name not in installed_plugins:
                logger.debug(f"忽略插件 {plugin_dir.name} 的依赖")
                continue
            for source_dir in self._source_dirs(plugin_dir):
                requirements_file = source_dir / "requirements.txt"
                if not requirements_file.is_file():
                    continue
                for package_name, specifiers in self._parse_requirements(
                    requirements_file
                ).items():
                    dependencies.setdefault(package_name, set()).update(specifiers)
        return self._merge(dependencies)

    def find_missing(self) -> list[str]:
        """返回当前插件集合缺失或不满足约束的依赖项。"""
        try:
            required = self._plugin_dependencies()
            installed = self._installed_packages()
            missing = []
            for package_name, specifier in required.items():
                installed_version = installed.get(package_name)
                try:
                    satisfied = installed_version is not None and SpecifierSet(
                        specifier
                    ).contains(installed_version, prereleases=True)
                except InvalidSpecifier as err:
                    logger.error(f"依赖 {package_name} 约束无效：{err}")
                    satisfied = False
                if not satisfied:
                    missing.append(f"{package_name}{specifier}")
            return missing
        except Exception as err:
            logger.error(f"收集所有需要安装或更新的依赖项时发生错误：{err}")
            return []

    def _wheels_dirs(self) -> list[Path]:
        """收集已安装插件附带的本地 wheels 目录。"""
        result = []
        installed_plugins = {
            plugin_id.lower()
            for plugin_id in self._installed_plugins_provider() or []
        }
        for plugin_id in installed_plugins:
            for source_dir in self._source_dirs(self._plugin_dir / plugin_id):
                wheels_dir = source_dir / "wheels"
                if wheels_dir.is_dir():
                    result.append(wheels_dir)
        return list(dict.fromkeys(result))

    def install(self, dependencies: list[str]) -> tuple[bool, str]:
        """把依赖写入临时 requirements 并调用现有 pip 健康检查策略。"""
        if not dependencies:
            return False, "没有传入需要安装的依赖项"
        requirements_file = (
            Path(settings.TEMP_PATH)
            / "plugin_dependencies"
            / "requirements.txt"
        )
        try:
            requirements_file.parent.mkdir(parents=True, exist_ok=True)
            requirements_file.write_text(
                "".join(f"{dependency}\n" for dependency in dependencies),
                encoding="utf-8",
            )
            return self._helper.pip_install_with_fallback(
                requirements_file,
                self._wheels_dirs(),
            )
        except Exception as err:
            logger.error(f"安装依赖项时发生错误：{err}")
            return False, f"安装依赖项时发生错误：{err}"
        finally:
            requirements_file.unlink(missing_ok=True)

    async def async_find_missing(self) -> list[str]:
        """在线程池中扫描缺失依赖，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.find_missing)

    async def async_install(self, dependencies: list[str]) -> tuple[bool, str]:
        """在线程池中安装依赖，复用同步 pip 健康检查策略。"""
        return await asyncio.to_thread(self.install, dependencies)


def _bound_version(specifier: Specifier) -> Optional[Version]:
    """解析比较运算符右侧的版本号。

    :param specifier: 单条版本约束
    :return: 版本号；含通配符或无法解析时为 None
    """
    if "*" in specifier.version:
        return None
    try:
        return Version(specifier.version)
    except InvalidVersion:
        return None


def _tighter_bound(
    current: Optional[tuple[Version, bool]],
    candidate: tuple[Version, bool],
    *,
    upper: bool,
) -> tuple[Version, bool]:
    """在同向的两个边界中取更紧的一个。

    :param current: 当前边界 `(版本, 是否含端点)`，尚无边界时为 None
    :param candidate: 候选边界 `(版本, 是否含端点)`
    :param upper: 是否为上界；上界取更小值，下界取更大值，端点相同时开区间更紧
    :return: 更紧的边界
    """
    if current is None:
        return candidate
    if candidate[0] == current[0]:
        return current if candidate[1] else candidate
    tighter = candidate[0] < current[0] if upper else candidate[0] > current[0]
    return candidate if tighter else current


def is_unsatisfiable(specifiers: Iterable[str]) -> bool:
    """判断同一个包的一组版本约束交集是否为空。

    存在锁定版本时，只要没有任何一个锁定值满足全部约束即为空集；没有锁定版本
    时按上下界比较，下界高于上界、或上下界重合但任一侧为开区间即为空集。无法
    解析的约束一律按可满足处理，交由 pip 自行裁决，避免误判造成错误拒绝。

    :param specifiers: 同一个包的多条版本约束文本
    :return: 交集为空时为 True
    """
    spec_set = SpecifierSet()
    for text in specifiers:
        if not text:
            continue
        try:
            spec_set &= SpecifierSet(text)
        except InvalidSpecifier as err:
            logger.debug(f"版本约束无法解析，按可满足处理：{text} - {err}")
            return False
    pins: list[Version] = []
    for specifier in spec_set:
        if specifier.operator not in _PINNING_OPERATORS:
            continue
        version = _bound_version(specifier)
        if version is not None:
            pins.append(version)
    if pins:
        return not any(spec_set.contains(pin, prereleases=True) for pin in pins)
    lower: Optional[tuple[Version, bool]] = None
    upper: Optional[tuple[Version, bool]] = None
    for specifier in spec_set:
        version = _bound_version(specifier)
        if version is None:
            continue
        if specifier.operator in (">=", "~="):
            lower = _tighter_bound(lower, (version, True), upper=False)
        elif specifier.operator == ">":
            lower = _tighter_bound(lower, (version, False), upper=False)
        elif specifier.operator == "<=":
            upper = _tighter_bound(upper, (version, True), upper=True)
        elif specifier.operator == "<":
            upper = _tighter_bound(upper, (version, False), upper=True)
    if lower is None or upper is None:
        return False
    if lower[0] > upper[0]:
        return True
    return lower[0] == upper[0] and not (lower[1] and upper[1])


def read_requirement_specifiers(source_dir: Path) -> dict[str, list[str]]:
    """读取一个插件源码目录声明的依赖包及其版本约束。

    :param source_dir: 插件源码目录
    :return: 标准化包名到版本约束文本列表的映射，无 requirements.txt 时为空字典
    """
    requirements_file = Path(source_dir) / "requirements.txt"
    if not requirements_file.is_file():
        return {}
    return PluginDependencyInstaller._parse_requirements(requirements_file)


def find_version_dependency_conflicts(
    existing_requirements: dict[str, list[str]],
    new_requirements: dict[str, list[str]],
) -> list[PluginVersionDependencyConflict]:
    """对两个插件版本共同依赖的包求约束交集，返回交集为空的那些包。

    :param existing_requirements: 已装版本的依赖约束
    :param new_requirements: 待装版本的依赖约束
    :return: 不相容的依赖包列表，为空表示两版本可以并存
    """
    conflicts: list[PluginVersionDependencyConflict] = []
    for package_name in sorted(set(existing_requirements) & set(new_requirements)):
        existing_specifiers = existing_requirements[package_name]
        new_specifiers = new_requirements[package_name]
        if not is_unsatisfiable([*existing_specifiers, *new_specifiers]):
            continue
        conflicts.append(
            PluginVersionDependencyConflict(
                package=package_name,
                existing_specifier=_render_specifiers(existing_specifiers),
                new_specifier=_render_specifiers(new_specifiers),
            )
        )
    return conflicts


def _render_specifiers(specifiers: Iterable[str]) -> str:
    """把一个包的多条版本约束拼成可读文本。

    :param specifiers: 版本约束文本列表
    :return: 逗号分隔的约束文本，全部为空时返回「任意版本」
    """
    rendered = ", ".join(text for text in specifiers if text)
    return rendered or "任意版本"


def describe_version_dependency_conflicts(
    existing_version: str,
    new_version: str,
    conflicts: list[PluginVersionDependencyConflict],
) -> str:
    """拼装两个插件版本依赖冲突的拒绝说明。

    :param existing_version: 已装版本号
    :param new_version: 待装版本号
    :param conflicts: 不相容的依赖包列表
    :return: 拒绝说明文案，指明冲突的包与双方约束
    """
    details = "；".join(
        f"{conflict.package}（v{existing_version} 要求 {conflict.existing_specifier}，"
        f"v{new_version} 要求 {conflict.new_specifier}）"
        for conflict in conflicts
    )
    return (
        f"该插件的 v{existing_version} 与 v{new_version} 依赖冲突，无法并存，请选择其一。"
        f"冲突依赖：{details}"
    )
