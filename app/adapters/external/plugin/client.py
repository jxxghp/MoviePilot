"""插件市场查询客户端。"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, cast
from urllib.parse import urlparse, urlsplit

import httpx2
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from requests import Response

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.domain.plugin import (
    build_local_plugin_source,
    check_plugin_system_version,
    is_local_plugin_source,
    is_physical_plugin_id,
    is_plugin_generation_compatible,
    parse_local_plugin_generation,
    parse_local_plugin_path,
    parse_local_plugin_reference,
)
from app.foundation.environment import is_free_threaded_runtime
from app.foundation.url import UrlUtils
from app.foundation.version import compare_version
from app.runtime.cache import async_fresh, cached, fresh, is_fresh
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.tasks import get_task_registry
from app.runtime.version import get_app_version

PluginPayload = dict[str, Any]
PluginIndex = dict[str, PluginPayload]
PluginReleaseList = list[PluginPayload]
PluginRequestOptions = dict[str, Any]
PluginReleaseTask = asyncio.Task[Optional[PluginReleaseList]]

PLUGIN_INDEX_MAX_BYTES = 1024 * 1024
PLUGIN_INDEX_MAX_ENTRIES = 4096
PLUGIN_INDEX_MAX_HISTORY_ENTRIES = 512
PLUGIN_INDEX_MAX_NESTING = 64
PLUGIN_INDEX_READ_CHUNK_SIZE = 64 * 1024
PLUGIN_INDEX_COMPATIBILITY_FLAG_PATTERN = re.compile(r"^v\d+t?$")
PLUGIN_INDEX_TEXT_LIMITS = {
    "name": 256,
    "author": 256,
    "description": 4096,
    "icon": 2048,
    "author_url": 2048,
    "authorUrl": 2048,
    "project_url": 2048,
    "homepage": 2048,
    "repo_url": 2048,
}


class _PluginIndexTooLargeError(RuntimeError):
    """插件索引的声明长度或实际读取字节超过资源边界。"""


def build_local_repo_url(
    plugin_id: str,
    *,
    repo_path: Optional[Path] = None,
    package_version: Optional[str] = None,
) -> str:
    """生成兼容插件 API 使用的本地仓库来源标识。"""
    return build_local_plugin_source(
        plugin_id,
        repo_path=str(repo_path) if repo_path is not None else None,
        package_generation=package_version,
    )


def is_local_repo_url(repo_url: Optional[str]) -> bool:
    """判断插件来源是否为本地仓库标识。"""
    return is_local_plugin_source(repo_url)


def parse_local_repo_plugin_id(repo_url: str) -> Optional[str]:
    """从本地仓库来源标识提取插件 ID。"""
    return parse_local_plugin_reference(repo_url)


def parse_local_repo_path(
    repo_url: str,
    *,
    root_path: Optional[Path] = None,
) -> Optional[Path]:
    """解析本地来源中的仓库路径，并相对宿主根目录完成定位。"""
    try:
        raw_path = parse_local_plugin_path(repo_url)
        if not raw_path:
            return None
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            if root_path is None:
                return None
            path = root_path / path
        return path.resolve()
    except (OSError, TypeError, ValueError):
        return None


def parse_local_repo_generation(repo_url: str) -> Optional[str]:
    """解析本地来源标识中的 package 代际参数。"""
    return parse_local_plugin_generation(repo_url)


PLUGIN_SYSTEM_VERSION_FIELD = "system_version"
PLUGIN_MARKET_WIKI_START = "<!-- plugin-market-repos:start -->"
PLUGIN_MARKET_WIKI_END = "<!-- plugin-market-repos:end -->"
PLUGIN_MARKET_WIKI_URL = (
    "https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/plugin.md"
)
PLUGIN_MARKET_REPO_PATTERN = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
    re.IGNORECASE,
)
# 主程序重大版本可扫描的旧索引；V3 临时默认兼容 V2，条目可用 v3:false 排除。
VERSION_BACKWARD_COMPATIBLE_FLAGS: Dict[str, List[str]] = {
    "v3": ["v2"],
}

def normalize_plugin_market_repo_url(repo_url: str) -> Optional[str]:
    """规范化插件仓库地址，便于跨来源合并去重。"""
    repo_url = (repo_url or "").strip().rstrip("/")
    if not repo_url:
        return None
    repo_url = repo_url.removesuffix(".git")
    parsed_url = urlparse(repo_url)
    if parsed_url.scheme not in {"http", "https"}:
        return None
    if (parsed_url.hostname or "").lower() != "github.com":
        return None
    paths = [item for item in parsed_url.path.split("/") if item]
    if len(paths) < 2:
        return None
    return f"https://github.com/{paths[0]}/{paths[1]}"


def split_plugin_market_repo_urls(value: Optional[str]) -> list[str]:
    """拆分插件市场仓库配置并保持原有顺序去重。"""
    repos: list[str] = []
    seen_repos = set()
    for item in re.split(r"[\n,，]+", value or ""):
        normalized_repo = normalize_plugin_market_repo_url(item)
        if not normalized_repo or normalized_repo.lower() in seen_repos:
            continue
        repos.append(normalized_repo)
        seen_repos.add(normalized_repo.lower())
    return repos


def extract_plugin_market_repos_from_wiki(
    markdown: str, require_markers: bool = False
) -> list[str]:
    """
    从 Wiki 插件文档中提取插件仓库地址。

    :param markdown: Wiki 插件文档 Markdown 内容
    :param require_markers: 是否要求文档包含唯一且有序的清单边界标记
    :return: 规范化并按文档顺序去重的插件仓库地址
    """
    content = markdown or ""
    start_count = content.count(PLUGIN_MARKET_WIKI_START)
    end_count = content.count(PLUGIN_MARKET_WIKI_END)
    start_index = content.find(PLUGIN_MARKET_WIKI_START)
    end_index = content.find(PLUGIN_MARKET_WIKI_END)
    if start_count == 1 and end_count == 1 and start_index < end_index:
        content = content[
            start_index + len(PLUGIN_MARKET_WIKI_START):end_index
        ]
    elif require_markers:
        raise ValueError("Wiki 插件仓库清单必须包含唯一且有序的开始和结束标记")

    repos: list[str] = []
    seen_repos = set()
    for item in PLUGIN_MARKET_REPO_PATTERN.findall(content):
        normalized_repo = normalize_plugin_market_repo_url(item)
        if not normalized_repo or normalized_repo.lower() in seen_repos:
            continue
        repos.append(normalized_repo)
        seen_repos.add(normalized_repo.lower())
    return repos


def merge_plugin_market_repos(
    local_repos: list[str], wiki_repos: list[str]
) -> list[str]:
    """合并本地与 Wiki 插件仓库地址，并保持来源中的既有顺序。"""
    merged_repos: list[str] = []
    seen_repos = set()
    for repo in local_repos + wiki_repos:
        normalized_repo = normalize_plugin_market_repo_url(repo)
        if not normalized_repo or normalized_repo.lower() in seen_repos:
            continue
        merged_repos.append(normalized_repo)
        seen_repos.add(normalized_repo.lower())
    return merged_repos

class PluginMarketTransport:
    """负责插件市场、本地仓库和 GitHub 元数据读取。"""

    _base_url = "https://raw.githubusercontent.com/{user}/{repo}/main/"
    _release_task_lock = threading.Lock()
    _release_tasks: dict[
        tuple[asyncio.AbstractEventLoop, str, bool],
        PluginReleaseTask,
    ] = {}

    @staticmethod
    def is_local_repo_url(repo_url: Optional[str]) -> bool:
        """
        判断是否为本地插件来源标识
        """
        return is_local_repo_url(repo_url)

    @staticmethod
    def make_local_repo_url(pid: str, repo_path: Optional[Path] = None,
                            package_version: Optional[str] = None) -> str:
        """
        生成本地插件安装来源标识
        """
        return build_local_repo_url(
            pid,
            repo_path=repo_path,
            package_version=package_version,
        )

    @staticmethod
    def parse_local_repo_url(repo_url: str) -> Optional[str]:
        """
        从本地插件来源标识中解析插件ID
        """
        return parse_local_repo_plugin_id(repo_url)

    @staticmethod
    def parse_local_repo_path(repo_url: str) -> Optional[Path]:
        """
        从本地插件来源标识中解析仓库路径
        """
        return parse_local_repo_path(
            repo_url,
            root_path=(
                Path(root_path)
                if (root_path := get_runtime_setting('ROOT_PATH')) is not None
                else None
            ),
        )

    @staticmethod
    def parse_local_repo_package_version(repo_url: str) -> Optional[str]:
        """
        从本地插件来源标识中解析 package 版本
        """
        return parse_local_repo_generation(repo_url)

    @staticmethod
    def get_current_system_version() -> Optional[Version]:
        """
        解析当前主程序版本，供插件 package 中的系统版本范围匹配使用。
        """
        try:
            return Version(get_app_version())
        except InvalidVersion:
            logger.error(f"当前主程序版本号无法解析：{get_app_version()}")
            return None

    @classmethod
    def get_compatible_version_flags(cls) -> list[str]:
        """
        返回当前主程序版本可兼容的全部版本标识，包含自身及向后兼容的低版本，按优先级降序。
        未启用 VERSION_FLAG（v1）时返回空列表，表示仅使用 package.json 基础索引。
        """
        flags: list[str] = []
        if get_runtime_setting('VERSION_FLAG'):
            flags.append(get_runtime_setting('VERSION_FLAG'))
            flags.extend(VERSION_BACKWARD_COMPATIBLE_FLAGS.get(get_runtime_setting('VERSION_FLAG'), []))
        return flags

    @classmethod
    def is_plugin_info_compatible(
        cls, plugin_info: Optional[PluginPayload]
    ) -> bool:
        """
        判断 package.json 中的插件元数据是否兼容当前主程序版本。

        默认索引需要声明当前版本；V3 临时兼容已声明 V2 的共享实现，
        但显式 ``v3: false`` 始终优先拒绝。
        """
        if not isinstance(plugin_info, dict):
            return False
        if is_free_threaded_runtime() and plugin_info.get("v3t") is False:
            return False
        if not get_runtime_setting('VERSION_FLAG'):
            return True
        current_flag = get_runtime_setting('VERSION_FLAG')
        if plugin_info.get(current_flag) is False:
            return False
        if plugin_info.get(current_flag) is True:
            return True
        return any(
            plugin_info.get(flag) is True
            for flag in VERSION_BACKWARD_COMPATIBLE_FLAGS.get(
                current_flag, []
            )
        )

    @classmethod
    def is_package_plugin_compatible(
            cls,
            plugin_info: Optional[PluginPayload],
            package_version: Optional[str],
    ) -> bool:
        """
        判断指定索引中的插件条目能否在当前主程序版本使用。

        当前代专用索引直接兼容。V3 临时默认兼容 V2 专用索引，
        除非条目显式声明 ``v3: false``；默认索引仍需先声明 ``v2: true``。
        """
        if not isinstance(plugin_info, dict):
            return False
        if is_free_threaded_runtime() and plugin_info.get("v3t") is False:
            return False
        current_flag = get_runtime_setting('VERSION_FLAG')
        if not current_flag:
            return not package_version
        if package_version == current_flag:
            return True
        if package_version in VERSION_BACKWARD_COMPATIBLE_FLAGS.get(
                current_flag, []
        ):
            return plugin_info.get(current_flag) is not False
        if not package_version:
            return cls.is_plugin_info_compatible(plugin_info)
        return False

    @staticmethod
    def _package_version_candidates(
            package_version: Optional[str],
    ) -> tuple[str, ...]:
        """返回插件安装唯一的代际候选顺序，并去除重复的基础索引。"""
        preferred_version = package_version or get_runtime_setting('VERSION_FLAG')
        candidates = [preferred_version]
        candidates.extend(
            VERSION_BACKWARD_COMPATIBLE_FLAGS.get(preferred_version, [])
        )
        candidates.append("")
        return tuple(dict.fromkeys(candidates))

    @classmethod
    def _select_compatible_package_version(
            cls,
            pid: str,
            package_version: str,
            plugins: Optional[PluginIndex],
    ) -> Optional[str]:
        """从一个索引结果选择目标插件，并复用统一的代际兼容判定。"""
        plugin = (plugins or {}).get(pid)
        if plugin and cls.is_package_plugin_compatible(plugin, package_version):
            return package_version
        return None

    @classmethod
    def check_plugin_system_version(
        cls, plugin_info: Optional[PluginPayload]
    ) -> tuple[bool, str]:
        """
        检查插件 package 元数据中的主系统版本范围是否满足当前 MoviePilot 版本。
        """
        if not isinstance(plugin_info, dict):
            return True, ""

        raw_specifier = plugin_info.get(PLUGIN_SYSTEM_VERSION_FIELD)
        if raw_specifier is None or raw_specifier == "":
            return True, ""
        if not isinstance(raw_specifier, str):
            return False, (
                f"插件限定的系统版本范围 {PLUGIN_SYSTEM_VERSION_FIELD} 必须是字符串，"
                f"请使用 PEP 440 版本范围格式，例如 >=2.12.0,<3"
            )

        system_version = cls.get_current_system_version()
        if system_version is None:
            return False, f"当前 MoviePilot 版本 {get_app_version()} 无法解析，已拒绝安装带版本限制的插件"

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
            f"插件要求 MoviePilot 版本 {raw_specifier}，当前版本 {get_app_version()} 不满足，已拒绝安装"
        )

    @classmethod
    def annotate_plugin_system_version(
        cls, plugin_info: PluginPayload
    ) -> PluginPayload:
        """
        为插件 package 元数据补充系统版本兼容状态，便于市场展示和安装流程复用。
        """
        if not isinstance(plugin_info, dict):
            return plugin_info

        compatible, message = cls.check_plugin_system_version(plugin_info)
        plugin_info["system_version_compatible"] = compatible
        plugin_info["system_version_message"] = message
        return plugin_info

    @staticmethod
    def get_local_repo_paths() -> list[Path]:
        """
        获取本地插件仓库目录列表
        """
        if not get_runtime_setting('PLUGIN_LOCAL_REPO_PATHS'):
            return []
        paths = []
        for item in get_runtime_setting('PLUGIN_LOCAL_REPO_PATHS').split(","):
            local_repo_path = item.strip()
            if not local_repo_path:
                continue
            path = Path(local_repo_path).expanduser()
            if not path.is_absolute():
                path = get_runtime_setting('ROOT_PATH') / path
            paths.append(path.resolve())
        return paths

    @classmethod
    def __get_local_package(
        cls, repo_path: Path, package_version: Optional[str] = None
    ) -> Optional[PluginIndex]:
        """
        从本地插件仓库读取 package.json 或 package.{version}.json
        """
        package_file = repo_path / (
            f"package.{package_version}.json" if package_version else "package.json"
        )
        if not package_file.exists():
            return {}
        try:
            if package_file.stat().st_size > PLUGIN_INDEX_MAX_BYTES:
                raise _PluginIndexTooLargeError(
                    f"插件索引超过 {PLUGIN_INDEX_MAX_BYTES} 字节"
                )
            with package_file.open("rb") as file_handle:
                raw_content = file_handle.read(PLUGIN_INDEX_MAX_BYTES + 1)
            if len(raw_content) > PLUGIN_INDEX_MAX_BYTES:
                raise _PluginIndexTooLargeError(
                    f"插件索引超过 {PLUGIN_INDEX_MAX_BYTES} 字节"
                )
            content = raw_content.decode("utf-8", errors="replace")
        except OSError as error:
            raise RuntimeError(f"读取本地插件包 {package_file} 失败：{error}") from error
        payload = cls.__parse_plugin_index_response(content)
        if payload is None:
            raise RuntimeError(f"本地插件包 {package_file} 格式无效")
        return payload

    @staticmethod
    def __get_local_plugin_dir(repo_path: Path, pid: str, package_version: Optional[str]) -> Path:
        """按插件包版本计算本地插件源码目录。"""
        plugin_root = f"plugins.{package_version}" if package_version else "plugins"
        return repo_path / plugin_root / pid.lower()

    def get_local_plugin_candidates(self) -> PluginIndex:
        """
        扫描本地插件仓库，按插件ID保留版本号最高的候选
        """
        candidates: PluginIndex = {}
        for repo_order, repo_path in enumerate(self.get_local_repo_paths()):
            if not repo_path.exists() or not repo_path.is_dir():
                logger.warn(f"本地插件仓库目录不存在或不可读：{repo_path}")
                continue

            package_candidates = []
            if get_runtime_setting('VERSION_FLAG'):
                package_candidates.append((get_runtime_setting('VERSION_FLAG'), self.__get_local_package(repo_path,
                                                                                           get_runtime_setting('VERSION_FLAG'))))
                # 向后兼容：补充扫描更低版本的 package 文件，便于本地仓库复用历史版本插件。
                for backward_flag in VERSION_BACKWARD_COMPATIBLE_FLAGS.get(get_runtime_setting('VERSION_FLAG'), []):
                    package_candidates.append((backward_flag, self.__get_local_package(repo_path, backward_flag)))
            package_candidates.append(("", self.__get_local_package(repo_path)))

            for package_version, local_plugins in package_candidates:
                if local_plugins is None:
                    continue
                for pid, plugin_info in local_plugins.items():
                    if not isinstance(plugin_info, dict):
                        continue
                    if not self.is_package_plugin_compatible(
                            plugin_info, package_version
                    ):
                        continue

                    plugin_dir = self.__get_local_plugin_dir(repo_path, pid, package_version)
                    if not plugin_dir.is_dir():
                        logger.debug(f"跳过本地插件 {pid}：插件目录不存在 {plugin_dir}")
                        continue

                    candidate = plugin_info.copy()
                    candidate["id"] = pid
                    candidate["package_version"] = package_version
                    candidate["repo_order"] = repo_order
                    candidate["repo_path"] = repo_path
                    candidate["path"] = plugin_dir
                    candidate["repo_url"] = self.make_local_repo_url(
                        pid,
                        repo_path,
                        package_version or None,
                    )
                    self.annotate_plugin_system_version(candidate)
                    candidate_version = str(candidate.get("version") or "0")

                    existing = candidates.get(pid)
                    if not existing:
                        candidates[pid] = candidate
                        continue

                    existing_version = str(existing.get("version") or "0")
                    if compare_version(candidate_version, ">", existing_version):
                        candidates[pid] = candidate
                    elif (
                        candidate_version == existing_version
                        and repo_order < int(existing.get("repo_order", repo_order))
                    ):
                        logger.info(f"本地插件 {pid} 存在同版本来源，使用靠前目录：{repo_path}")
                        candidates[pid] = candidate

        return candidates

    def get_local_plugin_candidate(self, pid: str, package_version: Optional[str] = None,
                                   repo_path: Optional[Path] = None,
                                   strict_compat: bool = True,
                                   strict_system_version: bool = True) -> Optional[PluginPayload]:
        """
        获取指定插件ID的本地插件候选
        :param strict_system_version: 是否将主系统版本范围不匹配视为不可用候选
        """
        if not pid:
            return None
        if package_version is not None or repo_path is not None:
            repo_paths = [repo_path.resolve()] if repo_path else self.get_local_repo_paths()
            package_versions = [package_version] if package_version is not None else []
            if package_version is None:
                if get_runtime_setting('VERSION_FLAG'):
                    package_versions.append(get_runtime_setting('VERSION_FLAG'))
                    package_versions.extend(VERSION_BACKWARD_COMPATIBLE_FLAGS.get(get_runtime_setting('VERSION_FLAG'), []))
                package_versions.append("")
            selected_candidate = None
            for repo_order, local_repo_path in enumerate(self.get_local_repo_paths()):
                if local_repo_path not in repo_paths:
                    continue
                for current_package_version in package_versions:
                    local_plugins = self.__get_local_package(local_repo_path, current_package_version or "")
                    if not local_plugins:
                        continue
                    for candidate_pid, plugin_info in local_plugins.items():
                        if candidate_pid.lower() != pid.lower() or not isinstance(plugin_info, dict):
                            continue
                        is_compatible = self.is_package_plugin_compatible(
                            plugin_info,
                            current_package_version or "",
                        )
                        if not is_compatible and strict_compat:
                            continue
                        plugin_dir = self.__get_local_plugin_dir(local_repo_path, candidate_pid,
                                                                 current_package_version or "")
                        if not plugin_dir.is_dir():
                            continue
                        candidate = plugin_info.copy()
                        candidate["id"] = candidate_pid
                        candidate["package_version"] = current_package_version or ""
                        candidate["repo_order"] = repo_order
                        candidate["repo_path"] = local_repo_path
                        candidate["path"] = plugin_dir
                        candidate["repo_url"] = self.make_local_repo_url(
                            candidate_pid,
                            local_repo_path,
                            current_package_version or None,
                        )
                        if not is_compatible:
                            candidate["compatible"] = False
                            candidate["skip_reason"] = (
                                f"插件索引条目不兼容 {get_runtime_setting('VERSION_FLAG')}"
                            )
                        self.annotate_plugin_system_version(candidate)
                        if strict_system_version and candidate.get("system_version_compatible") is False:
                            candidate["compatible"] = False
                            candidate["skip_reason"] = candidate.get("system_version_message")
                        elif not strict_system_version and is_compatible:
                            candidate.pop("compatible", None)
                            candidate.pop("skip_reason", None)
                        if package_version is not None:
                            return candidate
                        if not selected_candidate:
                            selected_candidate = candidate
                            continue
                        selected_version = str(selected_candidate.get("version") or "0")
                        candidate_version = str(candidate.get("version") or "0")
                        if compare_version(candidate_version, ">", selected_version):
                            selected_candidate = candidate
            return selected_candidate

        candidates = self.get_local_plugin_candidates()
        for candidate_pid, candidate in candidates.items():
            if candidate_pid.lower() == pid.lower():
                if strict_system_version and candidate.get("system_version_compatible") is False:
                    candidate = candidate.copy()
                    candidate["compatible"] = False
                    candidate["skip_reason"] = candidate.get("system_version_message")
                return candidate
        return None

    @staticmethod
    def __append_cache_buster(url: str) -> str:
        """
        强制刷新插件库索引时追加时间戳，绕过 GitHub 镜像或中间代理的缓存。
        """
        if not is_fresh():
            return url

        parts = urlsplit(url)
        refresh_param = f"_refresh={time.time_ns()}"
        query = f"{parts.query}&{refresh_param}" if parts.query else refresh_param
        return parts._replace(query=query).geturl()

    @staticmethod
    def __safe_plugin_id(plugin_id: object) -> str:
        """生成适合日志展示的有界插件 ID，不回显完整外部载荷。"""
        if not isinstance(plugin_id, str):
            return f"<{type(plugin_id).__name__}>"
        return json.dumps(plugin_id[:64], ensure_ascii=True)

    @staticmethod
    def __plugin_index_nesting_is_valid(payload: object) -> bool:
        """限制外部 JSON 的容器深度，避免后续序列化递归耗尽调用栈。"""
        pending = [(payload, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > PLUGIN_INDEX_MAX_NESTING:
                return False
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        return True

    @classmethod
    def __normalize_plugin_index_entry(
        cls,
        plugin_id: object,
        plugin_info: object,
    ) -> tuple[Optional[PluginPayload], Optional[str], list[str]]:
        """校验影响安装决策的字段，并宽容丢弃异常展示字段。"""
        if not isinstance(plugin_id, str) or not is_physical_plugin_id(plugin_id):
            return None, "插件 ID 非法", []
        if not isinstance(plugin_info, dict):
            return None, "条目不是对象", []

        declared_id = plugin_info.get("id")
        if "id" in plugin_info and (
            not isinstance(declared_id, str)
            or not is_physical_plugin_id(declared_id)
            or declared_id.lower() != plugin_id.lower()
        ):
            return None, "id 与索引键不一致", []

        version = plugin_info.get("version")
        if not isinstance(version, str) or not version or len(version) > 64:
            return None, "version 非法", []

        system_version = plugin_info.get("system_version")
        if "system_version" in plugin_info and (
            not isinstance(system_version, str) or len(system_version) > 256
        ):
            return None, "system_version 非法", []

        level = plugin_info.get("level")
        if "level" in plugin_info and (
            isinstance(level, bool)
            or not isinstance(level, int)
            or not 0 <= level <= 99
        ):
            return None, "level 非法", []

        public_key = plugin_info.get("key")
        if "key" in plugin_info and (
            not isinstance(public_key, str) or len(public_key) > 16 * 1024
        ):
            return None, "key 非法", []

        for field, value in plugin_info.items():
            if field == "release" and not isinstance(value, bool):
                return None, "release 非法", []
            if (
                PLUGIN_INDEX_COMPATIBILITY_FLAG_PATTERN.fullmatch(field)
                and not isinstance(value, bool)
            ):
                return None, "兼容标志非法", []

        normalized = plugin_info.copy()
        if declared_id is not None:
            normalized["id"] = plugin_id
        dropped_fields: list[str] = []
        for field, max_length in PLUGIN_INDEX_TEXT_LIMITS.items():
            if field not in normalized:
                continue
            value = normalized[field]
            if not isinstance(value, str) or len(value) > max_length:
                normalized.pop(field)
                dropped_fields.append(field)

        labels = normalized.get("labels")
        if labels is not None:
            valid_labels = (
                isinstance(labels, str)
                and len(labels) <= 1024
            ) or (
                isinstance(labels, list)
                and len(labels) <= 64
                and all(
                    isinstance(item, str) and len(item) <= 128
                    for item in labels
                )
            )
            if not valid_labels:
                normalized.pop("labels")
                dropped_fields.append("labels")

        history = normalized.get("history")
        if history is not None and (
            not isinstance(history, dict)
            or len(history) > PLUGIN_INDEX_MAX_HISTORY_ENTRIES
        ):
            normalized.pop("history")
            dropped_fields.append("history")

        return normalized, None, dropped_fields

    @classmethod
    def __parse_plugin_index_response(cls, content: str) -> Optional[PluginIndex]:
        """解析并规范化插件索引，仅缓存满足索引级边界的结果。"""
        try:
            payload = json.loads(content)
        except (ValueError, RecursionError):
            logger.warning("插件包数据解析失败：响应不是有效 JSON")
            return None

        if not isinstance(payload, dict):
            logger.warning(
                f"插件包数据格式不正确，期望 dict，实际为 {type(payload).__name__}"
            )
            return None
        if not cls.__plugin_index_nesting_is_valid(payload):
            logger.warning(
                f"插件包 JSON 嵌套超过上限：{PLUGIN_INDEX_MAX_NESTING} 层"
            )
            return None
        if len(payload) > PLUGIN_INDEX_MAX_ENTRIES:
            logger.warning(
                f"插件包条目超过上限：{len(payload)} > {PLUGIN_INDEX_MAX_ENTRIES}"
            )
            return None

        normalized: PluginIndex = {}
        skipped_reasons: Counter[str] = Counter()
        dropped_fields: Counter[str] = Counter()
        skipped_examples: list[str] = []
        for plugin_id, plugin_info in payload.items():
            item, reason, dropped = cls.__normalize_plugin_index_entry(
                plugin_id,
                plugin_info,
            )
            if item is None:
                skipped_reasons[reason or "未知原因"] += 1
                if len(skipped_examples) < 5:
                    skipped_examples.append(cls.__safe_plugin_id(plugin_id))
                continue
            normalized[plugin_id] = item
            dropped_fields.update(dropped)

        summaries = []
        if skipped_reasons:
            reasons = "、".join(
                f"{reason} {count} 条"
                for reason, count in sorted(skipped_reasons.items())
            )
            examples = "、".join(skipped_examples)
            summaries.append(
                f"跳过 {sum(skipped_reasons.values())} 个条目（{reasons}；示例 {examples}）"
            )
        if dropped_fields:
            fields = "、".join(
                f"{field} {count} 次"
                for field, count in sorted(dropped_fields.items())
            )
            summaries.append(f"丢弃异常展示字段（{fields}）")
        if summaries:
            logger.warning(f"插件包数据已隔离异常内容：{'；'.join(summaries)}")

        return normalized

    @staticmethod
    def __declared_plugin_index_too_large(headers: Any) -> bool:
        """仅把合法的 Content-Length 用作流式读取前的快速拒绝依据。"""
        try:
            value = headers.get("Content-Length")
            return value is not None and int(value) > PLUGIN_INDEX_MAX_BYTES
        except (AttributeError, TypeError, ValueError):
            return False

    @classmethod
    def __read_plugin_index_response(cls, response: Response) -> tuple[int, str]:
        """从同步流式响应读取有界的解压后索引文本。"""
        status_code = response.status_code
        if status_code != 200:
            return status_code, ""
        if cls.__declared_plugin_index_too_large(response.headers):
            raise _PluginIndexTooLargeError(
                f"插件索引响应超过 {PLUGIN_INDEX_MAX_BYTES} 字节"
            )
        content = bytearray()
        for chunk in response.iter_content(chunk_size=PLUGIN_INDEX_READ_CHUNK_SIZE):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > PLUGIN_INDEX_MAX_BYTES:
                raise _PluginIndexTooLargeError(
                    f"插件索引响应超过 {PLUGIN_INDEX_MAX_BYTES} 字节"
                )
        return status_code, content.decode("utf-8", errors="replace")

    @classmethod
    async def __async_read_plugin_index_response(
        cls,
        response: httpx2.Response,
    ) -> tuple[int, str]:
        """从异步流式响应读取有界的解压后索引文本。"""
        status_code = response.status_code
        if status_code != 200:
            return status_code, ""
        if cls.__declared_plugin_index_too_large(response.headers):
            raise _PluginIndexTooLargeError(
                f"插件索引响应超过 {PLUGIN_INDEX_MAX_BYTES} 字节"
            )
        content = bytearray()
        async for chunk in response.aiter_bytes(
            chunk_size=PLUGIN_INDEX_READ_CHUNK_SIZE
        ):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > PLUGIN_INDEX_MAX_BYTES:
                raise _PluginIndexTooLargeError(
                    f"插件索引响应超过 {PLUGIN_INDEX_MAX_BYTES} 字节"
                )
        return status_code, content.decode("utf-8", errors="replace")

    @classmethod
    def _build_plugin_index_request(
            cls,
            repo_url: str,
            package_version: Optional[str] = None,
    ) -> Optional[tuple[str, PluginRequestOptions]]:
        """构造插件索引请求，统一仓库解析、代际文件名和鉴权请求头。"""
        if not repo_url:
            return None

        user, repo = cls.get_repo_info(repo_url)
        if not user or not repo:
            return None

        raw_url = cls._base_url.format(user=user, repo=repo)
        package_file = (
            f"package.{package_version}.json"
            if package_version
            else "package.json"
        )
        package_url = cls.__append_cache_buster(f"{raw_url}{package_file}")
        headers = get_runtime_setting('REPO_GITHUB_HEADERS')(repo=f"{user}/{repo}")
        return package_url, headers

    @classmethod
    def __request_plugin_index_with_fallback(
        cls,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
    ) -> Optional[tuple[int, str]]:
        """按 GitHub 降级顺序流式读取同步插件索引，并限制解压后字节数。"""
        strategies = cls._build_github_request_strategies(
            url=url,
            headers=headers,
            timeout=timeout,
        )
        for strategy_name, target_url, request_params in strategies:
            logger.debug(
                f"[GitHub] 尝试使用策略：{strategy_name} 请求插件索引：{target_url}"
            )
            try:
                request = RequestUtils(**request_params)
                with request.get_stream(
                    url=target_url,
                    raise_exception=True,
                ) as response:
                    if response is None:
                        continue
                    return cls.__read_plugin_index_response(response)
            except _PluginIndexTooLargeError:
                raise
            except Exception as error:  # noqa: BLE001 - 失败后尝试下一传输策略
                logger.error(
                    f"[GitHub] 插件索引请求失败，策略：{strategy_name}，"
                    f"URL：{target_url}，错误：{error}"
                )
        logger.error(f"[GitHub] 所有策略均无法读取插件索引，URL：{url}")
        return None

    @classmethod
    async def __async_request_plugin_index_with_fallback(
        cls,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
    ) -> Optional[tuple[int, str]]:
        """按 GitHub 降级顺序流式读取异步插件索引，并限制解压后字节数。"""
        strategies = cls._build_github_request_strategies(
            url=url,
            headers=headers,
            timeout=timeout,
        )
        for strategy_name, target_url, request_params in strategies:
            logger.debug(
                f"[GitHub] 尝试使用策略：{strategy_name} 请求插件索引：{target_url}"
            )
            try:
                request = AsyncRequestUtils(**request_params)
                async with request.get_stream(
                    url=target_url,
                    raise_exception=True,
                ) as response:
                    if response is None:
                        continue
                    return await cls.__async_read_plugin_index_response(response)
            except _PluginIndexTooLargeError:
                raise
            except Exception as error:  # noqa: BLE001 - 失败后尝试下一传输策略
                logger.error(
                    f"[GitHub] 插件索引请求失败，策略：{strategy_name}，"
                    f"URL：{target_url}，错误：{error}"
                )
        logger.error(f"[GitHub] 所有策略均无法读取插件索引，URL：{url}")
        return None

    @classmethod
    def _resolve_plugin_index_response(
            cls,
            status_code: int,
            content: str,
    ) -> Optional[PluginIndex]:
        """统一解释插件索引 HTTP 响应，保留不存在、失败和有效索引三态。"""
        try:
            payload = cls._resolve_plugin_index_result((status_code, content))
        except RuntimeError:
            return None
        return payload if payload is not None else {}

    @classmethod
    def _resolve_plugin_index_result(
            cls,
            response: Optional[tuple[int, str]],
    ) -> Optional[PluginIndex]:
        """
        把传输结果统一映射为索引读取合同。

        :param response: 传输层返回的状态码和已受限响应文本；None 表示连接失败
        :return: 有效索引，或以 None 表示仓库明确不存在该索引
        :raises RuntimeError: 连接失败、非成功状态或响应格式无效
        """
        if response is None:
            raise RuntimeError("插件索引请求失败：连接失败")
        status_code, content = response
        if status_code == 404:
            return None
        if status_code != 200:
            raise RuntimeError(f"插件索引请求失败：HTTP {status_code}")
        payload = cls.__parse_plugin_index_response(content)
        if payload is None:
            raise RuntimeError("插件索引响应格式无效")
        return payload

    @staticmethod
    def __build_plugin_release_item(
        pid: str, release_info: PluginPayload
    ) -> Optional[PluginPayload]:
        """
        从 GitHub release 响应中提取可安装版本，仅接受规范 tag 与同名 zip 资产。
        """
        if not isinstance(release_info, dict):
            return None

        tag_name = release_info.get("tag_name")
        if not isinstance(tag_name, str):
            return None

        tag_prefix = f"{pid}_v"
        if not tag_name.startswith(tag_prefix):
            return None

        version = tag_name[len(tag_prefix):]
        if not version:
            return None

        asset_name = f"{tag_name.lower()}.zip"
        assets = release_info.get("assets") or []
        if not any(isinstance(asset, dict) and asset.get("name") == asset_name for asset in assets):
            return None

        return {
            "version": version,
            "tag_name": tag_name,
            "name": release_info.get("name") or tag_name,
            "published_at": release_info.get("published_at"),
            "body": release_info.get("body") or "",
            "asset_name": asset_name,
        }

    @staticmethod
    def __parse_plugin_release_response(
        pid: str, payload: object
    ) -> PluginReleaseList:
        """
        解析 GitHub release 列表，过滤出当前插件可直接安装的 release 资产。
        """
        if not isinstance(payload, list):
            return []

        releases: PluginReleaseList = []
        for release_info in payload:
            item = PluginMarketTransport.__build_plugin_release_item(pid, release_info)
            if item:
                releases.append(item)
        return releases

    @staticmethod
    def __normalize_plugin_release_response(
        payload: object,
    ) -> PluginReleaseList:
        """仅保留版本展示和资产匹配所需字段，控制仓库级缓存体积。"""
        if not isinstance(payload, list):
            return []
        return [
            {
                "tag_name": release_info.get("tag_name"),
                "name": release_info.get("name"),
                "published_at": release_info.get("published_at"),
                "body": release_info.get("body"),
                "assets": [
                    {"name": asset.get("name")}
                    for asset in release_info.get("assets") or []
                    if isinstance(asset, dict)
                ],
            }
            for release_info in payload
            if isinstance(release_info, dict)
        ]

    @classmethod
    def _iter_plugin_release_page_requests(
            cls,
            repo_url: str,
    ) -> Iterator[tuple[str, PluginRequestOptions]]:
        """按需生成仓库 Release 分页请求，统一仓库解析、请求头和页数上限。"""
        if not repo_url:
            return

        user, repo = cls.get_repo_info(repo_url)
        if not user or not repo:
            return

        user_repo = f"{user}/{repo}"
        headers = get_runtime_setting('REPO_GITHUB_HEADERS')(repo=user_repo)
        for page in range(1, 11):
            release_api = (
                f"https://api.github.com/repos/{user_repo}/releases"
                f"?per_page=100&page={page}"
            )
            yield cls.__append_cache_buster(release_api), headers

    @classmethod
    def _merge_plugin_release_page(
            cls,
            repo_url: str,
            response: Any,
            releases: PluginReleaseList,
    ) -> Optional[bool]:
        """合并一页 Release 响应；返回真继续、假结束，None 表示整次读取失败。"""
        if response is None or response.status_code != 200:
            return None

        try:
            payload = response.json()
        except Exception as error:
            logger.error(f"解析插件仓库 {repo_url} Release 列表失败：{error}")
            return None

        if not payload:
            return False
        if not isinstance(payload, list):
            return None

        releases.extend(cls.__normalize_plugin_release_response(payload))
        return len(payload) >= 100

    @cached(maxsize=1024, ttl=1800, skip_none=False)  # type: ignore[misc]
    def get_plugin_index_result(
            self,
            repo_url: str,
            package_version: Optional[str] = None,
    ) -> Optional[PluginIndex]:
        """读取插件索引；404 返回 None，读取失败由调用方记录。"""
        request = self._build_plugin_index_request(repo_url, package_version)
        if request is None:
            raise ValueError("插件仓库地址无效")
        package_url, headers = request
        response = self.__request_plugin_index_with_fallback(
            package_url,
            headers=headers,
        )
        return self._resolve_plugin_index_result(response)

    def get_plugins(self, repo_url: str,
                    package_version: Optional[str] = None) -> Optional[PluginIndex]:
        """
        获取 Github 插件列表，保留旧的 dict/{}/None 兼容返回。
        :param repo_url: Github仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        """
        try:
            payload = self.get_plugin_index_result(repo_url, package_version)
        except (ValueError, RuntimeError):
            return None
        return payload if payload is not None else {}

    @cached(maxsize=256, ttl=1800, shared_key="get_plugin_repo_releases")  # type: ignore[misc]
    def _get_plugin_repo_releases(
        self, repo_url: str
    ) -> Optional[PluginReleaseList]:
        """
        按仓库获取 GitHub Release 原始分页数据，供仓库内所有插件共享。
        """
        releases: PluginReleaseList = []
        for release_api, headers in self._iter_plugin_release_page_requests(
                repo_url
        ):
            res = self.__request_with_fallback(
                release_api,
                headers=headers,
                timeout=30,
                is_api=True,
            )
            should_continue = self._merge_plugin_release_page(
                repo_url,
                res,
                releases,
            )
            if should_continue is None:
                return None
            if not should_continue:
                break
        return releases

    def get_plugin_release_versions(
        self, pid: str, repo_url: str
    ) -> PluginReleaseList:
        """
        获取插件可安装的 GitHub Release 版本列表。

        GitHub 分页结果按仓库缓存，插件 ID 只参与本地过滤，避免同仓库重复分页。
        """
        if not pid or not repo_url:
            return []
        return self.__parse_plugin_release_response(pid, self._get_plugin_repo_releases(repo_url.rstrip("/")))

    def get_plugin_package_version(self, pid: str, repo_url: str,
                                   package_version: Optional[str] = None) -> Optional[str]:
        """
        检查并获取指定插件的可用版本，支持多版本优先级加载和版本兼容性检测
        1. 如果未指定版本，则使用系统配置的默认版本（通过 get_runtime_setting('VERSION_FLAG') 设置）
        2. 优先检查指定版本的插件（如 `package.v2.json`）
        3. 检查更低版本的 package 文件，并应用版本兼容标志
        4. 检查 `package.json` 文件，并应用共享实现兼容标志
        5. 如果插件不存在或不兼容指定版本，返回 `None`
        :param pid: 插件 ID，用于在插件列表中查找
        :param repo_url: 插件仓库的 URL，指定用于获取插件信息的 GitHub 仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如不指定则默认使用系统配置的版本
        :return: 返回可用的插件版本号 (如 "v2"，如果指定版本不可用则返回空字符串表示 v1)，如果插件不可用则返回 None
        """
        for candidate in self._package_version_candidates(package_version):
            selected = self._select_compatible_package_version(
                pid=pid,
                package_version=candidate,
                plugins=self.get_plugins(repo_url, candidate or None),
            )
            if selected is not None:
                return selected

        # 如果所有版本都不存在或插件不兼容，返回 None，表示插件不可用
        return None

    @staticmethod
    def get_repo_info(repo_url: str) -> tuple[Optional[str], Optional[str]]:
        """
        获取GitHub仓库信息
        """
        if not repo_url:
            return None, None
        if not repo_url.endswith("/"):
            repo_url += "/"
        if repo_url.count("/") < 6:
            repo_url = f"{repo_url}main/"
        try:
            user, repo = repo_url.split("/")[-4:-2]
        except Exception as e:
            logger.error(f"解析GitHub仓库地址失败：{str(e)} - {traceback.format_exc()}")
            return None, None
        return user, repo

    @staticmethod
    def _build_github_request_strategies(
            url: str,
            headers: Optional[dict[str, str]] = None,
            timeout: Optional[int] = 60,
            is_api: bool = False,
    ) -> list[tuple[str, str, PluginRequestOptions]]:
        """构造同步与异步 GitHub 请求共用的镜像、代理和直连顺序。"""
        strategies: list[tuple[str, str, PluginRequestOptions]] = []
        if not is_api and get_runtime_setting('GITHUB_PROXY'):
            proxy_url = (
                f"{UrlUtils.standardize_base_url(get_runtime_setting('GITHUB_PROXY'))}{url}"
            )
            strategies.append(
                ("镜像站", proxy_url, {"headers": headers, "timeout": timeout})
            )
        if get_runtime_setting('PROXY_HOST'):
            strategies.append(
                (
                    "代理",
                    url,
                    {
                        "headers": headers,
                        "proxies": get_runtime_setting('PROXY'),
                        "timeout": timeout,
                    },
                )
            )
        strategies.append(
            ("直连", url, {"headers": headers, "timeout": timeout})
        )
        return strategies

    @staticmethod
    def __request_with_fallback(url: str,
                                headers: Optional[dict[str, str]] = None,
                                timeout: Optional[int] = 60,
                                is_api: bool = False) -> Optional[Response]:
        """
        使用自动降级策略，请求资源，优先级依次为镜像站、代理、直连
        :param url: 目标URL
        :param headers: 请求头信息
        :param timeout: 请求超时时间
        :param is_api: 是否为GitHub API请求，API请求不走镜像站
        :return: 请求成功则返回 Response，失败返回 None
        """
        strategies = PluginMarketTransport._build_github_request_strategies(
            url=url,
            headers=headers,
            timeout=timeout,
            is_api=is_api,
        )

        # 遍历策略并尝试请求
        for strategy_name, target_url, request_params in strategies:
            logger.debug(f"[GitHub] 尝试使用策略：{strategy_name} 请求 URL：{target_url}")

            try:
                res = RequestUtils(**request_params).get_res(url=target_url, raise_exception=True)
                logger.debug(f"[GitHub] 请求成功，策略：{strategy_name}, URL: {target_url}")
                return res
            except Exception as e:
                logger.error(f"[GitHub] 请求失败，策略：{strategy_name}, URL: {target_url}，错误：{str(e)}")

        logger.error(f"[GitHub] 所有策略均请求失败，URL: {url}，请检查网络连接或 GitHub 配置")
        return None

    @staticmethod
    def request_with_fallback(
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Optional[Response]:
        """向包来源客户端公开既有同步 GitHub 降级传输。"""
        return PluginMarketTransport.__request_with_fallback(
            url, headers=headers, timeout=timeout, is_api=is_api
        )

    def __get_plugin_meta(
        self, pid: str, repo_url: str, package_version: Optional[str]
    ) -> PluginPayload:
        """读取远端插件元数据，供兼容性查询保留旧私有入口。"""
        try:
            plugins = self.get_plugins(repo_url, package_version or None) or {}
            meta = plugins.get(pid)
            return meta if isinstance(meta, dict) else {}
        except Exception as error:
            logger.error(f"获取插件 {pid} 元数据失败：{error}")
            return {}

    def get_plugin_system_version_check_message(self, pid: str, repo_url: str) -> Optional[str]:
        """
        获取指定插件来源的主系统版本兼容错误；兼容或无法定位元数据时返回 None。
        """
        if not pid or not repo_url:
            return None

        if self.is_local_repo_url(repo_url):
            candidate = self.get_local_plugin_candidate(
                pid=pid,
                package_version=self.parse_local_repo_package_version(repo_url),
                repo_path=self.parse_local_repo_path(repo_url),
                strict_compat=False
            )
            if not candidate:
                return None
            compatible, message = self.check_plugin_system_version(candidate)
            return None if compatible else message

        package_version = self.get_plugin_package_version(pid, repo_url, get_runtime_setting('VERSION_FLAG'))
        if package_version is None:
            return None
        meta = self.__get_plugin_meta(pid, repo_url, package_version)
        compatible, message = self.check_plugin_system_version(meta)
        return None if compatible else message

    async def async_get_plugin_system_version_check_message(self, pid: str, repo_url: str) -> Optional[str]:
        """
        异步获取指定插件来源的主系统版本兼容错误；兼容或无法定位元数据时返回 None。
        """
        if not pid or not repo_url:
            return None

        if self.is_local_repo_url(repo_url):
            return await asyncio.to_thread(self.get_plugin_system_version_check_message, pid, repo_url)

        package_version = await self.async_get_plugin_package_version(pid, repo_url, get_runtime_setting('VERSION_FLAG'))
        if package_version is None:
            return None
        meta = await self.__async_get_plugin_meta(pid, repo_url, package_version)
        compatible, message = self.check_plugin_system_version(meta)
        return None if compatible else message

    async def async_get_plugin_package_version(self, pid: str, repo_url: str,
                                               package_version: Optional[str] = None) -> Optional[str]:
        """
        异步版本的获取插件版本方法，功能同 get_plugin_package_version
        """
        for candidate in self._package_version_candidates(package_version):
            selected = self._select_compatible_package_version(
                pid=pid,
                package_version=candidate,
                plugins=await self.async_get_plugins(
                    repo_url,
                    candidate or None,
                ),
            )
            if selected is not None:
                return selected

        return None

    @staticmethod
    async def __async_request_with_fallback(url: str,
                                            headers: Optional[dict[str, str]] = None,
                                            timeout: Optional[int] = 60,
                                            is_api: bool = False) -> Optional[httpx2.Response]:
        """
        使用自动降级策略，异步请求资源，优先级依次为镜像站、代理、直连
        :param url: 目标URL
        :param headers: 请求头信息
        :param timeout: 请求超时时间
        :param is_api: 是否为GitHub API请求，API请求不走镜像站
        :return: 请求成功则返回 Response，失败返回 None
        """
        strategies = PluginMarketTransport._build_github_request_strategies(
            url=url,
            headers=headers,
            timeout=timeout,
            is_api=is_api,
        )

        # 遍历策略并尝试请求
        for strategy_name, target_url, request_params in strategies:
            logger.debug(f"[GitHub] 尝试使用策略：{strategy_name} 请求 URL：{target_url}")

            try:
                res = await AsyncRequestUtils(**request_params).get_res(url=target_url, raise_exception=True)
                logger.debug(f"[GitHub] 请求成功，策略：{strategy_name}, URL: {target_url}")
                return res
            except Exception as e:
                logger.error(f"[GitHub] 请求失败，策略：{strategy_name}, URL: {target_url}，错误：{str(e)}")

        logger.error(f"[GitHub] 所有策略均请求失败，URL: {url}，请检查网络连接或 GitHub 配置")
        return None

    @staticmethod
    async def async_request_with_fallback(
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Optional[httpx2.Response]:
        """向包来源客户端公开既有异步 GitHub 降级传输。"""
        return await PluginMarketTransport.__async_request_with_fallback(
            url, headers=headers, timeout=timeout, is_api=is_api
        )

    async def __async_get_plugin_meta(
        self, pid: str, repo_url: str, package_version: Optional[str]
    ) -> PluginPayload:
        """异步读取远端插件元数据，供兼容性查询保留旧私有入口。"""
        try:
            plugins = await self.async_get_plugins(
                repo_url, package_version or None
            ) or {}
            meta = plugins.get(pid)
            return meta if isinstance(meta, dict) else {}
        except Exception as error:
            logger.warning(f"获取插件 {pid} 元数据失败：{error}")
            return {}

    @cached(maxsize=1024, ttl=1800, skip_none=False)  # type: ignore[misc]
    async def async_get_plugin_index_result(
            self,
            repo_url: str,
            package_version: Optional[str] = None,
    ) -> Optional[PluginIndex]:
        """异步读取插件索引；404 返回 None，读取失败由调用方记录。"""
        request = self._build_plugin_index_request(repo_url, package_version)
        if request is None:
            raise ValueError("插件仓库地址无效")
        package_url, headers = request
        response = await self.__async_request_plugin_index_with_fallback(
            package_url,
            headers=headers,
        )
        return self._resolve_plugin_index_result(response)

    async def async_get_plugins(self, repo_url: str,
                                package_version: Optional[str] = None) -> Optional[PluginIndex]:
        """
        异步获取 Github 插件列表，保留旧的 dict/{}/None 兼容返回。
        :param repo_url: Github仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        """
        try:
            payload = await self.async_get_plugin_index_result(
                repo_url,
                package_version,
            )
        except (ValueError, RuntimeError):
            return None
        return payload if payload is not None else {}

    @cached(maxsize=256, ttl=1800, shared_key="get_plugin_repo_releases")  # type: ignore[misc]
    async def _async_get_plugin_repo_releases(
        self, repo_url: str
    ) -> Optional[PluginReleaseList]:
        """
        异步按仓库获取 GitHub Release 原始分页数据。
        """
        releases: PluginReleaseList = []
        for release_api, headers in self._iter_plugin_release_page_requests(
                repo_url
        ):
            res = await self.__async_request_with_fallback(
                release_api,
                headers=headers,
                timeout=30,
                is_api=True,
            )
            should_continue = self._merge_plugin_release_page(
                repo_url,
                res,
                releases,
            )
            if should_continue is None:
                return None
            if not should_continue:
                break
        return releases

    async def async_get_plugin_release_versions(
        self, pid: str, repo_url: str
    ) -> PluginReleaseList:
        """
        异步获取插件可安装的 GitHub Release 版本列表。

        同一事件循环内，同仓库的并发读取和强制刷新共享一个请求任务。
        """
        if not pid or not repo_url:
            return []

        loop = asyncio.get_running_loop()
        normalized_repo_url = repo_url.rstrip("/")
        normal_task_key = (loop, normalized_repo_url, False)
        force_task_key = (loop, normalized_repo_url, True)
        with self._release_task_lock:
            if is_fresh():
                force_task = self._release_tasks.get(force_task_key)
                if force_task and not force_task.done():
                    task_key = force_task_key
                    task = force_task
                else:
                    pending_normal_task = self._release_tasks.get(normal_task_key)
                    if pending_normal_task and pending_normal_task.done():
                        pending_normal_task = None
                    task_key = force_task_key
                    task = get_task_registry().create(
                        self._async_refresh_plugin_repo_releases(
                            normalized_repo_url,
                            pending_normal_task,
                        ),
                        owner="plugin.market.release_refresh",
                    )
                    self._release_tasks[task_key] = task
                    task.add_done_callback(
                        lambda completed_task: self._remove_release_task(task_key, completed_task)
                    )
            else:
                task_key = normal_task_key
                pending_normal_task = self._release_tasks.get(normal_task_key)
                if pending_normal_task is None or pending_normal_task.done():
                    task = get_task_registry().create(
                        self._async_get_plugin_repo_releases(normalized_repo_url),
                        owner="plugin.market.release_read",
                    )
                    self._release_tasks[task_key] = task
                    task.add_done_callback(
                        lambda completed_task: self._remove_release_task(task_key, completed_task)
                    )
                else:
                    task = pending_normal_task

        payload = await asyncio.shield(task)
        return self.__parse_plugin_release_response(pid, payload)

    async def async_has_plugin_release_cache(self, repo_url: str) -> bool:
        """
        判断指定仓库的 Release 列表缓存是否已经存在。
        """
        if not repo_url:
            return False
        return bool(
            await self._async_get_plugin_repo_releases.cache_exists(
                self, repo_url.rstrip("/")
            )
        )

    async def _async_refresh_plugin_repo_releases(
        self,
        repo_url: str,
        pending_normal_task: Optional[PluginReleaseTask],
    ) -> Optional[PluginReleaseList]:
        """等待在途普通读取落盘后执行强刷，确保旧结果不会覆盖强刷缓存。"""
        if pending_normal_task:
            try:
                await asyncio.shield(pending_normal_task)
            except (Exception, asyncio.CancelledError):
                pass
        return cast(
            Optional[PluginReleaseList],
            await self._async_get_plugin_repo_releases(repo_url),
        )

    @classmethod
    def _remove_release_task(
        cls,
        task_key: tuple[asyncio.AbstractEventLoop, str, bool],
        task: PluginReleaseTask,
    ) -> None:
        """请求任务完成后释放事件循环和仓库引用。"""
        with cls._release_task_lock:
            if cls._release_tasks.get(task_key) is task:
                cls._release_tasks.pop(task_key, None)


class PluginMarketClient:
    """把插件市场、版本元数据和本地仓库查询隔离为只读客户端。"""

    def __init__(self, transport: Optional[PluginMarketTransport] = None) -> None:
        """保存索引传输端口；默认实现必须先由兼容市场模块显式注册。"""
        self._transport = transport or PluginMarketTransport()

    def get_plugins(
        self,
        repo_url: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[PluginIndex]:
        """同步读取指定仓库和代际的插件索引。"""
        with fresh(force):
            return self._transport.get_plugins(repo_url, package_version)

    async def async_get_plugins(
        self,
        repo_url: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[PluginIndex]:
        """异步读取指定仓库和代际的插件索引。"""
        async with async_fresh(force):
            return await self._transport.async_get_plugins(repo_url, package_version)

    def get_plugin_index_result(
        self,
        repo_url: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[PluginIndex]:
        """读取插件索引的三态结果，供库存读取保留失败事实。"""
        with fresh(force):
            return cast(
                Optional[PluginIndex],
                self._transport.get_plugin_index_result(repo_url, package_version),
            )

    async def async_get_plugin_index_result(
        self,
        repo_url: str,
        package_version: Optional[str] = None,
        force: bool = False,
    ) -> Optional[PluginIndex]:
        """异步读取插件索引的三态结果，供库存读取保留失败事实。"""
        async with async_fresh(force):
            return cast(
                Optional[PluginIndex],
                await self._transport.async_get_plugin_index_result(
                    repo_url,
                    package_version,
                ),
            )

    def get_local_candidates(self) -> PluginIndex:
        """返回全部本地插件仓库候选。"""
        return self._transport.get_local_plugin_candidates()

    def get_local_candidate(
        self,
        plugin_id: str,
        package_version: Optional[str] = None,
        repo_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> Optional[PluginPayload]:
        """返回指定插件的本地仓库候选。"""
        return self._transport.get_local_plugin_candidate(
            pid=plugin_id,
            package_version=package_version,
            repo_path=repo_path,
            **kwargs,
        )

    @staticmethod
    def get_local_repo_paths() -> list[Path]:
        """返回配置中有效的本地插件仓库目录。"""
        value = get_runtime_setting('PLUGIN_LOCAL_REPO_PATHS')
        if not value:
            return []
        root_path = Path(get_runtime_setting('ROOT_PATH'))
        paths: list[Path] = []
        for item in value.split(","):
            local_repo_path = item.strip()
            if not local_repo_path:
                continue
            path = Path(local_repo_path).expanduser()
            if not path.is_absolute():
                path = root_path / path
            paths.append(path.resolve())
        return paths

    @staticmethod
    def make_local_repo_url(
        plugin_id: str,
        repo_path: Optional[object] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """生成兼容旧入口的本地插件来源标识。"""
        return build_local_repo_url(
            plugin_id,
            repo_path=cast(Optional[Path], repo_path),
            package_version=package_version,
        )

    @staticmethod
    def is_local_repo_url(repo_url: Optional[str]) -> bool:
        """判断插件来源是否为本地仓库标识。"""
        return is_local_plugin_source(repo_url)

    @staticmethod
    def annotate_system_version(plugin_info: PluginPayload) -> PluginPayload:
        """补充插件所需 MoviePilot 版本兼容状态。"""
        compatible, message = check_plugin_system_version(
            plugin_info,
            current_version=get_app_version(),
        )
        plugin_info["system_version_compatible"] = compatible
        plugin_info["system_version_message"] = message
        return plugin_info

    @staticmethod
    def is_package_compatible(
        plugin_info: PluginPayload,
        package_version: Optional[str],
    ) -> bool:
        """判断插件条目是否兼容目标插件包代际。"""
        return is_plugin_generation_compatible(
            plugin_info,
            package_version,
            current_generation=get_runtime_setting('VERSION_FLAG'),
            free_threaded=is_free_threaded_runtime(),
        )


class PluginPackageSourceClient:
    """为系统包 owner 提供只读市场元数据和远端制品传输。"""

    def __init__(self, transport: Optional[PluginMarketTransport] = None) -> None:
        """保存由组合根注入的市场传输，不导入兼容 Helper 实现。"""
        self._transport = transport or PluginMarketTransport()

    @staticmethod
    def is_local_repo_url(repo_url: Optional[str]) -> bool:
        """判断来源是否为本地仓库标识。"""
        return is_local_repo_url(repo_url)

    @staticmethod
    def parse_local_repo_url(repo_url: str) -> Optional[str]:
        """解析本地仓库来源中的插件标识。"""
        return parse_local_repo_plugin_id(repo_url)

    @staticmethod
    def parse_local_repo_path(repo_url: str) -> Optional[Path]:
        """解析本地仓库来源中的物理路径。"""
        root = get_runtime_setting('ROOT_PATH')
        return parse_local_repo_path(
            repo_url,
            root_path=Path(root) if root is not None else None,
        )

    @staticmethod
    def parse_local_repo_package_version(repo_url: str) -> Optional[str]:
        """解析本地仓库来源中的包代际。"""
        return parse_local_repo_generation(repo_url)

    @staticmethod
    def make_local_repo_url(
        plugin_id: str,
        repo_path: Optional[Path] = None,
        package_version: Optional[str] = None,
    ) -> str:
        """生成本地仓库来源标识。"""
        return build_local_repo_url(
            plugin_id,
            repo_path=repo_path,
            package_version=package_version,
        )

    def get_local_plugin_candidate(
        self,
        pid: str,
        package_version: Optional[str] = None,
        repo_path: Optional[Path] = None,
        strict_compat: bool = True,
        strict_system_version: bool = True,
    ) -> Optional[PluginPayload]:
        """读取一个本地插件候选。"""
        return self._transport.get_local_plugin_candidate(
            pid=pid,
            package_version=package_version,
            repo_path=repo_path,
            strict_compat=strict_compat,
            strict_system_version=strict_system_version,
        )

    @staticmethod
    def check_plugin_system_version(
        plugin_info: PluginPayload,
    ) -> tuple[bool, str]:
        """校验插件声明的宿主版本约束。"""
        return check_plugin_system_version(
            plugin_info, current_version=get_app_version()
        )

    def get_plugin_package_version(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str],
    ) -> Optional[str]:
        """选择适用于当前宿主的远端索引代际。"""
        return self._transport.get_plugin_package_version(
            plugin_id, repo_url, package_version
        )

    async def async_get_plugin_package_version(
        self,
        plugin_id: str,
        repo_url: str,
        package_version: Optional[str],
    ) -> Optional[str]:
        """异步选择适用于当前宿主的远端索引代际。"""
        return await self._transport.async_get_plugin_package_version(
            plugin_id, repo_url, package_version
        )

    def get_plugins(
        self, repo_url: str, package_version: Optional[str] = None
    ) -> Optional[PluginIndex]:
        """读取远端插件索引。"""
        return self._transport.get_plugins(repo_url, package_version)

    async def async_get_plugins(
        self, repo_url: str, package_version: Optional[str] = None
    ) -> Optional[PluginIndex]:
        """异步读取远端插件索引。"""
        return await self._transport.async_get_plugins(repo_url, package_version)

    def get_plugin_release_versions(
        self, plugin_id: str, repo_url: str
    ) -> list[dict[str, Any]]:
        """读取插件可安装 Release。"""
        return self._transport.get_plugin_release_versions(plugin_id, repo_url)

    async def async_get_plugin_release_versions(
        self, plugin_id: str, repo_url: str
    ) -> list[dict[str, Any]]:
        """异步读取插件可安装 Release。"""
        return await self._transport.async_get_plugin_release_versions(
            plugin_id, repo_url
        )

    @staticmethod
    def get_repo_info(repo_url: str) -> tuple[Optional[str], Optional[str]]:
        """解析 GitHub 仓库所有者和仓库名。"""
        if not repo_url:
            return None, None
        parts = repo_url.rstrip("/").split("/")
        if len(parts) < 5:
            return None, None
        return parts[-2], parts[-1]

    def request(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Any:
        """同步读取 GitHub 元数据或制品。"""
        return self._transport.request_with_fallback(
            url, headers=headers, timeout=timeout, is_api=is_api
        )

    async def async_request(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[int] = 60,
        is_api: bool = False,
    ) -> Any:
        """异步读取 GitHub 元数据或制品。"""
        return await self._transport.async_request_with_fallback(
            url, headers=headers, timeout=timeout, is_api=is_api
        )


setattr(
    PluginMarketTransport.get_plugin_release_versions,
    "cache_clear",
    getattr(PluginMarketTransport._get_plugin_repo_releases, "cache_clear"),
)
setattr(
    PluginMarketTransport.get_plugin_release_versions,
    "cache_region",
    getattr(PluginMarketTransport._get_plugin_repo_releases, "cache_region"),
)
setattr(
    PluginMarketTransport.async_get_plugin_release_versions,
    "cache_clear",
    getattr(PluginMarketTransport._async_get_plugin_repo_releases, "cache_clear"),
)
setattr(
    PluginMarketTransport.async_get_plugin_release_versions,
    "cache_region",
    getattr(PluginMarketTransport._async_get_plugin_repo_releases, "cache_region"),
)
