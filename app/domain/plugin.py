"""插件来源、代际与安装计划的纯领域规则。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

LOCAL_PLUGIN_SOURCE_PREFIX = "local://"
PLUGIN_SYSTEM_VERSION_FIELD = "system_version"
_PHYSICAL_PLUGIN_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
PLUGIN_GENERATION_COMPATIBILITY: dict[str, tuple[str, ...]] = {
    "v3": ("v2",),
}


@dataclass(frozen=True, slots=True)
class PluginReleaseInstallPlan:
    """描述远端插件内容准备模式，不持有同步或异步 I/O 实现。"""

    release_tag: str | None
    fallback_to_filelist: bool


def is_physical_plugin_id(plugin_id: object) -> bool:
    """判断值是否符合物理插件在市场、目录和来源身份中共用的 ID 规则。"""
    return bool(
        isinstance(plugin_id, str)
        and plugin_id == plugin_id.strip()
        and _PHYSICAL_PLUGIN_ID_PATTERN.fullmatch(plugin_id)
    )


def is_local_plugin_source(repo_url: str | None) -> bool:
    """判断插件来源是否为宿主定义的不透明本地仓库标识。"""
    return bool(repo_url and repo_url.startswith(LOCAL_PLUGIN_SOURCE_PREFIX))


def build_local_plugin_source(
    plugin_id: str,
    *,
    repo_path: str | None = None,
    package_generation: str | None = None,
) -> str:
    """生成本地插件来源标识，不解释或访问其中的宿主路径。"""
    repo_url = f"{LOCAL_PLUGIN_SOURCE_PREFIX}{quote(plugin_id, safe='')}"
    parameters: list[str] = []
    if repo_path:
        parameters.append(f"path={quote(repo_path, safe='/:~')}")
    if package_generation:
        parameters.append(f"version={quote(package_generation, safe='')}")
    return f"{repo_url}?{'&'.join(parameters)}" if parameters else repo_url


def parse_local_plugin_reference(repo_url: str) -> str | None:
    """从不透明本地来源标识中提取插件 ID。"""
    if not is_local_plugin_source(repo_url):
        return None
    try:
        parsed = urlsplit(repo_url)
        plugin_id = unquote(parsed.netloc or parsed.path.strip("/"))
    except (TypeError, ValueError):
        return None
    return plugin_id or None


def parse_local_plugin_path(repo_url: str) -> str | None:
    """从本地来源标识中提取未解释的仓库路径字符串。"""
    if not is_local_plugin_source(repo_url):
        return None
    try:
        values = parse_qs(urlsplit(repo_url).query).get("path")
    except (TypeError, ValueError):
        return None
    return values[0] if values else None


def parse_local_plugin_generation(repo_url: str) -> str | None:
    """从本地来源标识中提取 package 代际。"""
    if not is_local_plugin_source(repo_url):
        return None
    try:
        values = parse_qs(urlsplit(repo_url).query).get("version")
    except (TypeError, ValueError):
        return None
    return values[0] if values else None


def compatible_plugin_generations(current_generation: str | None) -> tuple[str, ...]:
    """返回当前宿主允许读取的插件代际，按优先级去重。"""
    if not current_generation:
        return ()
    return tuple(
        dict.fromkeys(
            (
                current_generation,
                *PLUGIN_GENERATION_COMPATIBILITY.get(current_generation, ()),
            )
        )
    )


def plugin_generation_candidates(
    requested_generation: str | None,
    *,
    current_generation: str | None,
) -> tuple[str, ...]:
    """返回安装读取唯一的代际候选顺序，并把基础索引固定在末尾。"""
    preferred_generation = requested_generation or current_generation
    candidates = [preferred_generation]
    candidates.extend(
        PLUGIN_GENERATION_COMPATIBILITY.get(preferred_generation or "", ())
    )
    candidates.append("")
    return tuple(dict.fromkeys(candidate or "" for candidate in candidates))


def is_plugin_generation_compatible(
    plugin_info: object,
    package_generation: str | None,
    *,
    current_generation: str | None,
    free_threaded: bool,
) -> bool:
    """按当前代际、兼容代际和基础索引规则判断一个市场条目。"""
    if not isinstance(plugin_info, Mapping):
        return False
    if free_threaded and plugin_info.get("v3t") is False:
        return False
    if not current_generation:
        return not package_generation
    if plugin_info.get(current_generation) is False:
        return False
    if package_generation == current_generation:
        return True
    if package_generation in PLUGIN_GENERATION_COMPATIBILITY.get(
        current_generation, ()
    ):
        return plugin_info.get(current_generation) is not False
    if package_generation:
        return False
    if plugin_info.get(current_generation) is True:
        return True
    return any(
        plugin_info.get(generation) is True
        for generation in PLUGIN_GENERATION_COMPATIBILITY.get(
            current_generation, ()
        )
    )


def check_plugin_system_version(
    plugin_info: object,
    *,
    current_version: str,
) -> tuple[bool, str]:
    """检查插件声明的 MoviePilot PEP 440 版本范围。"""
    if not isinstance(plugin_info, Mapping):
        return True, ""
    raw_specifier = plugin_info.get(PLUGIN_SYSTEM_VERSION_FIELD)
    if raw_specifier is None or raw_specifier == "":
        return True, ""
    if not isinstance(raw_specifier, str):
        return False, (
            f"插件限定的系统版本范围 {PLUGIN_SYSTEM_VERSION_FIELD} 必须是字符串，"
            f"请使用 PEP 440 版本范围格式，例如 >=2.12.0,<3"
        )
    try:
        system_version = Version(current_version)
    except InvalidVersion:
        return False, (
            f"当前 MoviePilot 版本 {current_version} 无法解析，"
            f"已拒绝安装带版本限制的插件"
        )
    try:
        specifier_set = SpecifierSet(raw_specifier)
    except InvalidSpecifier:
        return False, (
            f"插件限定的系统版本范围格式不正确：{raw_specifier}，"
            f"请使用 PEP 440 版本范围格式，例如 >=2.12.0,<3"
        )
    if specifier_set.contains(system_version, prereleases=True):
        return True, ""
    return False, (
        f"插件要求 MoviePilot 版本 {raw_specifier}，当前版本 {current_version} 不满足，"
        f"已拒绝安装"
    )


def build_plugin_release_install_plan(
    *,
    plugin_id: str,
    metadata: Mapping[str, Any],
    release_version: str | None,
    release_items: Sequence[Mapping[str, Any]],
    current_version: str,
) -> tuple[PluginReleaseInstallPlan | None, str]:
    """统一选择指定 Release、可回退当前 Release 或文件列表安装模式。"""
    is_release = metadata.get("release")
    plugin_version = metadata.get("version")
    if release_version:
        if not is_release:
            return None, f"{plugin_id} 未声明 Release 安装，无法安装指定版本"
        if not any(
            item.get("version") == release_version for item in release_items
        ):
            return None, f"{plugin_id} 未找到可安装的 Release 版本：{release_version}"
        if release_version == plugin_version:
            compatible, message = check_plugin_system_version(
                metadata,
                current_version=current_version,
            )
            if not compatible:
                return None, message
        return PluginReleaseInstallPlan(
            release_tag=f"{plugin_id}_v{release_version}",
            fallback_to_filelist=False,
        ), ""
    compatible, message = check_plugin_system_version(
        metadata,
        current_version=current_version,
    )
    if not compatible:
        return None, message
    if not is_release:
        return PluginReleaseInstallPlan(None, False), ""
    if not plugin_version:
        return None, (
            f"未在插件清单中找到 {plugin_id} 的版本号，无法进行 Release 安装"
        )
    return PluginReleaseInstallPlan(
        release_tag=f"{plugin_id}_v{plugin_version}",
        fallback_to_filelist=True,
    ), ""
