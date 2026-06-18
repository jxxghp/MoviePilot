import asyncio
from collections import deque
import importlib
import io
import json
import shutil
import site
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Callable, Awaitable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import aiofiles
import aioshutil
import httpx
from anyio import Path as AsyncPath
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version, InvalidVersion
from importlib.metadata import distributions
from requests import Response

from app.core.cache import cached, is_fresh
from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.utils.singleton import WeakSingleton
from app.utils.string import StringUtils
from app.utils.system import SystemUtils
from app.utils.url import UrlUtils
from version import APP_VERSION

PLUGIN_DIR = Path(settings.ROOT_PATH) / "app" / "plugins"
LOCAL_REPO_PREFIX = "local://"
PLUGIN_SYSTEM_VERSION_FIELD = "system_version"


class PluginHelper(metaclass=WeakSingleton):
    """
    插件市场管理，下载安装插件到本地
    """

    _base_url = "https://raw.githubusercontent.com/{user}/{repo}/main/"
    # 串行化运行期依赖安装，避免多个 pip 子进程和导入缓存刷新互相踩踏。
    _pip_install_lock = threading.Lock()
    # 这些包一旦被插件覆盖，最容易直接拖垮主程序启动，因此冲突提示需要单独高亮。
    _protected_runtime_packages = frozenset({
        "alembic",
        "fastapi",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "sqlalchemy",
        "starlette",
        "uvicorn",
    })
    _runtime_import_probe = (
        "import alembic, fastapi, pydantic, pydantic_core, pydantic_settings, "
        "sqlalchemy, starlette, uvicorn; from pydantic import BaseModel, Field"
    )

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    @staticmethod
    def is_local_repo_url(repo_url: Optional[str]) -> bool:
        """
        判断是否为本地插件来源标识
        """
        return bool(repo_url and repo_url.startswith(LOCAL_REPO_PREFIX))

    @staticmethod
    def make_local_repo_url(pid: str, repo_path: Optional[Path] = None,
                            package_version: Optional[str] = None) -> str:
        """
        生成本地插件安装来源标识
        """
        repo_url = f"{LOCAL_REPO_PREFIX}{quote(pid, safe='')}"
        params = []
        if repo_path:
            params.append(f"path={quote(str(repo_path), safe='/:~')}")
        if package_version:
            params.append(f"version={quote(package_version, safe='')}")
        if params:
            repo_url = f"{repo_url}?{'&'.join(params)}"
        return repo_url

    @staticmethod
    def parse_local_repo_url(repo_url: str) -> Optional[str]:
        """
        从本地插件来源标识中解析插件ID
        """
        if not PluginHelper.is_local_repo_url(repo_url):
            return None
        try:
            parts = urlsplit(repo_url)
            pid = unquote(parts.netloc or parts.path.strip("/"))
        except Exception:
            pid = repo_url[len(LOCAL_REPO_PREFIX):].split("?", 1)[0].strip("/")
        return pid or None

    @staticmethod
    def parse_local_repo_path(repo_url: str) -> Optional[Path]:
        """
        从本地插件来源标识中解析仓库路径
        """
        if not PluginHelper.is_local_repo_url(repo_url):
            return None
        try:
            values = parse_qs(urlsplit(repo_url).query).get("path")
            if not values:
                return None
            path = Path(values[0]).expanduser()
            if not path.is_absolute():
                path = settings.ROOT_PATH / path
            return path.resolve()
        except Exception:
            return None

    @staticmethod
    def parse_local_repo_package_version(repo_url: str) -> Optional[str]:
        """
        从本地插件来源标识中解析 package 版本
        """
        if not PluginHelper.is_local_repo_url(repo_url):
            return None
        try:
            values = parse_qs(urlsplit(repo_url).query).get("version")
            if not values:
                return None
            return values[0]
        except Exception:
            return None

    @staticmethod
    def get_current_system_version() -> Optional[Version]:
        """
        解析当前主程序版本，供插件 package 中的系统版本范围匹配使用。
        """
        try:
            return Version(str(APP_VERSION))
        except InvalidVersion:
            logger.error(f"当前主程序版本号无法解析：{APP_VERSION}")
            return None

    @classmethod
    def check_plugin_system_version(cls, plugin_info: Optional[dict]) -> Tuple[bool, str]:
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
                f"请使用 pip 依赖版本格式，例如 >=2.12.0,<3"
            )

        system_version = cls.get_current_system_version()
        if system_version is None:
            return False, f"当前 MoviePilot 版本 {APP_VERSION} 无法解析，已拒绝安装带版本限制的插件"

        try:
            specifier_set = SpecifierSet(raw_specifier)
        except InvalidSpecifier:
            return False, (
                f"插件限定的系统版本范围格式不正确：{raw_specifier}，"
                f"请使用 pip 依赖版本格式，例如 >=2.12.0,<3"
            )

        if specifier_set.contains(system_version, prereleases=True):
            return True, ""

        return False, (
            f"插件要求 MoviePilot 版本 {raw_specifier}，当前版本 {APP_VERSION} 不满足，已拒绝安装"
        )

    @classmethod
    def annotate_plugin_system_version(cls, plugin_info: dict) -> dict:
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
    def get_local_repo_paths() -> List[Path]:
        """
        获取本地插件仓库目录列表
        """
        if not settings.PLUGIN_LOCAL_REPO_PATHS:
            return []
        paths = []
        for item in settings.PLUGIN_LOCAL_REPO_PATHS.split(","):
            local_repo_path = item.strip()
            if not local_repo_path:
                continue
            path = Path(local_repo_path).expanduser()
            if not path.is_absolute():
                path = settings.ROOT_PATH / path
            paths.append(path.resolve())
        return paths

    @staticmethod
    def __get_local_package(repo_path: Path, package_version: Optional[str] = None) -> Optional[Dict[str, dict]]:
        """
        从本地插件仓库读取 package.json 或 package.{version}.json
        """
        package_file = repo_path / (
            f"package.{package_version}.json" if package_version else "package.json"
        )
        if not package_file.exists():
            return {}
        try:
            content = package_file.read_text(encoding="utf-8")
            payload = json.loads(content)
        except Exception as e:
            logger.warn(f"读取本地插件包 {package_file} 失败：{e}")
            return None
        if not isinstance(payload, dict):
            logger.warn(f"本地插件包 {package_file} 格式不正确")
            return None
        return payload

    @staticmethod
    def __get_local_plugin_dir(repo_path: Path, pid: str, package_version: Optional[str]) -> Path:
        plugin_root = f"plugins.{package_version}" if package_version else "plugins"
        return repo_path / plugin_root / pid.lower()

    def get_local_plugin_candidates(self) -> Dict[str, dict]:
        """
        扫描本地插件仓库，按插件ID保留版本号最高的候选
        """
        candidates: Dict[str, dict] = {}
        for repo_order, repo_path in enumerate(self.get_local_repo_paths()):
            if not repo_path.exists() or not repo_path.is_dir():
                logger.warn(f"本地插件仓库目录不存在或不可读：{repo_path}")
                continue

            package_candidates = []
            if settings.VERSION_FLAG:
                package_candidates.append((settings.VERSION_FLAG, self.__get_local_package(repo_path,
                                                                                           settings.VERSION_FLAG)))
            package_candidates.append(("", self.__get_local_package(repo_path)))

            for package_version, local_plugins in package_candidates:
                if local_plugins is None:
                    continue
                for pid, plugin_info in local_plugins.items():
                    if not isinstance(plugin_info, dict):
                        continue
                    # package.json 中的旧结构需要声明兼容当前版本。
                    if (
                            not package_version
                            and settings.VERSION_FLAG
                            and plugin_info.get(settings.VERSION_FLAG) is not True
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
                    self.annotate_plugin_system_version(candidate)
                    candidate_version = str(candidate.get("version") or "0")

                    existing = candidates.get(pid)
                    if not existing:
                        candidates[pid] = candidate
                        continue

                    existing_version = str(existing.get("version") or "0")
                    if StringUtils.compare_version(candidate_version, ">", existing_version):
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
                                   strict_system_version: bool = True) -> Optional[dict]:
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
                if settings.VERSION_FLAG:
                    package_versions.append(settings.VERSION_FLAG)
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
                        is_compatible = not (
                                not current_package_version
                                and settings.VERSION_FLAG
                                and plugin_info.get(settings.VERSION_FLAG) is not True
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
                        if not is_compatible:
                            candidate["compatible"] = False
                            candidate["skip_reason"] = f"package.json 未声明 {settings.VERSION_FLAG} 兼容"
                        self.annotate_plugin_system_version(candidate)
                        if strict_system_version and candidate.get("system_version_compatible") is False:
                            candidate["compatible"] = False
                            candidate["skip_reason"] = candidate.get("system_version_message")
                        if package_version is not None:
                            return candidate
                        if not selected_candidate:
                            selected_candidate = candidate
                            continue
                        selected_version = str(selected_candidate.get("version") or "0")
                        candidate_version = str(candidate.get("version") or "0")
                        if StringUtils.compare_version(candidate_version, ">", selected_version):
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
    def __parse_plugin_index_response(content: str) -> Optional[Dict[str, dict]]:
        """
        解析插件索引响应，仅缓存成功解析出的字典结果。
        """
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            if "404: Not Found" not in content:
                logger.warn(f"插件包数据解析失败：{content}")
            return None

        if not isinstance(payload, dict):
            logger.warn(f"插件包数据格式不正确，期望 dict，实际为 {type(payload).__name__}")
            return None

        return payload

    @staticmethod
    def __build_plugin_release_item(pid: str, release_info: dict) -> Optional[dict]:
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
    def __parse_plugin_release_response(pid: str, payload) -> List[dict]:
        """
        解析 GitHub release 列表，过滤出当前插件可直接安装的 release 资产。
        """
        if not isinstance(payload, list):
            return []

        releases = []
        for release_info in payload:
            item = PluginHelper.__build_plugin_release_item(pid, release_info)
            if item:
                releases.append(item)
        return releases

    @cached(maxsize=128, ttl=1800)
    def get_plugins(self, repo_url: str,
                    package_version: Optional[str] = None) -> Optional[Dict[str, dict]]:
        """
        获取Github所有最新插件列表
        :param repo_url: Github仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        """
        if not repo_url:
            return None

        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return None

        raw_url = self._base_url.format(user=user, repo=repo)
        package_url = f"{raw_url}package.{package_version}.json" if package_version else f"{raw_url}package.json"
        package_url = self.__append_cache_buster(package_url)

        res = self.__request_with_fallback(package_url, headers=settings.REPO_GITHUB_HEADERS(repo=f"{user}/{repo}"))
        if res is None:
            return None
        if res.status_code == 404:
            return {}
        if res.status_code != 200:
            return None
        return self.__parse_plugin_index_response(res.text)

    @cached(maxsize=128, ttl=1800)
    def get_plugin_release_versions(self, pid: str, repo_url: str) -> List[dict]:
        """
        获取插件可安装的 GitHub Release 版本列表。
        """
        if not pid or not repo_url:
            return []

        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return []

        user_repo = f"{user}/{repo}"
        releases = []
        for page in range(1, 11):
            release_api = f"https://api.github.com/repos/{user_repo}/releases?per_page=100&page={page}"
            release_api = self.__append_cache_buster(release_api)
            res = self.__request_with_fallback(
                release_api,
                headers=settings.REPO_GITHUB_HEADERS(repo=user_repo),
                timeout=30,
                is_api=True,
            )
            if res is None or res.status_code != 200:
                break

            try:
                payload = res.json()
                if not payload:
                    break
                releases.extend(self.__parse_plugin_release_response(pid, payload))
                if len(payload) < 100:
                    break
            except Exception as e:
                logger.error(f"解析插件 {pid} Release 列表失败：{e}")
                break
        return releases

    @staticmethod
    def __has_installable_release_version(release_items: List[dict], release_version: str) -> bool:
        """
        指定版本必须来自已解析出的可安装 Release 列表，避免直接拼接任意 tag。
        """
        return any(item.get("version") == release_version for item in release_items)

    def get_plugin_package_version(self, pid: str, repo_url: str,
                                   package_version: Optional[str] = None) -> Optional[str]:
        """
        检查并获取指定插件的可用版本，支持多版本优先级加载和版本兼容性检测
        1. 如果未指定版本，则使用系统配置的默认版本（通过 settings.VERSION_FLAG 设置）
        2. 优先检查指定版本的插件（如 `package.v2.json`）
        3. 如果插件不存在于指定版本，检查 `package.json` 文件，查看该插件是否兼容指定版本
        4. 如果插件不存在或不兼容指定版本，返回 `None`
        :param pid: 插件 ID，用于在插件列表中查找
        :param repo_url: 插件仓库的 URL，指定用于获取插件信息的 GitHub 仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如不指定则默认使用系统配置的版本
        :return: 返回可用的插件版本号 (如 "v2"，如果指定版本不可用则返回空字符串表示 v1)，如果插件不可用则返回 None
        """
        # 如果没有指定版本，则使用当前系统配置的版本（如 "v2"）
        if not package_version:
            package_version = settings.VERSION_FLAG

        # 优先检查指定版本的插件，即 package.v(x).json 文件中是否存在该插件，如果存在，返回该版本号
        if pid in (self.get_plugins(repo_url, package_version) or []):
            return package_version

        # 如果指定版本的插件不存在，检查全局 package.json 文件，查看插件是否兼容指定的版本
        plugin = (self.get_plugins(repo_url) or {}).get(pid, None)
        # 检查插件是否明确支持当前指定的版本（如 v2 或 v3），如果支持，返回空字符串表示使用 package.json（v1）
        if plugin and plugin.get(package_version) is True:
            return ""

        # 如果所有版本都不存在或插件不兼容，返回 None，表示插件不可用
        return None

    @staticmethod
    def get_repo_info(repo_url: str) -> Tuple[Optional[str], Optional[str]]:
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

    def install(self, pid: str, repo_url: str, package_version: Optional[str] = None,
                release_version: Optional[str] = None, force_install: bool = False) \
            -> Tuple[bool, str]:
        """
        安装插件，包括依赖安装和文件下载，相关资源支持自动降级策略
        1. 检查并获取插件的指定版本，确认版本兼容性
        2. 从 GitHub 获取文件列表（包括 requirements.txt）
        3. 删除旧的插件目录（如非强制安装则进行备份）
        4. 下载并预安装 requirements.txt 中的依赖（如果存在）
        5. 下载并安装插件的其他文件
        6. 再次尝试安装依赖（确保安装完整）
        :param pid: 插件 ID
        :param repo_url: 插件仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如不指定则默认使用系统配置的版本
        :param release_version: 指定安装的 release 资产版本；未指定时安装当前索引版本
        :param force_install: 是否强制安装插件，默认不启用，启用时不进行备份和恢复操作
        :return: (是否成功, 错误信息)
        """
        if self.is_local_repo_url(repo_url):
            return self.install_local(pid=pid, repo_url=repo_url, force_install=force_install)

        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"

        # 验证参数
        if not pid or not repo_url:
            return False, "参数错误"

        # 从 GitHub 的 repo_url 获取用户和项目名
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"

        user_repo = f"{user}/{repo}"

        if not package_version:
            package_version = settings.VERSION_FLAG

        # 1. 优先检查指定版本的插件
        package_version = self.get_plugin_package_version(pid, repo_url, package_version)
        # 如果 package_version 为None，说明没有找到匹配的插件
        if package_version is None:
            msg = f"{pid} 没有找到适用于当前版本的插件"
            logger.debug(msg)
            return False, msg
        # package_version 为空，表示从 package.json 中找到插件
        elif package_version == "":
            logger.debug(f"{pid} 从 package.json 中找到适用于当前版本的插件")
        else:
            logger.debug(f"{pid} 从 package.{package_version}.json 中找到适用于当前版本的插件")

        # 2. 决定安装方式（release 或文件列表）并执行统一安装流程。
        meta = self.__get_plugin_meta(pid, repo_url, package_version)
        # 是否使用 Release 打包。Release 缺失或资产不可用时仍保留文件列表兜底，
        # 避免索引先发布、Actions 打包滞后导致插件短时间无法安装。
        is_release = meta.get("release")
        # 插件版本号
        plugin_version = meta.get("version")
        if release_version:
            if not is_release:
                return False, f"{pid} 未声明 Release 安装，无法安装指定版本"
            if not self.__has_installable_release_version(
                    self.get_plugin_release_versions(pid, repo_url), release_version
            ):
                return False, f"{pid} 未找到可安装的 Release 版本：{release_version}"
            if release_version == plugin_version:
                compatible, message = self.check_plugin_system_version(meta)
                if not compatible:
                    logger.debug(f"{pid} 插件系统版本兼容性检查失败：{message}")
                    return False, message
            release_tag = f"{pid}_v{release_version}"

            def prepare_selected_release() -> Tuple[bool, str]:
                return self.__install_from_release(pid, user_repo, release_tag)

            return self.__install_flow_sync(pid, force_install, prepare_selected_release, repo_url)

        compatible, message = self.check_plugin_system_version(meta)
        if not compatible:
            logger.debug(f"{pid} 插件系统版本兼容性检查失败：{message}")
            return False, message
        if is_release:
            # 使用 插件ID_插件版本号 作为 Release tag
            if not plugin_version:
                return False, f"未在插件清单中找到 {pid} 的版本号，无法进行 Release 安装"
            # 拼接 release_tag
            release_tag = f"{pid}_v{plugin_version}"

            # 使用 release 进行安装
            def prepare_release() -> Tuple[bool, str]:
                ok, msg = self.__install_from_release(pid, user_repo, release_tag)
                if ok:
                    return True, msg
                logger.warning(f"{pid} Release 安装失败，回退文件列表安装：{msg}")
                self.__remove_old_plugin(pid)
                return self.__prepare_content_via_filelist_sync(pid.lower(), user_repo, package_version)

            return self.__install_flow_sync(pid, force_install, prepare_release, repo_url)
        else:
            # 未声明 release 打包的插件继续使用文件列表方式安装。
            def prepare_filelist() -> Tuple[bool, str]:
                return self.__prepare_content_via_filelist_sync(pid.lower(), user_repo, package_version)

            return self.__install_flow_sync(pid, force_install, prepare_filelist, repo_url)

    def install_local(self, pid: str, repo_url: str = "", force_install: bool = False) -> Tuple[bool, str]:
        """
        从本地插件仓库目录安装插件
        """
        local_pid = self.parse_local_repo_url(repo_url) if repo_url else pid
        if not local_pid or local_pid.lower() != pid.lower():
            return False, "本地插件来源与插件ID不匹配"

        repo_path = self.parse_local_repo_path(repo_url) if repo_url else None
        package_version = self.parse_local_repo_package_version(repo_url) if repo_url else None
        candidate = self.get_local_plugin_candidate(
            pid,
            package_version=package_version,
            repo_path=repo_path
        )
        if not candidate:
            return False, f"未找到本地插件：{pid}"
        compatible, message = self.check_plugin_system_version(candidate)
        if not compatible:
            logger.debug(f"{pid} 本地插件系统版本兼容性检查失败：{message}")
            return False, message

        source_dir = Path(candidate.get("path"))
        dest_dir = PLUGIN_DIR / pid.lower()
        try:
            if source_dir.resolve() == dest_dir.resolve():
                return False, "本地插件来源不能与运行目录相同"
        except Exception:
            return False, "本地插件来源路径无效"

        def prepare_local() -> Tuple[bool, str]:
            try:
                shutil.copytree(
                    source_dir,
                    dest_dir,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
                )
                return True, ""
            except Exception as e:
                logger.error(f"复制本地插件 {pid} 失败：{e}")
                return False, f"复制本地插件失败：{e}"

        return self.__install_flow_sync(
            pid=pid,
            force_install=force_install,
            prepare_content=prepare_local,
            repo_url=repo_url or self.make_local_repo_url(
                pid,
                candidate.get("repo_path"),
                candidate.get("package_version")
            )
        )

    def __get_file_list(self, pid: str, user_repo: str, package_version: Optional[str] = None) -> \
            Tuple[Optional[list], Optional[str]]:
        """
        获取插件的文件列表
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (文件列表, 错误信息)
        """
        file_api = f"https://api.github.com/repos/{user_repo}/contents/plugins"
        # 如果 package_version 存在（如 "v2"），则加上版本号
        if package_version:
            file_api += f".{package_version}"
        file_api += f"/{pid.lower()}"

        res = self.__request_with_fallback(file_api,
                                           headers=settings.REPO_GITHUB_HEADERS(repo=user_repo),
                                           is_api=True,
                                           timeout=30)
        if res is None:
            return None, "连接仓库失败"
        elif res.status_code != 200:
            return None, f"连接仓库失败：{res.status_code} - " \
                         f"{'超出速率限制，请设置Github Token或稍后重试' if res.status_code == 403 else res.reason}"

        try:
            ret = res.json()
            if isinstance(ret, list) and len(ret) > 0 and "message" not in ret[0]:
                return ret, ""
            else:
                return None, "插件在仓库中不存在或返回数据格式不正确"
        except Exception as e:
            logger.error(f"插件数据解析失败：{e}")
            return None, "插件数据解析失败"

    def __download_files(self, pid: str, file_list: List[dict], user_repo: str,
                         package_version: Optional[str] = None, skip_requirements: bool = False) -> Tuple[bool, str]:
        """
        下载插件文件
        :param pid: 插件 ID
        :param file_list: 要下载的文件列表，包含文件的元数据（包括下载链接）
        :param user_repo: GitHub 仓库的 user/repo 路径
        :param skip_requirements: 是否跳过 requirements.txt 文件的下载
        :return: (是否成功, 错误信息)
        """
        if not file_list:
            return False, "文件列表为空"

        # 使用栈结构来替代递归调用，避免递归深度过大问题
        stack = [(pid, file_list)]

        while stack:
            current_pid, current_file_list = stack.pop()

            for item in current_file_list:
                # 跳过 requirements.txt 的下载
                if skip_requirements and item.get("name") == "requirements.txt":
                    continue

                if item.get("download_url"):
                    logger.debug(f"正在下载文件：{item.get('path')}")
                    res = self.__request_with_fallback(item.get('download_url'),
                                                       headers=settings.REPO_GITHUB_HEADERS(repo=user_repo))
                    if not res:
                        return False, f"文件 {item.get('path')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('path')} 失败：{res.status_code}"

                    # 确保文件路径不包含版本号（如 v2、v3），如果有 package_version，移除路径中的版本号
                    relative_path = item.get("path")
                    if package_version:
                        relative_path = relative_path.replace(f"plugins.{package_version}", "plugins", 1)

                    # 创建插件文件夹并写入文件
                    file_path = Path(settings.ROOT_PATH) / "app" / relative_path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                    logger.debug(f"文件 {item.get('path')} 下载成功，保存路径：{file_path}")
                else:
                    # 如果是子目录，则将子目录内容加入栈中继续处理
                    sub_list, msg = self.__get_file_list(f"{current_pid}/{item.get('name')}", user_repo,
                                                         package_version)
                    if not sub_list:
                        return False, msg
                    stack.append((f"{current_pid}/{item.get('name')}", sub_list))

        return True, ""

    def __download_and_install_requirements(self, requirements_file_info: dict, pid: str, user_repo: str) \
            -> Tuple[bool, str]:
        """
        下载并安装 requirements.txt 文件中的依赖
        :param requirements_file_info: requirements.txt 文件的元数据信息
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (是否成功, 错误信息)
        """
        # 下载 requirements.txt
        res = self.__request_with_fallback(requirements_file_info.get("download_url"),
                                           headers=settings.REPO_GITHUB_HEADERS(repo=user_repo))
        if not res:
            return False, "requirements.txt 文件下载失败"
        elif res.status_code != 200:
            return False, f"下载 requirements.txt 文件失败：{res.status_code}"

        requirements_txt = res.text
        if requirements_txt.strip():
            # 保存并安装依赖
            requirements_file_path = PLUGIN_DIR / pid.lower() / "requirements.txt"
            requirements_file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(requirements_file_path, "w", encoding="utf-8") as f:
                f.write(requirements_txt)

            return self.pip_install_with_fallback(requirements_file_path)

        return True, ""  # 如果 requirements.txt 为空，视作成功

    def __install_dependencies_if_required(self, pid: str) -> Tuple[bool, bool, str]:
        """
        安装插件依赖。
        :param pid: 插件 ID
        :return: (是否存在依赖，安装是否成功, 错误信息)
        """
        # 定位插件目录和依赖文件
        plugin_dir = PLUGIN_DIR / pid.lower()
        requirements_file = plugin_dir / "requirements.txt"

        # 检查是否存在 requirements.txt 文件
        if requirements_file.exists():
            logger.info(f"{pid} 存在依赖，开始尝试安装依赖")
            success, error_message = self.pip_install_with_fallback(requirements_file)
            if success:
                return True, True, ""
            else:
                return True, False, error_message

        return False, False, "不存在依赖"

    @staticmethod
    def __backup_plugin(pid: str) -> str:
        """
        备份旧插件目录
        :param pid: 插件 ID
        :return: 备份目录路径
        """
        plugin_dir = PLUGIN_DIR / pid.lower()
        backup_dir = Path(settings.TEMP_PATH) / "plugin_backup" / pid.lower()

        if plugin_dir.exists():
            # 备份时清理已有的备份目录，防止残留文件影响
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
                logger.debug(f"{pid} 旧的备份目录已清理 {backup_dir}")

            shutil.copytree(plugin_dir, backup_dir, dirs_exist_ok=True)
            logger.debug(f"{pid} 插件已备份到 {backup_dir}")

        return str(backup_dir) if backup_dir.exists() else None

    @staticmethod
    def __restore_plugin(pid: str, backup_dir: str):
        """
        还原旧插件目录
        :param pid: 插件 ID
        :param backup_dir: 备份目录路径
        """
        plugin_dir = PLUGIN_DIR / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
            logger.debug(f"{pid} 已清理插件目录 {plugin_dir}")

        if Path(backup_dir).exists():
            shutil.copytree(backup_dir, plugin_dir, dirs_exist_ok=True)
            logger.debug(f"{pid} 已还原插件目录 {plugin_dir}")
            shutil.rmtree(backup_dir, ignore_errors=True)
            logger.debug(f"{pid} 已删除备份目录 {backup_dir}")

    @staticmethod
    def __remove_old_plugin(pid: str):
        """
        删除旧插件
        :param pid: 插件 ID
        """
        plugin_dir = PLUGIN_DIR / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)

    @staticmethod
    def refresh_persistent_plugin_backup(pid: str) -> bool:
        """
        刷新插件持久化备份目录，供 docker 重置后恢复使用
        """
        if not SystemUtils.is_docker():
            return True

        plugin_dir = PLUGIN_DIR / pid.lower()
        if not plugin_dir.exists():
            logger.warn(f"{pid} 插件目录不存在，跳过刷新插件备份")
            return False

        backup_root = settings.CONFIG_PATH / "plugins_backup"
        backup_dir = backup_root / pid.lower()
        try:
            backup_root.mkdir(parents=True, exist_ok=True)
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            shutil.copytree(
                plugin_dir,
                backup_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
            )
            logger.info(f"已刷新插件备份: {pid}")
            return True
        except Exception as e:
            logger.error(f"刷新插件备份失败: {pid} - {e}")
            return False

    def __collect_plugin_wheels_dirs(self) -> List[Path]:
        """
        收集已安装插件目录下可用的 wheels 目录，供批量依赖安装时复用。
        """
        wheels_dirs = []
        try:
            install_plugins = {
                plugin_id.lower()
                for plugin_id in self.systemconfig.get(SystemConfigKey.UserInstalledPlugins) or []
            }
            for plugin_id in install_plugins:
                wheels_dir = PLUGIN_DIR / plugin_id / "wheels"
                if wheels_dir.is_dir():
                    wheels_dirs.append(wheels_dir)
        except Exception as e:
            logger.error(f"收集插件 wheels 目录时发生错误：{e}")
            return []

        # 去重并保持稳定顺序，避免重复传递相同目录
        return list(dict.fromkeys(wheels_dirs))

    @staticmethod
    def __build_pip_install_strategies(base_cmd: List[str]) -> List[Tuple[str, List[str]]]:
        """
        为 pip 命令构建统一的网络降级策略，避免不同安装路径各自拼接参数。
        """
        strategies = []
        if settings.PIP_PROXY:
            strategies.append(("镜像站", base_cmd + ["-i", settings.PIP_PROXY]))
        if settings.PROXY_HOST:
            strategies.append(("代理", base_cmd + ["--proxy", settings.PROXY_HOST]))
        strategies.append(("直连", base_cmd))
        return strategies

    @staticmethod
    def __build_runtime_pip_command(*args: str) -> List[str]:
        """
        优先使用当前解释器同目录的 pip 入口，以便 uv-pip-compat 能接管兼容命令。
        """
        pip_name = "pip.exe" if sys.platform == "win32" else "pip"
        pip_bin = Path(sys.executable).with_name(pip_name)
        if pip_bin.exists():
            return [str(pip_bin), *args]
        return [sys.executable, "-m", "pip", *args]

    @staticmethod
    def __format_pkg_name_for_pip(name: str) -> str:
        """
        将内部统一使用的下划线包名转回 pip 更常见的连字符写法，便于日志和约束文件阅读。
        """
        return name.replace("_", "-")

    @staticmethod
    def __marker_matches(marker, extra: str = "") -> bool:
        """
        使用当前运行环境和可选 extra 上下文判断 marker 是否生效。
        """
        if not marker:
            return True
        try:
            env = default_environment()
            env["extra"] = extra
            return marker.evaluate(env)
        except Exception as err:
            logger.debug(f"依赖 marker 计算失败，按不匹配处理：{err}")
            return False

    @classmethod
    def __parse_project_requirement_roots(
            cls,
            requirements_file: Path,
            visited_files: Optional[Set[Path]] = None
    ) -> Dict[str, Set[str]]:
        """
        解析主项目 requirements 文件，收集根依赖及其启用的 extras。
        支持递归处理 -r/--requirement，忽略索引、约束等 pip 选项。
        """
        roots = {}
        if visited_files is None:
            visited_files = set()

        try:
            requirements_file = requirements_file.resolve()
        except Exception:
            requirements_file = Path(requirements_file)

        if requirements_file in visited_files:
            return roots
        visited_files.add(requirements_file)

        if not requirements_file.exists():
            logger.warning(f"主项目依赖文件不存在：{requirements_file}")
            return roots

        try:
            with open(requirements_file, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue

                    include_path = None
                    if line.startswith("-r"):
                        include_path = line[2:].strip() if line != "-r" else ""
                    elif line.startswith("--requirement"):
                        include_path = line[len("--requirement"):].strip()

                    if include_path is not None:
                        if include_path.startswith("="):
                            include_path = include_path[1:].strip()
                        if not include_path:
                            logger.debug(f"忽略无法识别的 requirements 引用：{line}")
                            continue
                        included_roots = cls.__parse_project_requirement_roots(
                            requirements_file.parent / include_path,
                            visited_files
                        )
                        for package_name, extras in included_roots.items():
                            roots.setdefault(package_name, set()).update(extras)
                        continue

                    if line.startswith((
                            "-c", "--constraint", "-i", "--index-url", "--extra-index-url",
                            "-f", "--find-links", "--trusted-host", "--no-index"
                    )):
                        continue

                    try:
                        requirement = Requirement(line)
                    except Exception as err:
                        logger.debug(f"无法解析主项目依赖项 '{line}'：{err}")
                        continue

                    if not cls.__marker_matches(requirement.marker):
                        continue

                    package_name = cls.__standardize_pkg_name(requirement.name)
                    roots.setdefault(package_name, set()).update(
                        extra.lower() for extra in requirement.extras
                    )
            return roots
        except Exception as e:
            logger.error(f"解析主项目依赖文件失败：{requirements_file} - {e}")
            return {}

    @classmethod
    def __get_installed_distribution_requirements(cls) -> Dict[str, Tuple[Version, List[Requirement]]]:
        """
        获取当前环境中每个已安装包的依赖声明，用于展开主程序依赖图。
        """
        requirement_graph = {}
        try:
            for dist in distributions():
                name = dist.metadata.get("Name")
                if not name:
                    continue

                package_name = cls.__standardize_pkg_name(name)
                version_str = dist.metadata.get("Version") or getattr(dist, "version", None)
                if not version_str:
                    continue

                try:
                    version = Version(version_str)
                except InvalidVersion:
                    logger.debug(f"无法解析已安装包 '{package_name}' 的版本：{version_str}")
                    continue

                requirements = []
                for raw_requirement in dist.requires or []:
                    try:
                        requirements.append(Requirement(raw_requirement))
                    except Exception as err:
                        logger.debug(f"无法解析已安装包 '{package_name}' 的依赖项 '{raw_requirement}'：{err}")

                if package_name not in requirement_graph or version > requirement_graph[package_name][0]:
                    requirement_graph[package_name] = (version, requirements)
            return requirement_graph
        except Exception as e:
            logger.error(f"收集已安装包依赖图时发生错误：{e}")
            return {}

    @classmethod
    def __get_protected_runtime_packages(
            cls,
            installed_packages: Optional[Dict[str, Version]] = None
    ) -> Dict[str, Version]:
        """
        仅收集主程序依赖图中的已安装包版本。

        主项目 requirements 中声明的根依赖及其当前已安装的传递依赖都会被冻结，
        未被主程序依赖图引用的插件自带包允许后续插件按需升级或降级。
        """
        if installed_packages is None:
            installed_packages = cls.__get_installed_packages()
        protected_packages = {
            package_name: version
            for package_name, version in installed_packages.items()
            if package_name in cls._protected_runtime_packages
        }

        root_requirements_file = settings.ROOT_PATH / "requirements.txt"
        if not root_requirements_file.exists():
            root_requirements_file = settings.ROOT_PATH / "requirements.in"

        root_requirements = cls.__parse_project_requirement_roots(root_requirements_file)
        if not root_requirements:
            return protected_packages

        requirement_graph = cls.__get_installed_distribution_requirements()
        active_extras = {
            package_name: set(extras)
            for package_name, extras in root_requirements.items()
        }
        pending_packages = deque(active_extras.keys())
        processed_extras: Dict[str, Set[str]] = {}

        while pending_packages:
            package_name = pending_packages.popleft()
            selected_extras = active_extras.get(package_name, set())
            previous_extras = processed_extras.get(package_name)
            if previous_extras is not None and selected_extras.issubset(previous_extras):
                continue

            processed_extras[package_name] = set(selected_extras)
            if package_name in installed_packages:
                protected_packages[package_name] = installed_packages[package_name]

            _, requirements = requirement_graph.get(package_name, (None, []))
            if not requirements:
                continue

            active_extra_values = [""] + sorted(selected_extras)
            for requirement in requirements:
                if requirement.marker and not any(
                        cls.__marker_matches(requirement.marker, extra)
                        for extra in active_extra_values
                ):
                    continue

                dep_name = cls.__standardize_pkg_name(requirement.name)
                known_extras = active_extras.setdefault(dep_name, set())
                before_len = len(known_extras)
                known_extras.update(extra.lower() for extra in requirement.extras)
                if dep_name not in processed_extras or len(known_extras) != before_len:
                    pending_packages.append(dep_name)

        return protected_packages

    @staticmethod
    def __is_upgrade_only_conflict(specifier_set: SpecifierSet, installed_version: Version) -> bool:
        """
        判断版本冲突是否只能通过升级来解决（specifier 允许的所有版本都严格高于已安装版本）。
        返回 True 表示纯升级冲突；返回 False 表示可能需要降级或无法确定方向。
        """
        has_lower_bound = False
        for spec in specifier_set:
            op = spec.operator
            ver_str = spec.version.rstrip("*").rstrip(".") or "0"
            try:
                ver = Version(ver_str)
            except InvalidVersion:
                return False

            if op in ("<", "<="):
                upper = ver if op == "<" else Version(f"{ver}.post0")
                if upper <= installed_version:
                    return False
            elif op == "==":
                if ver <= installed_version:
                    return False
            elif op == "~=":
                # ~=X.Y.Z 等价于 >=X.Y.Z, <X.(Y+1)；若 X.Y.Z <= 已安装版本说明需降级
                if ver <= installed_version:
                    return False
                has_lower_bound = True
            elif op in (">=", ">"):
                has_lower_bound = True
            # != 操作符：单独出现时可能允许低版本，需结合其他约束判断

        # 若没有任何明确的下限约束（仅 != 等），保守地视为不确定 → 返回 False
        return has_lower_bound

    @classmethod
    def __validate_runtime_dependency_conflicts(
            cls,
            requirements_file: Path,
            protected_packages: Dict[str, Version]
    ) -> Tuple[bool, str]:
        """
        在真正执行 pip 前，先拦截插件对主程序依赖的显式覆盖请求。

        共享 venv 场景下，仅冻结主程序依赖；插件新增依赖、以及插件之间共享的额外依赖，
        允许后续安装继续调整版本。
        """
        conflicts = []
        try:
            with open(requirements_file, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        requirement = Requirement(line)
                    except Exception as err:
                        logger.debug(f"无法解析依赖项 '{line}'，跳过运行环境冲突预检：{err}")
                        continue

                    if not cls.__marker_matches(requirement.marker):
                        continue

                    package_name = cls.__standardize_pkg_name(requirement.name)
                    installed_version = protected_packages.get(package_name)
                    if installed_version is None:
                        continue

                    if requirement.url:
                        conflicts.append((
                            package_name,
                            str(installed_version),
                            f"来自 {requirement.url} 的同名包",
                            package_name in cls._protected_runtime_packages,
                        ))
                        continue

                    if requirement.specifier and not requirement.specifier.contains(
                            installed_version,
                            prereleases=True
                    ):
                        is_core = package_name in cls._protected_runtime_packages
                        # 非核心包的纯升级冲突（插件要求更新版本）允许放行，由 pip 约束文件控制实际安装
                        if is_core or not cls.__is_upgrade_only_conflict(
                                requirement.specifier, installed_version):
                            conflicts.append((
                                package_name,
                                str(installed_version),
                                str(requirement.specifier),
                                is_core,
                            ))
        except Exception as e:
            logger.error(f"执行运行环境依赖冲突预检时发生错误：{e}")
            return False, f"插件依赖预检失败：{e}"

        if not conflicts:
            return True, ""

        def sort_key(item: Tuple[str, str, str, bool]) -> Tuple[int, str]:
            return 0 if item[3] else 1, item[0]

        details = []
        for package_name, installed_version, expected, _is_protected in sorted(conflicts, key=sort_key)[:5]:
            details.append(
                f"{cls.__format_pkg_name_for_pip(package_name)} 当前为 {installed_version}，"
                f"插件要求 {expected}"
            )
        if len(conflicts) > 5:
            details.append(f"其余 {len(conflicts) - 5} 项冲突已省略")

        scope = "主程序核心依赖" if any(item[3] for item in conflicts) else "主程序依赖"
        return False, (
            f"插件依赖与当前运行环境的{scope}冲突：{'；'.join(details)}。"
            f"为避免共享运行环境被污染，已拒绝安装。"
        )

    @classmethod
    def __create_runtime_constraints_file(cls, protected_packages: Dict[str, Version]) -> Path:
        """
        以主程序依赖的当前已安装版本生成临时约束文件，确保插件安装不会改写主程序依赖。
        """
        temp_dir = Path(settings.TEMP_PATH) / "plugin_dependencies"
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=temp_dir,
                prefix="runtime-constraints-",
                suffix=".txt",
                delete=False
        ) as temp_file:
            for package_name, version in sorted(protected_packages.items()):
                if package_name in cls._protected_runtime_packages:
                    # 核心包严格锁定，插件不得改写
                    temp_file.write(f"{cls.__format_pkg_name_for_pip(package_name)}=={version}\n")
                else:
                    # 非核心主程序依赖：允许升级，但禁止降级
                    temp_file.write(f"{cls.__format_pkg_name_for_pip(package_name)}>={version}\n")
        return Path(temp_file.name)

    @staticmethod
    def __refresh_import_system():
        """
        依赖安装或修复后刷新当前解释器的导入缓存，保证后续动态导入能看到新状态。
        """
        importlib.reload(site)
        importlib.invalidate_caches()

    @classmethod
    def __run_runtime_healthcheck(cls) -> Tuple[bool, str]:
        """
        安装完成后立即执行运行环境自检，尽量在插件加载前发现依赖图已被污染。
        """
        checks = [
            ("pip check", cls.__build_runtime_pip_command("check")),
            ("核心依赖导入检查", [sys.executable, "-c", cls._runtime_import_probe]),
        ]
        for check_name, command in checks:
            success, message = SystemUtils.execute_with_subprocess(command)
            if not success:
                return False, f"{check_name}失败：{message}"
        return True, ""

    @classmethod
    def __repair_main_runtime_dependencies(cls, snapshot_file: Optional[Path] = None) -> Tuple[bool, str]:
        """
        依赖安装后如果发现主运行环境已异常，优先恢复主程序依赖快照；
        若快照不可用，再按主项目依赖重新安装进行自愈。
        """
        repair_target = snapshot_file
        repair_desc = "主程序依赖快照"
        if repair_target and not repair_target.exists():
            repair_target = None
        if repair_target is None:
            repair_target = settings.ROOT_PATH / "requirements.txt"
            repair_desc = "主程序 requirements.txt"
        if not repair_target.exists():
            return False, f"恢复依赖文件不存在：{repair_target}"

        last_error = ""
        base_cmd = [sys.executable, "-m", "pip", "install", "-r", str(repair_target)]
        for strategy_name, pip_command in cls.__build_pip_install_strategies(base_cmd):
            logger.warning(f"[PIP] 运行环境异常，尝试使用策略：{strategy_name} 恢复{repair_desc}")
            success, message = SystemUtils.execute_with_subprocess(pip_command)
            if success:
                cls.__refresh_import_system()
                return True, message
            last_error = message
            logger.error(f"[PIP] 使用策略：{strategy_name} 恢复{repair_desc}失败：{message}")
        return False, last_error or f"恢复{repair_desc}失败"

    @classmethod
    def pip_install_with_fallback(cls,
                                  requirements_file: Path,
                                  find_links_dirs: Optional[List[Path]] = None) -> Tuple[bool, str]:
        """
        使用自动降级策略安装依赖，并确保新安装的包可被动态导入
        :param requirements_file: 依赖的 requirements.txt 文件路径
        :param find_links_dirs: 额外的本地 wheels 目录列表
        :return: (是否成功, 错误信息)
        """
        wheels_dir = requirements_file.parent / "wheels"
        candidate_dirs = []
        if wheels_dir.is_dir():
            candidate_dirs.append(wheels_dir)
        if find_links_dirs:
            candidate_dirs.extend(find_links_dirs)

        # 去重并保持传入顺序
        resolved_dirs = []
        seen_dirs = set()
        for candidate_dir in candidate_dirs:
            candidate_path = Path(candidate_dir)
            if not candidate_path.is_dir():
                continue
            candidate_key = str(candidate_path.resolve())
            if candidate_key in seen_dirs:
                continue
            seen_dirs.add(candidate_key)
            resolved_dirs.append(candidate_path)

        find_links_option = []
        if resolved_dirs:
            for local_wheels_dir in resolved_dirs:
                logger.debug(f"[PIP] 发现可用的 wheels 目录: {local_wheels_dir}，将优先从本地安装。")
                find_links_option.extend(["--find-links", str(local_wheels_dir)])
        else:
            logger.debug(f"[PIP] 未发现可用的 wheels 目录，将仅使用在线源。")

        installed_packages = cls.__get_installed_packages()
        protected_packages = cls.__get_protected_runtime_packages(installed_packages)
        check_ok, check_message = cls.__validate_runtime_dependency_conflicts(requirements_file, protected_packages)
        if not check_ok:
            logger.error(f"[PIP] 运行环境冲突预检失败：{check_message}")
            return False, check_message

        constraints_file = None
        if protected_packages:
            try:
                constraints_file = cls.__create_runtime_constraints_file(protected_packages)
            except Exception as e:
                logger.error(f"[PIP] 创建运行环境约束文件失败：{e}")
                return False, f"创建运行环境约束文件失败：{e}"

        base_cmd = [sys.executable, "-m", "pip", "install"] + find_links_option
        if constraints_file:
            # 这里固定约束到主程序依赖的当前版本，避免共享 venv 被插件改写核心运行环境。
            base_cmd.extend(["-c", str(constraints_file)])
        base_cmd.extend(["-r", str(requirements_file)])
        strategies = cls.__build_pip_install_strategies(base_cmd)

        try:
            # pip 会修改当前解释器的 site-packages，安装与缓存刷新必须串行，避免运行态模块被并发安装窗口污染。
            with cls._pip_install_lock:
                loaded_modules_before_install = set(sys.modules.keys())
                # 遍历策略进行安装
                for strategy_name, pip_command in strategies:
                    logger.debug(f"[PIP] 尝试使用策略：{strategy_name} 安装依赖，命令：{' '.join(pip_command)}")
                    success, message = SystemUtils.execute_with_subprocess(pip_command)
                    if success:
                        logger.debug(f"[PIP] 策略：{strategy_name} 安装依赖成功，输出：{message}")
                        health_ok, health_message = cls.__run_runtime_healthcheck()
                        if not health_ok:
                            logger.error(f"[PIP] 依赖安装后运行环境自检失败：{health_message}")
                            repair_ok, repair_message = cls.__repair_main_runtime_dependencies(
                                constraints_file if protected_packages else None
                            )
                            if repair_ok:
                                health_restored, restored_message = cls.__run_runtime_healthcheck()
                                if health_restored:
                                    cls.__refresh_import_system()
                                    return False, (
                                        f"依赖安装后运行环境自检失败，已自动恢复主程序依赖：{health_message}"
                                    )
                                logger.error(
                                    f"[PIP] 主程序依赖恢复后仍未通过健康检查：{restored_message}"
                                )
                                return False, (
                                    f"依赖安装后运行环境自检失败，恢复主程序依赖后仍异常："
                                    f"{restored_message}"
                                )
                            return False, (
                                f"依赖安装后运行环境自检失败，且自动恢复主程序依赖失败："
                                f"{repair_message}"
                            )

                        cls.__refresh_import_system()
                        loaded_modules_after_install = set(sys.modules.keys())
                        loaded_modules_during_install = loaded_modules_after_install - loaded_modules_before_install
                        logger.debug(f"[PIP] 已刷新导入系统，新加载的模块: {loaded_modules_during_install}")
                        return True, message

                    logger.error(f"[PIP] 策略：{strategy_name} 安装依赖失败，错误信息：{message}")
        finally:
            if constraints_file:
                constraints_file.unlink(missing_ok=True)

        return False, "[PIP] 所有策略均安装依赖失败，请检查网络连接、PIP 配置或插件依赖约束"

    @staticmethod
    def __request_with_fallback(url: str,
                                headers: Optional[dict] = None,
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
        strategies = []

        # 1. 尝试使用镜像站，镜像站一般不支持API请求，因此API请求直接跳过镜像站
        if not is_api and settings.GITHUB_PROXY:
            proxy_url = f"{UrlUtils.standardize_base_url(settings.GITHUB_PROXY)}{url}"
            strategies.append(("镜像站", proxy_url, {"headers": headers, "timeout": timeout}))

        # 2. 尝试使用代理
        if settings.PROXY_HOST:
            strategies.append(("代理", url, {"headers": headers, "proxies": settings.PROXY, "timeout": timeout}))

        # 3. 最后尝试直连
        strategies.append(("直连", url, {"headers": headers, "timeout": timeout}))

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

    def __get_plugin_meta(self, pid: str, repo_url: str,
                          package_version: Optional[str]) -> dict:
        try:
            plugins = (
                          self.get_plugins(repo_url) if not package_version
                          else self.get_plugins(repo_url, package_version)
                      ) or {}
            meta = plugins.get(pid)
            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            logger.error(f"获取插件 {pid} 元数据失败：{e}")
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

        package_version = self.get_plugin_package_version(pid, repo_url, settings.VERSION_FLAG)
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

        package_version = await self.async_get_plugin_package_version(pid, repo_url, settings.VERSION_FLAG)
        if package_version is None:
            return None
        meta = await self.__async_get_plugin_meta(pid, repo_url, package_version)
        compatible, message = self.check_plugin_system_version(meta)
        return None if compatible else message

    def __install_flow_sync(self, pid: str, force_install: bool,
                            prepare_content: Callable[[], Tuple[bool, str]],
                            repo_url: Optional[str] = None) -> Tuple[bool, str]:
        """
        同步安装统一流程：备份→清理→准备内容→安装依赖→上报
        prepare_content 负责把插件文件放到 app/plugins/{pid}
        """
        backup_dir = None
        if not force_install:
            backup_dir = self.__backup_plugin(pid)

        self.__remove_old_plugin(pid)

        success, message = prepare_content()
        if not success:
            logger.error(f"{pid} 准备插件内容失败：{message}")
            if backup_dir:
                self.__restore_plugin(pid, backup_dir)
                logger.warn(f"{pid} 插件安装失败，已还原备份插件")
            else:
                self.__remove_old_plugin(pid)
                logger.warn(f"{pid} 已清理对应插件目录，请尝试重新安装")
            return False, message

        dependencies_exist, dep_ok, dep_msg = self.__install_dependencies_if_required(pid)
        if dependencies_exist and not dep_ok:
            logger.error(f"{pid} 依赖安装失败：{dep_msg}")
            if backup_dir:
                self.__restore_plugin(pid, backup_dir)
                logger.warn(f"{pid} 插件安装失败，已还原备份插件")
            else:
                self.__remove_old_plugin(pid)
                logger.warn(f"{pid} 已清理对应插件目录，请尝试重新安装")
            return False, dep_msg

        self.refresh_persistent_plugin_backup(pid)
        return True, ""

    def __install_from_release(self, pid: str, user_repo: str, release_tag: str) -> Tuple[bool, str]:
        """
        通过 GitHub Release 资产文件安装插件。
        规范：release 中存在名为 "{pid}_v{version}.zip" 的资产，zip 根即插件文件；
        将其全部解压到 app/plugins/{pid}
        """
        # 拼接资产文件名
        asset_name = f"{release_tag.lower()}.zip"

        release_api = f"https://api.github.com/repos/{user_repo}/releases/tags/{release_tag}"
        rel_res = self.__request_with_fallback(
            release_api,
            headers=settings.REPO_GITHUB_HEADERS(repo=user_repo),
            timeout=30,
            is_api=True,
        )
        if rel_res is None or rel_res.status_code != 200:
            return False, f"获取 Release 信息失败：{rel_res.status_code if rel_res else '连接失败'}"

        try:
            rel_json = rel_res.json()
            assets = rel_json.get("assets") or []
            asset = next((a for a in assets if a.get("name") == asset_name), None)
            if not asset:
                return False, f"未找到资产文件：{asset_name}"
            asset_id = asset.get("id")
            if not asset_id:
                return False, "资产缺少ID信息"
            # 构建资产的API下载URL
            download_url = f"https://api.github.com/repos/{user_repo}/releases/assets/{asset_id}"
        except Exception as e:
            logger.error(f"解析 Release 信息失败：{e}")
            return False, f"解析 Release 信息失败：{e}"

        # 使用资产的API端点下载，需要设置Accept头为application/octet-stream
        headers = settings.REPO_GITHUB_HEADERS(repo=user_repo).copy()
        headers["Accept"] = "application/octet-stream"
        res = self.__request_with_fallback(download_url, headers=headers, is_api=True)
        if res is None or res.status_code != 200:
            return False, f"下载资产失败：{res.status_code if res else '连接失败'}"

        try:
            with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
                namelist = zf.namelist()
                if not namelist:
                    return False, "压缩包内容为空"
                # 若所有条目均在同一顶层目录下（如 pid/），则剥离这一层，避免出现双层目录
                names_with_slash = [n for n in namelist if '/' in n]
                base_prefix = ''
                if names_with_slash and len(names_with_slash) == len(namelist):
                    first_seg = names_with_slash[0].split('/')[0]
                    if all(n.startswith(first_seg + '/') for n in namelist):
                        base_prefix = first_seg + '/'

                dest_base = Path(settings.ROOT_PATH) / "app" / "plugins" / pid.lower()
                wrote_any = False
                for name in namelist:
                    rel_path = name[len(base_prefix):]
                    if not rel_path:
                        continue
                    if rel_path.endswith('/'):
                        (dest_base / rel_path.rstrip('/')).mkdir(parents=True, exist_ok=True)
                        continue
                    dest_path = dest_base / rel_path
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name, 'r') as src, open(dest_path, 'wb') as dst:
                        dst.write(src.read())
                    wrote_any = True
                if not wrote_any:
                    return False, "压缩包中无可写入文件"
            return True, ""
        except Exception as e:
            logger.error(f"解压 Release 压缩包失败：{e}")
            return False, f"解压 Release 压缩包失败：{e}"

    def find_missing_dependencies(self) -> List[str]:
        """
        收集所有需要安装或更新的依赖项
        1. 收集所有插件的依赖项，合并版本约束
        2. 获取已安装的包及其版本
        3. 比较已安装的包与所需的依赖项，找出需要安装或升级的包
        :return: 需要安装或更新的依赖项列表，例如 ["package1>=1.0.0", "package2"]
        """
        try:
            # 收集所有插件的依赖项
            plugin_dependencies = self.__find_plugin_dependencies()  # 返回格式为 {package_name: version_specifier}
            # 获取已安装的包及其版本
            installed_packages = self.__get_installed_packages()  # 返回格式为 {package_name: Version}
            # 需要安装或更新的依赖项列表
            dependencies_to_install = []
            for pkg_name, version_specifier in plugin_dependencies.items():
                spec_set = SpecifierSet(version_specifier)
                installed_version = installed_packages.get(pkg_name)
                if installed_version is None:
                    # 包未安装，需要安装
                    if version_specifier:
                        dependencies_to_install.append(f"{pkg_name}{version_specifier}")
                    else:
                        dependencies_to_install.append(pkg_name)
                elif not spec_set.contains(installed_version, prereleases=True):
                    # 已安装的版本不满足版本约束，需要升级或降级
                    if version_specifier:
                        dependencies_to_install.append(f"{pkg_name}{version_specifier}")
                    else:
                        dependencies_to_install.append(pkg_name)
                # 已安装的版本满足要求，无需操作
            return dependencies_to_install
        except Exception as e:
            logger.error(f"收集所有需要安装或更新的依赖项时发生错误：{e}")
            return []

    def install_dependencies(self, dependencies: List[str]) -> Tuple[bool, str]:
        """
        安装指定的依赖项列表
        :param dependencies: 需要安装或更新的依赖项列表
        :return: (success, message)
        """
        if not dependencies:
            return False, "没有传入需要安装的依赖项"

        try:
            logger.debug(f"需要安装或更新的依赖项：{dependencies}")
            # 创建临时的 requirements.txt 文件用于批量安装
            requirements_temp_file = Path(settings.TEMP_PATH) / "plugin_dependencies" / "requirements.txt"
            requirements_temp_file.parent.mkdir(parents=True, exist_ok=True)
            with open(requirements_temp_file, "w", encoding="utf-8") as f:
                for dep in dependencies:
                    f.write(dep + "\n")
            try:
                # 使用自动降级策略安装依赖
                wheels_dirs = self.__collect_plugin_wheels_dirs()
                return self.pip_install_with_fallback(requirements_temp_file, wheels_dirs)
            finally:
                # 删除临时文件
                requirements_temp_file.unlink()
        except Exception as e:
            logger.error(f"安装依赖项时发生错误：{e}")
            return False, f"安装依赖项时发生错误：{e}"

    @classmethod
    def __get_installed_packages(cls) -> Dict[str, Version]:
        """
        获取已安装的包及其版本
        使用 importlib.metadata 获取当前环境中已安装的包，标准化包名并转换版本信息
        对于无法解析的版本，记录警告日志并跳过
        :return: 已安装包的字典，格式为 {package_name: Version}
        """
        installed_packages = {}
        try:
            for dist in distributions():
                name = dist.metadata.get("Name")
                if not name:
                    continue
                pkg_name = cls.__standardize_pkg_name(name)
                version_str = dist.metadata.get("Version") or getattr(dist, "version", None)
                if not version_str:
                    continue
                try:
                    v = Version(version_str)
                    if pkg_name not in installed_packages or v > installed_packages[pkg_name]:
                        installed_packages[pkg_name] = v
                except InvalidVersion:
                    logger.debug(f"无法解析已安装包 '{pkg_name}' 的版本：{version_str}")
                    continue
            return installed_packages
        except Exception as e:
            logger.error(f"获取已安装的包时发生错误：{e}")
            return {}

    def __find_plugin_dependencies(self) -> Dict[str, str]:
        """
        收集所有插件的依赖项
        遍历 plugins 目录下的所有插件，查找存在 requirements.txt 的插件目录
        ，并解析其中的依赖项，同时将所有插件的依赖项合并到字典中，方便后续统一处理
        :return: 依赖项字典，格式为 {package_name: set(version_specifiers)}
        """
        dependencies = {}
        try:
            install_plugins = {
                plugin_id.lower()  # 对应插件的小写目录名
                for plugin_id in SystemConfigOper().get(
                    SystemConfigKey.UserInstalledPlugins
                ) or []
            }
            for plugin_dir in PLUGIN_DIR.iterdir():
                if plugin_dir.is_dir():
                    requirements_file = plugin_dir / "requirements.txt"
                    if requirements_file.exists():
                        if plugin_dir.name not in install_plugins:
                            # 这个插件不在安装列表中 忽略它的依赖
                            logger.debug(f"忽略插件 {plugin_dir.name} 的依赖")
                            continue
                        # 解析当前插件的 requirements.txt，获取依赖项
                        plugin_deps = self.__parse_requirements(requirements_file)
                        for pkg_name, version_specifiers in plugin_deps.items():
                            if pkg_name in dependencies:
                                # 更新已存在的包的版本约束集合
                                dependencies[pkg_name].update(version_specifiers)
                            else:
                                # 添加新的包及其版本约束
                                dependencies[pkg_name] = set(version_specifiers)
            return self.__merge_dependencies(dependencies)
        except Exception as e:
            logger.error(f"收集插件依赖项时发生错误：{e}")
            return {}

    def __parse_requirements(self, requirements_file: Path) -> Dict[str, List[str]]:
        """
        解析 requirements.txt 文件，返回依赖项字典
        使用 packaging 库解析每一行依赖项，提取包名和版本约束
        对于无法解析的行，记录警告日志，便于后续检查
        :param requirements_file: requirements.txt 文件的路径
        :return: 依赖项字典，格式为 {package_name: [version_specifier]}
        """
        dependencies = {}
        try:
            with open(requirements_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # 使用 packaging 库解析依赖项
                        try:
                            req = Requirement(line)
                            pkg_name = self.__standardize_pkg_name(req.name)
                            version_specifier = str(req.specifier)
                            if pkg_name in dependencies:
                                dependencies[pkg_name].append(version_specifier)
                            else:
                                dependencies[pkg_name] = [version_specifier]
                        except Exception as e:
                            logger.debug(f"无法解析依赖项 '{line}'：{e}")
            return dependencies
        except Exception as e:
            logger.error(f"解析 requirements.txt 时发生错误：{e}")
            return {}

    @staticmethod
    def __merge_dependencies(dependencies: Dict[str, Set[str]]) -> Dict[str, str]:
        """
        合并依赖项，选择每个包的最高版本要求
        对于多个插件依赖同一包的情况，合并其版本约束，取交集以满足所有插件的要求
        如果交集为空，表示存在版本冲突，需要根据策略进行处理
        :param dependencies: 依赖项字典，格式为 {package_name: set(version_specifiers)}
        :return: 合并后的依赖项字典，格式为 {package_name: version_specifiers}
        """
        try:
            merged_dependencies = {}
            for pkg_name, version_specifiers in dependencies.items():
                # 合并版本约束
                spec_set = SpecifierSet()
                for specifier in version_specifiers:
                    try:
                        if specifier:
                            spec_set &= SpecifierSet(specifier)
                    except InvalidSpecifier as e:
                        logger.error(f"发生版本约束冲突：{e}")
                # 将合并后的版本约束添加到结果字典
                merged_dependencies[pkg_name] = str(spec_set) if spec_set else ''
            return merged_dependencies
        except Exception as e:
            logger.error(f"合并依赖项时发生错误：{e}")
            return {}

    @staticmethod
    def __standardize_pkg_name(name: str) -> str:
        """
        标准化包名，将包名转换为小写，连字符与点替换为下划线（与 PEP 503 归一化风格一致）

        :param name: 原始包名
        :return: 标准化后的包名
        """
        if not name:
            return name
        return name.lower().replace("-", "_").replace(".", "_")

    async def async_get_plugin_package_version(self, pid: str, repo_url: str,
                                               package_version: Optional[str] = None) -> Optional[str]:
        """
        异步版本的获取插件版本方法，功能同 get_plugin_package_version
        """
        if not package_version:
            package_version = settings.VERSION_FLAG

        if pid in (await self.async_get_plugins(repo_url, package_version) or []):
            return package_version

        plugin = (await self.async_get_plugins(repo_url) or {}).get(pid, None)
        if plugin and plugin.get(package_version) is True:
            return ""

        return None

    @staticmethod
    async def __async_request_with_fallback(url: str,
                                            headers: Optional[dict] = None,
                                            timeout: Optional[int] = 60,
                                            is_api: bool = False) -> Optional[httpx.Response]:
        """
        使用自动降级策略，异步请求资源，优先级依次为镜像站、代理、直连
        :param url: 目标URL
        :param headers: 请求头信息
        :param timeout: 请求超时时间
        :param is_api: 是否为GitHub API请求，API请求不走镜像站
        :return: 请求成功则返回 Response，失败返回 None
        """
        strategies = []

        # 1. 尝试使用镜像站，镜像站一般不支持API请求，因此API请求直接跳过镜像站
        if not is_api and settings.GITHUB_PROXY:
            proxy_url = f"{UrlUtils.standardize_base_url(settings.GITHUB_PROXY)}{url}"
            strategies.append(("镜像站", proxy_url, {"headers": headers, "timeout": timeout}))

        # 2. 尝试使用代理
        if settings.PROXY_HOST:
            strategies.append(("代理", url, {"headers": headers, "proxies": settings.PROXY, "timeout": timeout}))

        # 3. 最后尝试直连
        strategies.append(("直连", url, {"headers": headers, "timeout": timeout}))

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

    @cached(maxsize=128, ttl=1800)
    async def async_get_plugins(self, repo_url: str,
                                package_version: Optional[str] = None) -> Optional[Dict[str, dict]]:
        """
        异步获取Github所有最新插件列表
        :param repo_url: Github仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        """
        if not repo_url:
            return None

        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return None

        raw_url = self._base_url.format(user=user, repo=repo)
        package_url = f"{raw_url}package.{package_version}.json" if package_version else f"{raw_url}package.json"
        package_url = self.__append_cache_buster(package_url)

        res = await self.__async_request_with_fallback(package_url,
                                                       headers=settings.REPO_GITHUB_HEADERS(repo=f"{user}/{repo}"))
        if res is None:
            return None
        if res.status_code == 404:
            return {}
        if res.status_code != 200:
            return None
        return self.__parse_plugin_index_response(res.text)

    @cached(maxsize=128, ttl=1800)
    async def async_get_plugin_release_versions(self, pid: str, repo_url: str) -> List[dict]:
        """
        异步获取插件可安装的 GitHub Release 版本列表。
        """
        if not pid or not repo_url:
            return []

        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return []

        user_repo = f"{user}/{repo}"
        releases = []
        for page in range(1, 11):
            release_api = f"https://api.github.com/repos/{user_repo}/releases?per_page=100&page={page}"
            release_api = self.__append_cache_buster(release_api)
            res = await self.__async_request_with_fallback(
                release_api,
                headers=settings.REPO_GITHUB_HEADERS(repo=user_repo),
                timeout=30,
                is_api=True,
            )
            if res is None or res.status_code != 200:
                break

            try:
                payload = res.json()
                if not payload:
                    break
                releases.extend(self.__parse_plugin_release_response(pid, payload))
                if len(payload) < 100:
                    break
            except Exception as e:
                logger.error(f"解析插件 {pid} Release 列表失败：{e}")
                break
        return releases

    async def __async_get_file_list(self, pid: str, user_repo: str, package_version: Optional[str] = None) -> \
            Tuple[Optional[list], Optional[str]]:
        """
        异步获取插件的文件列表
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (文件列表, 错误信息)
        """
        file_api = f"https://api.github.com/repos/{user_repo}/contents/plugins"
        # 如果 package_version 存在（如 "v2"），则加上版本号
        if package_version:
            file_api += f".{package_version}"
        file_api += f"/{pid.lower()}"

        res = await self.__async_request_with_fallback(file_api,
                                                       headers=settings.REPO_GITHUB_HEADERS(repo=user_repo),
                                                       is_api=True,
                                                       timeout=30)
        if res is None:
            return None, "连接仓库失败"
        elif res.status_code != 200:
            return None, f"连接仓库失败：{res.status_code} - " \
                         f"{'超出速率限制，请设置Github Token或稍后重试' if res.status_code == 403 else res.text}"

        try:
            ret = res.json()
            if isinstance(ret, list) and len(ret) > 0 and "message" not in ret[0]:
                return ret, ""
            else:
                return None, "插件在仓库中不存在或返回数据格式不正确"
        except Exception as e:
            logger.error(f"插件数据解析失败：{e}")
            return None, "插件数据解析失败"

    async def __async_download_files(self, pid: str, file_list: List[dict], user_repo: str,
                                     package_version: Optional[str] = None,
                                     skip_requirements: bool = False) -> Tuple[bool, str]:
        """
        异步下载插件文件
        :param pid: 插件 ID
        :param file_list: 要下载的文件列表，包含文件的元数据（包括下载链接）
        :param user_repo: GitHub 仓库的 user/repo 路径
        :param skip_requirements: 是否跳过 requirements.txt 文件的下载
        :return: (是否成功, 错误信息)
        """
        if not file_list:
            return False, "文件列表为空"

        # 使用栈结构来替代递归调用，避免递归深度过大问题
        stack = [(pid, file_list)]

        while stack:
            current_pid, current_file_list = stack.pop()

            for item in current_file_list:
                # 跳过 requirements.txt 的下载
                if skip_requirements and item.get("name") == "requirements.txt":
                    continue

                if item.get("download_url"):
                    logger.debug(f"正在下载文件：{item.get('path')}")
                    res = await self.__async_request_with_fallback(item.get('download_url'),
                                                                   headers=settings.REPO_GITHUB_HEADERS(repo=user_repo))
                    if not res:
                        return False, f"文件 {item.get('path')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('path')} 失败：{res.status_code}"

                    # 确保文件路径不包含版本号（如 v2、v3），如果有 package_version，移除路径中的版本号
                    relative_path = item.get("path")
                    if package_version:
                        relative_path = relative_path.replace(f"plugins.{package_version}", "plugins", 1)

                    # 创建插件文件夹并写入文件
                    file_path = AsyncPath(settings.ROOT_PATH) / "app" / relative_path
                    await file_path.parent.mkdir(parents=True, exist_ok=True)
                    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                        await f.write(res.text)
                    logger.debug(f"文件 {item.get('path')} 下载成功，保存路径：{file_path}")
                else:
                    # 如果是子目录，则将子目录内容加入栈中继续处理
                    sub_list, msg = await self.__async_get_file_list(f"{current_pid}/{item.get('name')}", user_repo,
                                                                     package_version)
                    if not sub_list:
                        return False, msg
                    stack.append((f"{current_pid}/{item.get('name')}", sub_list))

        return True, ""

    async def __async_download_and_install_requirements(self, requirements_file_info: dict, pid: str, user_repo: str) \
            -> Tuple[bool, str]:
        """
        异步下载并安装 requirements.txt 文件中的依赖
        :param requirements_file_info: requirements.txt 文件的元数据信息
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (是否成功, 错误信息)
        """
        # 下载 requirements.txt
        res = await self.__async_request_with_fallback(requirements_file_info.get("download_url"),
                                                       headers=settings.REPO_GITHUB_HEADERS(repo=user_repo))
        if not res:
            return False, "requirements.txt 文件下载失败"
        elif res.status_code != 200:
            return False, f"下载 requirements.txt 文件失败：{res.status_code}"

        requirements_txt = res.text
        if requirements_txt.strip():
            # 保存并安装依赖
            requirements_file_path = AsyncPath(PLUGIN_DIR) / pid.lower() / "requirements.txt"
            await requirements_file_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(requirements_file_path, "w", encoding="utf-8") as f:
                await f.write(requirements_txt)

            return await self.__async_pip_install_with_fallback(Path(requirements_file_path))

        return True, ""  # 如果 requirements.txt 为空，视作成功

    async def __async_pip_install_with_fallback(
            self,
            requirements_file: Path,
            find_links_dirs: Optional[List[Path]] = None) -> Tuple[bool, str]:
        """
        在线程池中执行插件依赖安装，避免同步 pip 子进程阻塞事件循环。
        """
        return await asyncio.to_thread(
            self.pip_install_with_fallback,
            requirements_file,
            find_links_dirs
        )

    async def __async_backup_plugin(self, pid: str) -> str:
        """
        异步备份旧插件目录
        :param pid: 插件 ID
        :return: 备份目录路径
        """
        plugin_dir = AsyncPath(PLUGIN_DIR) / pid.lower()
        backup_dir = AsyncPath(settings.TEMP_PATH) / "plugin_backup" / pid.lower()

        if await plugin_dir.exists():
            # 备份时清理已有的备份目录，防止残留文件影响
            if await backup_dir.exists():
                await aioshutil.rmtree(backup_dir, ignore_errors=True)
                logger.debug(f"{pid} 旧的备份目录已清理 {backup_dir}")

            # 异步复制目录
            await self._async_copytree(plugin_dir, backup_dir)
            logger.debug(f"{pid} 插件已备份到 {backup_dir}")

        return str(backup_dir) if await backup_dir.exists() else None

    async def __async_restore_plugin(self, pid: str, backup_dir: str):
        """
        异步还原旧插件目录
        :param pid: 插件 ID
        :param backup_dir: 备份目录路径
        """
        plugin_dir = AsyncPath(PLUGIN_DIR) / pid.lower()
        if await plugin_dir.exists():
            await aioshutil.rmtree(plugin_dir, ignore_errors=True)
            logger.debug(f"{pid} 已清理插件目录 {plugin_dir}")

        backup_path = AsyncPath(backup_dir)
        if await backup_path.exists():
            await self._async_copytree(src=backup_path, dst=plugin_dir)
            logger.debug(f"{pid} 已还原插件目录 {plugin_dir}")
            await aioshutil.rmtree(backup_path, ignore_errors=True)
            logger.debug(f"{pid} 已删除备份目录 {backup_dir}")

    @staticmethod
    async def __async_remove_old_plugin(pid: str):
        """
        异步删除旧插件
        :param pid: 插件 ID
        """
        plugin_dir = AsyncPath(PLUGIN_DIR) / pid.lower()
        if await plugin_dir.exists():
            await aioshutil.rmtree(plugin_dir, ignore_errors=True)

    async def _async_copytree(self, src: AsyncPath, dst: AsyncPath):
        """
        异步递归复制目录
        :param src: 源目录
        :param dst: 目标目录
        """
        if not await src.exists():
            return

        await dst.mkdir(parents=True, exist_ok=True)

        async for item in src.iterdir():
            dst_item = dst / item.name
            if await item.is_dir():
                await self._async_copytree(item, dst_item)
            else:
                async with aiofiles.open(item, 'rb') as src_file:
                    content = await src_file.read()
                async with aiofiles.open(dst_item, 'wb') as dst_file:
                    await dst_file.write(content)

    async def __async_install_dependencies_if_required(self, pid: str) -> Tuple[bool, bool, str]:
        """
        异步安装插件依赖。
        :param pid: 插件 ID
        :return: (是否存在依赖，安装是否成功, 错误信息)
        """
        # 定位插件目录和依赖文件
        plugin_dir = AsyncPath(PLUGIN_DIR) / pid.lower()
        requirements_file = plugin_dir / "requirements.txt"

        # 检查是否存在 requirements.txt 文件
        if await requirements_file.exists():
            logger.info(f"{pid} 存在依赖，开始尝试安装依赖")
            success, error_message = await self.__async_pip_install_with_fallback(Path(requirements_file))
            if success:
                return True, True, ""
            else:
                return True, False, error_message

        return False, False, "不存在依赖"

    async def async_install_dependencies(self, dependencies: List[str]) -> Tuple[bool, str]:
        """
        异步安装指定的依赖项列表
        :param dependencies: 需要安装或更新的依赖项列表
        :return: (success, message)
        """
        if not dependencies:
            return False, "没有传入需要安装的依赖项"

        try:
            logger.debug(f"需要安装或更新的依赖项：{dependencies}")
            # 创建临时的 requirements.txt 文件用于批量安装
            requirements_temp_file = AsyncPath(settings.TEMP_PATH) / "plugin_dependencies" / "requirements.txt"
            await requirements_temp_file.parent.mkdir(parents=True, exist_ok=True)

            async with aiofiles.open(requirements_temp_file, "w", encoding="utf-8") as f:
                for dep in dependencies:
                    await f.write(dep + "\n")

            try:
                # 使用自动降级策略安装依赖
                wheels_dirs = self.__collect_plugin_wheels_dirs()
                return await self.__async_pip_install_with_fallback(Path(requirements_temp_file), wheels_dirs)
            finally:
                # 删除临时文件
                await requirements_temp_file.unlink()
        except Exception as e:
            logger.error(f"安装依赖项时发生错误：{e}")
            return False, f"安装依赖项时发生错误：{e}"

    async def __async_find_plugin_dependencies(self) -> Dict[str, str]:
        """
        异步收集所有插件的依赖项
        遍历 plugins 目录下的所有插件，查找存在 requirements.txt 的插件目录
        ，并解析其中的依赖项，同时将所有插件的依赖项合并到字典中，方便后续统一处理
        :return: 依赖项字典，格式为 {package_name: set(version_specifiers)}
        """
        dependencies = {}
        try:
            install_plugins = {
                plugin_id.lower()  # 对应插件的小写目录名
                for plugin_id in SystemConfigOper().get(
                    SystemConfigKey.UserInstalledPlugins
                ) or []
            }

            plugin_dir_path = AsyncPath(PLUGIN_DIR)
            async for plugin_dir in plugin_dir_path.iterdir():
                if await plugin_dir.is_dir():
                    requirements_file = plugin_dir / "requirements.txt"
                    if await requirements_file.exists():
                        if plugin_dir.name not in install_plugins:
                            # 这个插件不在安装列表中 忽略它的依赖
                            logger.debug(f"忽略插件 {plugin_dir.name} 的依赖")
                            continue
                        # 解析当前插件的 requirements.txt，获取依赖项
                        plugin_deps = await self.__async_parse_requirements(requirements_file)
                        for pkg_name, version_specifiers in plugin_deps.items():
                            if pkg_name in dependencies:
                                # 更新已存在的包的版本约束集合
                                dependencies[pkg_name].update(version_specifiers)
                            else:
                                # 添加新的包及其版本约束
                                dependencies[pkg_name] = set(version_specifiers)
            return self.__merge_dependencies(dependencies)
        except Exception as e:
            logger.error(f"收集插件依赖项时发生错误：{e}")
            return {}

    async def __async_parse_requirements(self, requirements_file: AsyncPath) -> Dict[str, List[str]]:
        """
        异步解析 requirements.txt 文件，返回依赖项字典
        使用 packaging 库解析每一行依赖项，提取包名和版本约束
        对于无法解析的行，记录警告日志，便于后续检查
        :param requirements_file: requirements.txt 文件的路径
        :return: 依赖项字典，格式为 {package_name: [version_specifier]}
        """
        dependencies = {}
        try:
            async with aiofiles.open(requirements_file, "r", encoding="utf-8") as f:
                async for line in f:
                    line = str(line).strip()
                    if line and not line.startswith('#'):
                        # 使用 packaging 库解析依赖项
                        try:
                            req = Requirement(line)
                            pkg_name = self.__standardize_pkg_name(req.name)
                            version_specifier = str(req.specifier)
                            if pkg_name in dependencies:
                                dependencies[pkg_name].append(version_specifier)
                            else:
                                dependencies[pkg_name] = [version_specifier]
                        except Exception as e:
                            logger.debug(f"无法解析依赖项 '{line}'：{e}")
            return dependencies
        except Exception as e:
            logger.error(f"解析 requirements.txt 时发生错误：{e}")
            return {}

    async def async_find_missing_dependencies(self) -> List[str]:
        """
        异步收集所有需要安装或更新的依赖项
        1. 收集所有插件的依赖项，合并版本约束
        2. 获取已安装的包及其版本
        3. 比较已安装的包与所需的依赖项，找出需要安装或升级的包
        :return: 需要安装或更新的依赖项列表，例如 ["package1>=1.0.0", "package2"]
        """
        try:
            # 收集所有插件的依赖项
            plugin_dependencies = await self.__async_find_plugin_dependencies()  # 返回格式为 {package_name: version_specifier}
            # 获取已安装的包及其版本
            installed_packages = self.__get_installed_packages()  # 返回格式为 {package_name: Version}
            # 需要安装或更新的依赖项列表
            dependencies_to_install = []
            for pkg_name, version_specifier in plugin_dependencies.items():
                spec_set = SpecifierSet(version_specifier)
                installed_version = installed_packages.get(pkg_name)
                if installed_version is None:
                    # 包未安装，需要安装
                    if version_specifier:
                        dependencies_to_install.append(f"{pkg_name}{version_specifier}")
                    else:
                        dependencies_to_install.append(pkg_name)
                elif not spec_set.contains(installed_version, prereleases=True):
                    # 已安装的版本不满足版本约束，需要升级或降级
                    if version_specifier:
                        dependencies_to_install.append(f"{pkg_name}{version_specifier}")
                    else:
                        dependencies_to_install.append(pkg_name)
                # 已安装的版本满足要求，无需操作
            return dependencies_to_install
        except Exception as e:
            logger.error(f"收集所有需要安装或更新的依赖项时发生错误：{e}")
            return []

    async def async_install(self, pid: str, repo_url: str, package_version: Optional[str] = None,
                            release_version: Optional[str] = None,
                            force_install: bool = False) -> Tuple[bool, str]:
        """
        异步安装插件，包括依赖安装和文件下载，相关资源支持自动降级策略
        1. 检查并获取插件的指定版本，确认版本兼容性
        2. 从 GitHub 获取文件列表（包括 requirements.txt）
        3. 删除旧的插件目录（如非强制安装则进行备份）
        4. 下载并预安装 requirements.txt 中的依赖（如果存在）
        5. 下载并安装插件的其他文件
        6. 再次尝试安装依赖（确保安装完整）
        :param pid: 插件 ID
        :param repo_url: 插件仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如不指定则默认使用系统配置的版本
        :param release_version: 指定安装的 release 资产版本；未指定时安装当前索引版本
        :param force_install: 是否强制安装插件，默认不启用，启用时不进行备份和恢复操作
        :return: (是否成功, 错误信息)
        """
        if self.is_local_repo_url(repo_url):
            return await asyncio.to_thread(self.install_local, pid, repo_url, force_install)

        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"

        # 验证参数
        if not pid or not repo_url:
            return False, "参数错误"

        # 从 GitHub 的 repo_url 获取用户和项目名
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"

        user_repo = f"{user}/{repo}"

        if not package_version:
            package_version = settings.VERSION_FLAG

        # 1. 优先检查指定版本的插件
        package_version = await self.async_get_plugin_package_version(pid, repo_url, package_version)
        # 如果 package_version 为None，说明没有找到匹配的插件
        if package_version is None:
            msg = f"{pid} 没有找到适用于当前版本的插件"
            logger.debug(msg)
            return False, msg
        # package_version 为空，表示从 package.json 中找到插件
        elif package_version == "":
            logger.debug(f"{pid} 从 package.json 中找到适用于当前版本的插件")
        else:
            logger.debug(f"{pid} 从 package.{package_version}.json 中找到适用于当前版本的插件")

        # 2. 统一异步安装流程（release 或文件列表）。
        meta = await self.__async_get_plugin_meta(pid, repo_url, package_version)
        # 是否使用 Release 打包；失败时兜底文件列表，保持同步/异步安装语义一致。
        is_release = meta.get("release")
        # 插件版本号
        plugin_version = meta.get("version")
        if release_version:
            if not is_release:
                return False, f"{pid} 未声明 Release 安装，无法安装指定版本"
            release_items = await self.async_get_plugin_release_versions(pid, repo_url)
            if not self.__has_installable_release_version(release_items, release_version):
                return False, f"{pid} 未找到可安装的 Release 版本：{release_version}"
            if release_version == plugin_version:
                compatible, message = self.check_plugin_system_version(meta)
                if not compatible:
                    logger.debug(f"{pid} 插件系统版本兼容性检查失败：{message}")
                    return False, message
            release_tag = f"{pid}_v{release_version}"

            async def prepare_selected_release() -> Tuple[bool, str]:
                return await self.__async_install_from_release(pid, user_repo, release_tag)

            return await self.__install_flow_async(pid, force_install, prepare_selected_release, repo_url)

        compatible, message = self.check_plugin_system_version(meta)
        if not compatible:
            logger.debug(f"{pid} 插件系统版本兼容性检查失败：{message}")
            return False, message
        if is_release:
            # 使用 插件ID_插件版本号 作为 Release tag
            if not plugin_version:
                return False, f"未在插件清单中找到 {pid} 的版本号，无法进行 Release 安装"
            # 拼接 release_tag
            release_tag = f"{pid}_v{plugin_version}"

            # 使用 release 进行安装
            async def prepare_release() -> Tuple[bool, str]:
                ok, msg = await self.__async_install_from_release(pid, user_repo, release_tag)
                if ok:
                    return True, msg
                logger.warning(f"{pid} Release 安装失败，回退文件列表安装：{msg}")
                await self.__async_remove_old_plugin(pid)
                return await self.__prepare_content_via_filelist_async(pid.lower(), user_repo, package_version)

            return await self.__install_flow_async(pid, force_install, prepare_release, repo_url)
        else:
            # 未声明 release 打包的插件继续使用文件列表方式安装。
            async def prepare_filelist() -> Tuple[bool, str]:
                return await self.__prepare_content_via_filelist_async(pid.lower(), user_repo, package_version)

            return await self.__install_flow_async(pid, force_install, prepare_filelist, repo_url)

    async def __async_get_plugin_meta(self, pid: str, repo_url: str,
                                      package_version: Optional[str]) -> dict:
        try:
            plugins = (
                          await self.async_get_plugins(repo_url) if not package_version
                          else await self.async_get_plugins(repo_url, package_version)
                      ) or {}
            meta = plugins.get(pid)
            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            logger.warn(f"获取插件 {pid} 元数据失败：{e}")
            return {}

    async def __install_flow_async(self, pid: str, force_install: bool,
                                   prepare_content: Callable[[], Awaitable[Tuple[bool, str]]],
                                   repo_url: Optional[str] = None) -> Tuple[bool, str]:
        """
        异步安装流程，处理插件内容准备、依赖安装和注册
        """
        backup_dir = None
        if not force_install:
            backup_dir = await self.__async_backup_plugin(pid)

        await self.__async_remove_old_plugin(pid)

        success, message = await prepare_content()
        if not success:
            logger.error(f"{pid} 准备插件内容失败：{message}")
            if backup_dir:
                await self.__async_restore_plugin(pid, backup_dir)
                logger.warn(f"{pid} 插件安装失败，已还原备份插件")
            else:
                await self.__async_remove_old_plugin(pid)
                logger.warn(f"{pid} 已清理对应插件目录，请尝试重新安装")
            return False, message

        dependencies_exist, dep_ok, dep_msg = await self.__async_install_dependencies_if_required(pid)
        if dependencies_exist and not dep_ok:
            logger.error(f"{pid} 依赖安装失败：{dep_msg}")
            if backup_dir:
                await self.__async_restore_plugin(pid, backup_dir)
                logger.warn(f"{pid} 插件安装失败，已还原备份插件")
            else:
                await self.__async_remove_old_plugin(pid)
                logger.warn(f"{pid} 已清理对应插件目录，请尝试重新安装")
            return False, dep_msg

        await asyncio.to_thread(self.refresh_persistent_plugin_backup, pid)
        return True, ""

    def __prepare_content_via_filelist_sync(self, pid: str, user_repo: str,
                                            package_version: Optional[str]) -> Tuple[bool, str]:
        """
        同步准备插件内容，通过文件列表获取插件文件和依赖
        """
        file_list, msg = self.__get_file_list(pid, user_repo, package_version)
        if not file_list:
            return False, msg
        requirements_file_info = next((f for f in file_list if f.get("name") == "requirements.txt"), None)
        if requirements_file_info:
            ok, m = self.__download_and_install_requirements(requirements_file_info, pid, user_repo)
            if not ok:
                logger.debug(f"{pid} 依赖预安装失败：{m}")
            else:
                logger.debug(f"{pid} 依赖预安装成功")
        ok, m = self.__download_files(pid, file_list, user_repo, package_version, True)
        if not ok:
            return False, m
        return True, ""

    async def __prepare_content_via_filelist_async(self, pid: str, user_repo: str,
                                                   package_version: Optional[str]) -> Tuple[bool, str]:
        """
        异步准备插件内容，通过文件列表获取插件文件和依赖
        """
        file_list, msg = await self.__async_get_file_list(pid, user_repo, package_version)
        if not file_list:
            return False, msg
        requirements_file_info = next((f for f in file_list if f.get("name") == "requirements.txt"), None)
        if requirements_file_info:
            ok, m = await self.__async_download_and_install_requirements(requirements_file_info, pid, user_repo)
            if not ok:
                logger.debug(f"{pid} 依赖预安装失败：{m}")
            else:
                logger.debug(f"{pid} 依赖预安装成功")
        ok, m = await self.__async_download_files(pid, file_list, user_repo, package_version, True)
        if not ok:
            return False, m
        return True, ""

    async def __async_install_from_release(self, pid: str, user_repo: str, release_tag: str) -> Tuple[bool, str]:
        """
        通过 GitHub Release 资产文件安装插件（异步）。
        规范：release 中存在名为 "{pid}_v{version}.zip" 的资产，zip 根即插件文件；
        将其全部解压到 app/plugins/{pid}
        """
        # 拼接资产文件名
        asset_name = f"{release_tag.lower()}.zip"

        release_api = f"https://api.github.com/repos/{user_repo}/releases/tags/{release_tag}"
        rel_res = await self.__async_request_with_fallback(
            release_api,
            headers=settings.REPO_GITHUB_HEADERS(repo=user_repo),
            timeout=30,
            is_api=True,
        )
        if rel_res is None or rel_res.status_code != 200:
            return False, f"获取 Release 信息失败：{rel_res.status_code if rel_res else '连接失败'}"

        try:
            rel_json = rel_res.json()
            assets = rel_json.get("assets") or []
            asset = next((a for a in assets if a.get("name") == asset_name), None)
            if not asset:
                return False, f"未找到资产文件：{asset_name}"
            asset_id = asset.get("id")
            if not asset_id:
                return False, "资产缺少ID信息"
            # 构建资产的API下载URL
            download_url = f"https://api.github.com/repos/{user_repo}/releases/assets/{asset_id}"
        except Exception as e:
            logger.error(f"解析 Release 信息失败：{e}")
            return False, f"解析 Release 信息失败：{e}"

        # 使用资产的API端点下载，需要设置Accept头为application/octet-stream
        headers = settings.REPO_GITHUB_HEADERS(repo=user_repo).copy()
        headers["Accept"] = "application/octet-stream"
        res = await self.__async_request_with_fallback(download_url,
                                                       headers=headers,
                                                       is_api=True)
        if res is None or res.status_code != 200:
            return False, f"下载资产失败：{res.status_code if res else '连接失败'}"

        try:
            with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
                namelist = zf.namelist()
                if not namelist:
                    return False, "压缩包内容为空"
                names_with_slash = [n for n in namelist if '/' in n]
                base_prefix = ''
                if names_with_slash and len(names_with_slash) == len(namelist):
                    first_seg = names_with_slash[0].split('/')[0]
                    if all(n.startswith(first_seg + '/') for n in namelist):
                        base_prefix = first_seg + '/'

                dest_base = AsyncPath(settings.ROOT_PATH) / "app" / "plugins" / pid.lower()
                wrote_any = False
                for name in namelist:
                    rel_path = name[len(base_prefix):]
                    if not rel_path:
                        continue
                    if rel_path.endswith('/'):
                        await (dest_base / rel_path.rstrip('/')).mkdir(parents=True, exist_ok=True)
                        continue
                    dest_path = dest_base / rel_path
                    await dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name, 'r') as src:
                        data = src.read()
                    async with aiofiles.open(dest_path, 'wb') as dst:
                        await dst.write(data)
                    wrote_any = True
                if not wrote_any:
                    return False, "压缩包中无可写入文件"
            return True, ""
        except Exception as e:
            logger.error(f"解压 Release 压缩包失败：{e}")
            return False, f"解压 Release 压缩包失败：{e}"
