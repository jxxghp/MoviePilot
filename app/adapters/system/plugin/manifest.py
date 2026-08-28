"""插件 Python 依赖清单的选择和解析。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement

from app.runtime.log import logger

PYPROJECT_FILENAME = "pyproject.toml"
REQUIREMENTS_FILENAME = "requirements.txt"
DEPENDENCY_MANIFEST_PRIORITY = (
    PYPROJECT_FILENAME,
    REQUIREMENTS_FILENAME,
)
DEPENDENCY_MANIFEST_FILENAMES = frozenset(
    DEPENDENCY_MANIFEST_PRIORITY
)


class PluginDependencyManifestError(ValueError):
    """表示生效的现代依赖清单无法安全消费。"""


@dataclass(frozen=True)
class PluginDependencyManifest:
    """保存插件当前生效的依赖清单及其已解析依赖。"""

    path: Path
    dependencies: tuple[Requirement, ...]


def select_dependency_manifest(plugin_dir: Path) -> Path | None:
    """按现代清单优先级返回插件当前生效的依赖文件。"""
    pyproject_file = plugin_dir / PYPROJECT_FILENAME
    if pyproject_file.is_file():
        return pyproject_file
    requirements_file = plugin_dir / REQUIREMENTS_FILENAME
    if requirements_file.is_file():
        return requirements_file
    return None


def dependency_manifest_status(event_path: Path) -> bool | None:
    """判断文件事件是否改变生效清单，非清单文件返回 None。"""
    if event_path.name not in DEPENDENCY_MANIFEST_FILENAMES:
        return None
    active_manifest = select_dependency_manifest(event_path.parent)
    if event_path.is_file():
        return active_manifest == event_path
    if active_manifest is None:
        return True
    return DEPENDENCY_MANIFEST_PRIORITY.index(
        event_path.name
    ) < DEPENDENCY_MANIFEST_PRIORITY.index(active_manifest.name)


def load_dependency_manifest(
    plugin_dir: Path,
) -> PluginDependencyManifest | None:
    """读取插件当前生效的依赖清单，现代清单无效时拒绝回退。"""
    manifest_path = select_dependency_manifest(plugin_dir)
    if manifest_path is None:
        return None
    return load_dependency_file(manifest_path)


def load_dependency_file(path: Path) -> PluginDependencyManifest:
    """读取指定依赖文件，pyproject 严格校验，其余文件保持旧格式兼容。"""
    if path.name == PYPROJECT_FILENAME:
        dependencies = _load_pyproject_dependencies(path)
    else:
        dependencies = _load_requirements_dependencies(path)
    return PluginDependencyManifest(
        path=path,
        dependencies=dependencies,
    )


def dependency_manifest_declares_installation(
    manifest: PluginDependencyManifest,
) -> bool:
    """判断清单是否声明了可能改变共享 Python 环境的安装内容。"""
    if manifest.dependencies:
        return True
    if manifest.path.name == PYPROJECT_FILENAME:
        return False
    try:
        lines = manifest.path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        # 安装器随后会报告文件错误；观察边界按可能发生写入处理。
        return True
    return any(
        line.strip() and not line.lstrip().startswith("#")
        for line in lines
    )


def _load_pyproject_dependencies(path: Path) -> tuple[Requirement, ...]:
    """严格读取 PEP 621 ``project.dependencies``。"""
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise PluginDependencyManifestError(
            f"插件依赖清单 {path.name} 无法解析：{err}"
        ) from err

    project = document.get("project")
    if not isinstance(project, Mapping):
        raise PluginDependencyManifestError(
            f"插件依赖清单 {path.name} 缺少 [project] 表"
        )
    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PluginDependencyManifestError(
            f"插件依赖清单 {path.name} 的 project.name 必须是非空字符串"
        )
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or not all(
        isinstance(item, str) for item in dynamic
    ):
        raise PluginDependencyManifestError(
            f"插件依赖清单 {path.name} 的 project.dynamic 必须是字符串数组"
        )
    if "dependencies" in dynamic:
        raise PluginDependencyManifestError(
            f"插件依赖清单 {path.name} 不支持动态 dependencies"
        )
    version = project.get("version")
    if "version" in dynamic:
        if version is not None:
            raise PluginDependencyManifestError(
                f"插件依赖清单 {path.name} 不能同时静态和动态声明 version"
            )
    elif not isinstance(version, str) or not version.strip():
        raise PluginDependencyManifestError(
            f"插件依赖清单 {path.name} 必须声明非空 project.version，"
            "或将 version 加入 project.dynamic"
        )
    raw_dependencies = project.get("dependencies", [])
    if not isinstance(raw_dependencies, list) or not all(
        isinstance(item, str) for item in raw_dependencies
    ):
        raise PluginDependencyManifestError(
            f"插件依赖清单 {path.name} 的 project.dependencies 必须是字符串数组"
        )

    dependencies: list[Requirement] = []
    for item in raw_dependencies:
        try:
            dependencies.append(Requirement(item))
        except Exception as err:
            raise PluginDependencyManifestError(
                f"插件依赖清单 {path.name} 包含无效依赖项 {item!r}：{err}"
            ) from err
    return tuple(dependencies)


def _load_requirements_dependencies(path: Path) -> tuple[Requirement, ...]:
    """按旧行为逐行读取 requirements，忽略无法解析的兼容内容。"""
    dependencies: list[Requirement] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as err:
        logger.error(f"解析 requirements.txt 时发生错误：{err}")
        return ()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            dependencies.append(Requirement(line))
        except Exception as err:
            logger.debug(f"无法解析依赖项 '{line}'：{err}")
    return tuple(dependencies)
