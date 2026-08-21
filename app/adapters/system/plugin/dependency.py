"""插件 Python 依赖聚合和安装适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution, distributions
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, Specifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from app.adapters.system.plugin.manifest import (
    PluginDependencyManifestError,
    load_dependency_manifest,
)
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


@dataclass
class _RequirementGroup:
    """聚合同一包和安装来源的 extras 与版本约束。"""

    name: str  # PEP 503 规范化后的包名
    url: Optional[str]  # direct reference 来源；为空表示从索引安装
    extras: set[str] = field(default_factory=set)  # 所有插件要求启用的 extras
    specifiers: set[str] = field(default_factory=set)  # 待求交集的版本约束


class PluginDependencyInstaller:
    """独立负责插件依赖扫描、约束合并和安装。"""

    def __init__(
        self,
        helper: Any = None,
        *,
        installed_plugins_provider: Optional[Callable[[], list[str]]] = None,
        plugin_dir: Optional[Path] = None,
    ) -> None:
        """保存包安装端口和启动层提供的已安装插件读取器。"""
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
    def _installed_distribution(cls, package_name: str) -> Any | None:
        """读取一个包的元数据，用于校验 extras 和 direct URL 来源。"""
        try:
            return distribution(package_name)
        except PackageNotFoundError:
            return None

    def _requirement_satisfied(
        self,
        requirement: Requirement,
        installed: dict[str, Version],
        *,
        seen: Optional[set[tuple[str, tuple[str, ...], Optional[str]]]] = None,
    ) -> bool:
        """同时校验版本、extras 及 direct URL，不把同名包误认为同一制品。"""
        package_name = self._standardize(requirement.name)
        installed_version = installed.get(package_name)
        try:
            if installed_version is None or not SpecifierSet(
                requirement.specifier
            ).contains(installed_version, prereleases=True):
                return False
        except InvalidSpecifier as err:
            logger.error(f"依赖 {package_name} 约束无效：{err}")
            return False

        installed_distribution = self._installed_distribution(package_name)
        if installed_distribution is None:
            return False if requirement.extras or requirement.url else True

        if requirement.url and not self._direct_url_matches(
            installed_distribution, requirement.url
        ):
            return False

        requested_extras = {
            self._standardize_extra(extra) for extra in requirement.extras
        }
        if requested_extras:
            provided_extras = {
                self._standardize_extra(extra)
                for extra in installed_distribution.metadata.get_all(
                    "Provides-Extra"
                )
                or []
            }
            if not requested_extras.issubset(provided_extras):
                return False

        marker_key = (package_name, tuple(sorted(requested_extras)), requirement.url)
        if seen is None:
            seen = set()
        if marker_key in seen:
            return True
        seen.add(marker_key)

        for raw_dependency in installed_distribution.metadata.get_all(
            "Requires-Dist"
        ) or []:
            try:
                extra_dependency = Requirement(raw_dependency)
            except Exception as err:
                logger.debug(
                    f"无法解析已安装包 {package_name} 的依赖项 '{raw_dependency}'：{err}"
                )
                continue
            if not self._marker_matches_for_extras(
                extra_dependency, requested_extras
            ):
                continue
            if not self._requirement_satisfied(
                extra_dependency, installed, seen=seen
            ):
                return False
        return True

    @classmethod
    def _marker_matches_for_extras(
        cls, requirement: Requirement, extras: set[str]
    ) -> bool:
        """判断已安装发行版声明的可选依赖是否属于当前请求的 extra。"""
        if requirement.marker is None:
            return True
        environment = default_environment()
        if "extra" in str(requirement.marker):
            return any(
                requirement.marker.evaluate({**environment, "extra": extra})
                for extra in extras
            )
        return requirement.marker.evaluate(environment)

    @staticmethod
    def _standardize_extra(name: str) -> str:
        """按 PEP 685 兼容规则标准化 extra 名称。"""
        return (name or "").lower().replace("-", "_").replace(".", "_")

    @staticmethod
    def _direct_url_matches(installed_distribution: Any, required_url: str) -> bool:
        """校验安装发行版记录的 PEP 610 URL 与清单来源一致。"""
        try:
            payload = installed_distribution.read_text("direct_url.json")
            if not payload:
                return False
            direct_url = json.loads(payload).get("url")
            if not isinstance(direct_url, str):
                return False
            return PluginDependencyInstaller._canonical_direct_url(
                required_url
            ) == PluginDependencyInstaller._canonical_direct_url(direct_url)
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
            return False

    @staticmethod
    def _canonical_direct_url(value: str) -> tuple[str, str, str, str, str]:
        """规范化来源 URL，同时保留 fragment 中可能存在的校验信息。"""
        parsed = urlsplit(value)
        netloc = parsed.netloc.rsplit("@", 1)[-1].lower()
        return (
            parsed.scheme.lower(),
            netloc,
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )

    @classmethod
    def _merge(cls, dependencies: list[Requirement]) -> list[Requirement]:
        """按包和安装来源合并 extras 与约束，保留完整安装目标。"""
        groups: dict[tuple[str, Optional[str]], _RequirementGroup] = {}
        for requirement in dependencies:
            package_name = cls._standardize(requirement.name)
            key = (package_name, requirement.url)
            group = groups.setdefault(
                key,
                _RequirementGroup(name=package_name, url=requirement.url),
            )
            group.extras.update(requirement.extras)
            group.specifiers.add(str(requirement.specifier))

        merged: list[Requirement] = []
        for group in groups.values():
            spec_set = SpecifierSet()
            for specifier in group.specifiers:
                if not specifier:
                    continue
                try:
                    spec_set &= SpecifierSet(specifier)
                except InvalidSpecifier as err:
                    logger.error(f"发生版本约束冲突：{err}")
            target = group.name
            if group.extras:
                target += f"[{','.join(sorted(group.extras))}]"
            if group.url:
                target += f" @ {group.url}"
            elif spec_set:
                target += str(spec_set)
            merged.append(Requirement(target))
        return merged

    @staticmethod
    def _source_dirs(plugin_dir: Path) -> list[Path]:
        """列出一个插件目录下承载源码的目录。

        插件源码按版本分目录后，依赖清单与 wheels 随源码下沉一层；
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

    @staticmethod
    def _active_requirements(source_dir: Path) -> list[Requirement]:
        """读取一个源码目录生效清单中适用于当前环境的依赖。

        :param source_dir: 插件源码目录
        :return: 环境标记成立的依赖项；没有生效清单时为空列表
        """
        manifest = load_dependency_manifest(source_dir)
        if manifest is None:
            return []
        return [
            requirement
            for requirement in manifest.dependencies
            if not requirement.marker or requirement.marker.evaluate()
        ]

    def _plugin_manifests(self) -> list[Any]:
        """返回已安装插件各源码目录当前生效的依赖清单。

        :return: 依赖清单列表，按插件目录名和源码目录顺序排列
        """
        manifests = []
        installed_plugins = {
            plugin_id.lower()
            for plugin_id in self._installed_plugins_provider() or []
        }
        try:
            plugin_dirs = list(self._plugin_dir.iterdir())
        except (FileNotFoundError, OSError):
            return []
        for plugin_dir in sorted(plugin_dirs, key=lambda item: item.name):
            if not plugin_dir.is_dir():
                continue
            if plugin_dir.name not in installed_plugins:
                logger.debug(f"忽略插件 {plugin_dir.name} 的依赖")
                continue
            for source_dir in self._source_dirs(plugin_dir):
                manifest = load_dependency_manifest(source_dir)
                if manifest is None:
                    continue
                manifests.append(manifest)
        return manifests

    def _plugin_dependencies(self) -> list[Requirement]:
        """扫描已安装插件的生效依赖清单并合并版本约束。"""
        dependencies: list[Requirement] = []
        for manifest in self._plugin_manifests():
            dependencies.extend(
                requirement
                for requirement in manifest.dependencies
                if not requirement.marker or requirement.marker.evaluate()
            )
        return self._merge(dependencies)

    def find_missing(self) -> list[str]:
        """返回当前插件集合缺失或不满足约束的依赖项。"""
        try:
            required = self._plugin_dependencies()
            installed = self._installed_packages()
            missing = []
            for requirement in required:
                if not self._requirement_satisfied(requirement, installed):
                    missing.append(str(requirement))
            return missing
        except PluginDependencyManifestError:
            raise
        except Exception as err:
            logger.error(f"收集所有需要安装或更新的依赖项时发生错误：{err}")
            return []

    def classify_plugins(self) -> tuple[list[str], list[str], list[str]]:
        """按源码和依赖状态划分已安装插件。"""
        ready: list[str] = []
        missing_dependencies: list[str] = []
        missing_source: list[str] = []
        installed_packages = self._installed_packages()

        for plugin_id in self._installed_plugins_provider() or []:
            plugin_dir = self._plugin_dir / plugin_id.lower()
            if not plugin_dir.is_dir():
                missing_source.append(plugin_id)
                continue
            try:
                requirements = [
                    requirement
                    for source_dir in self._source_dirs(plugin_dir)
                    for requirement in self._active_requirements(source_dir)
                ]
            except PluginDependencyManifestError as error:
                logger.error(f"插件 {plugin_id} 依赖清单无效：{error}")
                missing_dependencies.append(plugin_id)
                continue
            if all(
                self._requirement_satisfied(requirement, installed_packages)
                for requirement in requirements
            ):
                ready.append(plugin_id)
            else:
                missing_dependencies.append(plugin_id)

        return ready, missing_dependencies, missing_source

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
        """把已安装插件的原始清单交给一次统一包安装。"""
        if not dependencies:
            return False, "没有传入需要安装的依赖项"
        try:
            manifest_paths = [manifest.path for manifest in self._plugin_manifests()]
            if not manifest_paths:
                return False, "没有找到已安装插件的依赖清单"
            return self._helper.install_packages_with_fallback(
                manifest_paths,
                self._wheels_dirs(),
            )
        except Exception as err:
            logger.error(f"安装依赖项时发生错误：{err}")
            return False, f"安装依赖项时发生错误：{err}"

    async def async_find_missing(self) -> list[str]:
        """在线程池中扫描缺失依赖，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.find_missing)

    async def async_install(self, dependencies: list[str]) -> tuple[bool, str]:
        """在线程池中安装依赖，复用同步包安装策略。"""
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
    :return: 标准化包名到版本约束文本列表的映射，没有生效依赖清单时为空字典
    """
    try:
        manifest = load_dependency_manifest(Path(source_dir))
    except PluginDependencyManifestError as err:
        logger.error(f"插件源码目录 {source_dir} 的依赖清单无效：{err}")
        return {}
    if manifest is None:
        return {}
    specifiers: dict[str, list[str]] = {}
    for requirement in manifest.dependencies:
        package_name = PluginDependencyInstaller._standardize(requirement.name)
        specifiers.setdefault(package_name, []).append(str(requirement.specifier))
    return specifiers


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
