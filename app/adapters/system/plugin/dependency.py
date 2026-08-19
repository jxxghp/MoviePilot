"""插件 Python 依赖聚合和安装适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import distributions
from pathlib import Path
from typing import Any, Optional

from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from app.adapters.system.plugin.manifest import (
    PluginDependencyManifestError,
    load_dependency_manifest,
)
from app.runtime.config import settings
from app.runtime.log import logger


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

    def _plugin_dependencies(self) -> list[Requirement]:
        """扫描已安装插件的生效依赖清单并合并版本约束。"""
        dependencies: list[Requirement] = []
        installed_plugins = {
            plugin_id.lower()
            for plugin_id in self._installed_plugins_provider() or []
        }
        try:
            plugin_dirs = list(self._plugin_dir.iterdir())
        except (FileNotFoundError, OSError):
            return []
        for plugin_dir in plugin_dirs:
            if not plugin_dir.is_dir():
                continue
            if plugin_dir.name not in installed_plugins:
                logger.debug(f"忽略插件 {plugin_dir.name} 的依赖")
                continue
            manifest = load_dependency_manifest(plugin_dir)
            if manifest is None:
                continue
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
                package_name = self._standardize(requirement.name)
                installed_version = installed.get(package_name)
                try:
                    satisfied = installed_version is not None and SpecifierSet(
                        requirement.specifier
                    ).contains(installed_version, prereleases=True)
                except InvalidSpecifier as err:
                    logger.error(f"依赖 {package_name} 约束无效：{err}")
                    satisfied = False
                if not satisfied:
                    missing.append(str(requirement))
            return missing
        except PluginDependencyManifestError:
            raise
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
            wheels_dir = self._plugin_dir / plugin_id / "wheels"
            if wheels_dir.is_dir():
                result.append(wheels_dir)
        return list(dict.fromkeys(result))

    def install(self, dependencies: list[str]) -> tuple[bool, str]:
        """把依赖写入临时 requirements 并调用统一包安装策略。"""
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
            return self._helper.install_packages_with_fallback(
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
        """在线程池中安装依赖，复用同步包安装策略。"""
        return await asyncio.to_thread(self.install, dependencies)
