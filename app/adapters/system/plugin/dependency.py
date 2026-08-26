"""插件 Python 依赖聚合和安装适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution, distributions
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from app.adapters.system.plugin.manifest import (
    PluginDependencyManifestError,
    load_dependency_manifest,
)
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


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
            Path(get_runtime_setting('ROOT_PATH')) / "app" / "plugins"
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

    def _plugin_manifests(self) -> list[Any]:
        """返回已安装插件当前生效的依赖清单。"""
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
            manifest = load_dependency_manifest(plugin_dir)
            if manifest is None:
                continue
            manifests.append(manifest)
        return manifests

    def _plugin_dependencies(self) -> list[Requirement]:
        """扫描已安装插件的生效依赖清单并合并版本约束。"""
        dependencies: list[Requirement] = []
        for manifest in self._plugin_manifests():
            for requirement in manifest.dependencies:
                if requirement.marker and not requirement.marker.evaluate():
                    continue
                dependencies.append(requirement)
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
                manifest = load_dependency_manifest(plugin_dir)
                requirements = [] if manifest is None else [
                    requirement
                    for requirement in manifest.dependencies
                    if not requirement.marker or requirement.marker.evaluate()
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
            wheels_dir = self._plugin_dir / plugin_id / "wheels"
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
        """异步安装依赖，使用可取消的包安装子进程。"""
        if not dependencies:
            return False, "没有传入需要安装的依赖项"
        try:
            manifest_paths = [manifest.path for manifest in self._plugin_manifests()]
            if not manifest_paths:
                return False, "没有找到已安装插件的依赖清单"
            return await self._helper.async_install_packages_with_fallback(
                manifest_paths,
                self._wheels_dirs(),
            )
        except Exception as err:
            logger.error(f"安装依赖项时发生错误：{err}")
            return False, f"安装依赖项时发生错误：{err}"
