"""
站点认证与索引资源的版本检查和离线下载适配器。
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import sysconfig
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

from app.adapters.network.http import (
    HttpConnectionError,
    HttpRequestError,
    HttpResponseError,
    HttpStreamError,
    HttpTimeoutError,
    RequestUtils,
)
from app.adapters.system.host import SystemUtils
from app.foundation.url import UrlUtils, mask_url_credentials
from app.foundation.version import compare_version
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

if TYPE_CHECKING:
    from requests import Response

ResourceVersionProvider = Callable[[], tuple[str, str]]
_RequestResult = TypeVar("_RequestResult")
# 仅重试幂等读取的暂时性 HTTP 故障；404 等永久错误应直接反馈。
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _unavailable_resource_versions() -> tuple[str, str]:
    """
    站点能力尚未装配时返回可安全比较的空版本。
    """
    return "0", "0"


_resource_version_provider: ResourceVersionProvider = _unavailable_resource_versions


def configure_resource_version_provider(provider: ResourceVersionProvider) -> None:
    """
    由组合根注入已加载的认证与索引版本，保持适配器不依赖应用层。
    """
    global _resource_version_provider
    _resource_version_provider = provider


def reset_resource_version_provider() -> None:
    """
    撤销当前 lifespan 的资源版本来源，恢复安全空版本。
    """
    global _resource_version_provider
    _resource_version_provider = _unavailable_resource_versions


def get_resource_versions() -> tuple[str, str]:
    """
    读取当前进程已加载的认证资源版本和索引资源版本。
    """
    try:
        return _resource_version_provider()
    # 版本读取失败时不能阻断系统状态接口
    except Exception as error:  # noqa: BLE001
        logger.warning(f"读取站点资源版本失败：{error}")
        return _unavailable_resource_versions()


class ResourceHelper:
    """
    检查站点资源版本，并把待更新文件下载到外部暂存目录。
    """

    _base_dir: Path = get_runtime_setting("ROOT_PATH")
    _resource_target = Path("app/application/site")
    _version_flag = get_runtime_setting("RESOURCE_VERSION_FLAG")
    _repo = (
        "https://raw.githubusercontent.com/"
        f"jxxghp/MoviePilot-Resources/main/package.{_version_flag}.json"
    )
    _files_api = (
        "https://api.github.com/repos/jxxghp/"
        f"MoviePilot-Resources/contents/resources.{_version_flag}"
    )

    @property
    def proxies(self) -> Optional[dict[str, str]]:
        """
        仅在存在非空镜像前缀时绕过全局代理，保持空白配置与直连地址一致。
        """
        proxies: Optional[dict[str, str]] = (
            None
            if str(get_runtime_setting("GITHUB_PROXY") or "").strip()
            else get_runtime_setting("PROXY")
        )
        return proxies

    @staticmethod
    def _get_python_version_tag() -> str:
        """
        返回资源文件名使用的 CPython ABI 标签。
        """
        version = sys.version_info
        free_threaded = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
        return f"cp{version.major}{version.minor}{free_threaded}"

    @staticmethod
    def _get_machine_tag() -> str:
        """
        将系统架构名称归一为资源文件使用的标签。
        """
        machine = platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            return "aarch64"
        if machine in {"x86_64", "amd64"}:
            return "x86_64"
        return machine

    @classmethod
    def _get_needed_files(cls) -> list[str]:
        """
        返回 V3 资源在当前平台需要下载的文件名。
        """
        python_version = cls._get_python_version_tag()
        python_ver = python_version.replace("cp", "")
        system = platform.system().lower()
        machine = cls._get_machine_tag()
        files = [f"user.sites.{cls._version_flag}.bin"]
        if system == "linux":
            files.append(f"sites.cpython-{python_ver}-{machine}-linux-gnu.so")
        elif system == "darwin":
            files.append(f"sites.cpython-{python_ver}-darwin.so")
        elif system == "windows":
            files.append(f"sites.cp{python_ver}-win_amd64.pyd")
        return files

    def _load_resource_info(self) -> Optional[Response]:
        """
        按请求时的代理配置读取 V3 清单，暂时性网络失败最多尝试三次。

        :raises RuntimeError: 请求失败，包含阶段、HTTP 状态或底层网络原因
        """
        return self._request_with_retry(
            lambda: self._get_response(self._proxied(self._repo), self.proxies, 10),
            "读取站点资源清单",
        )

    @staticmethod
    def _check_response(response: Optional[Response]) -> Response:
        """
        保留 HTTP 状态及无响应原因，供资源请求统一决定是否重试。
        """
        if response is None:
            raise HttpConnectionError("未收到响应")
        if response.status_code != 200:
            raise HttpResponseError(f"HTTP {response.status_code}", response=response)
        return response

    def _get_response(self, url: str, proxies: Optional[dict[str, str]], timeout: int) -> Response:
        """
        读取完整响应并保留传输异常；非成功响应在重试前释放。
        """
        response = RequestUtils(
            proxies=proxies,
            headers=get_runtime_setting("GITHUB_HEADERS"),
            timeout=timeout,
        ).get_res(url, raise_exception=True)
        try:
            return self._check_response(response)
        except HttpResponseError:
            if response is not None:
                response.close()
            raise

    @staticmethod
    def _request_with_retry(action: Callable[[], _RequestResult], label: str) -> _RequestResult:
        """
        对资源幂等请求作三次有界尝试，间隔一秒、两秒，不重试解析或磁盘错误。

        :param action: 单次请求，流式文件请求须自行回滚失败进度及临时内容
        :param label: 展示给用户的请求阶段，不包含凭据
        :raises RuntimeError: 永久故障或重试耗尽，保留脱敏后的原始原因
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return action()
            except HttpRequestError as error:
                retryable = isinstance(error, (HttpConnectionError, HttpTimeoutError, HttpStreamError)) or (
                    isinstance(error, HttpResponseError)
                    and error.response is not None
                    and error.response.status_code in _RETRYABLE_HTTP_STATUSES
                )
                detail = f"{type(error).__name__}：{mask_url_credentials(str(error))}"
                if not retryable or attempt >= 3:
                    raise RuntimeError(f"{label}失败（{attempt} 次请求）：{detail}") from error
                logger.warning(f"{label}失败，准备第 {attempt + 1}/3 次请求：{detail}")
                time.sleep(2 ** (attempt - 1))

    def get_update_info(
        self,
        *,
        auth_version: str | None = None,
        indexer_version: str | None = None,
    ) -> dict[str, Any] | None:
        """
        读取当前平台的资源更新元数据，但不写入运行目录。

        :param auth_version: 当前认证资源版本；省略时使用组合根注入值
        :param indexer_version: 当前索引资源版本；省略时使用组合根注入值
        :return: 有更新时返回资源版本和文件元数据，否则返回 None
        """
        if not get_runtime_setting("AUTO_UPDATE_RESOURCE") or SystemUtils.is_frozen():
            return None
        if auth_version is None or indexer_version is None:
            configured_auth, configured_indexer = get_resource_versions()
            auth_version = auth_version or configured_auth
            indexer_version = indexer_version or configured_indexer

        response = self._load_resource_info()
        if response is None:
            raise RuntimeError("无法连接资源包仓库")
        try:
            resource_info = json.loads(response.text)
        except json.JSONDecodeError as error:
            raise RuntimeError("资源包仓库数据解析失败") from error
        finally:
            response.close()

        if not isinstance(resource_info, dict):
            raise RuntimeError("资源包仓库数据格式异常")
        resources = resource_info.get("resources") or {}
        needed_files = self._get_needed_files()
        selected: dict[str, dict[str, Any]] = {}
        changed_types: set[str] = set()
        target_versions = {"auth": auth_version, "indexer": indexer_version}

        for resource_name in needed_files:
            resource = resources.get(resource_name)
            if not isinstance(resource, dict):
                raise RuntimeError(f"资源包清单缺少当前平台文件：{resource_name}")
            resource_type = resource.get("type")
            update_type = "indexer" if resource_type == "sites" else resource_type
            if update_type not in target_versions:
                raise RuntimeError(f"资源包清单包含未知资源类型：{resource_name}")
            declared_platform = resource.get("platform")
            if declared_platform and declared_platform != SystemUtils.platform():
                raise RuntimeError(f"资源包平台不匹配：{resource_name}")
            if Path(str(resource.get("target") or "")) != self._resource_target:
                raise RuntimeError(f"资源包目标目录不安全：{resource_name}")
            version = str(resource.get("version") or "").strip()
            if not version:
                raise RuntimeError(f"资源包清单缺少版本号：{resource_name}")
            local_version = str(target_versions[update_type] or "0")
            if compare_version(version, ">", local_version) is True:
                changed_types.add(update_type)
            selected[resource_name] = {
                "name": resource_name,
                "type": update_type,
                "version": version,
                "target": str(self._resource_target),
            }

        if not changed_types:
            logger.info("所有站点资源已最新，无需更新")
            return None

        logger.info(
            "发现站点资源更新：认证=%s，索引=%s",
            selected.get(needed_files[-1], {}).get("version"),
            selected.get(needed_files[0], {}).get("version"),
        )
        auth_target = next(
            (item["version"] for item in selected.values() if item["type"] == "auth"),
            None,
        )
        indexer_target = next(
            (item["version"] for item in selected.values() if item["type"] == "indexer"),
            None,
        )
        return {
            "package_version": str(resource_info.get("version") or ""),
            "auth_version": auth_target if "auth" in changed_types else None,
            "indexer_version": indexer_target if "indexer" in changed_types else None,
            "files": list(selected.values()),
        }

    def get_download_files(self, update_info: dict[str, Any]) -> list[dict[str, Any]]:
        """
        有限重试资源目录 API，补齐待下载地址和大小；API 沿用全局代理。
        """
        response = self._request_with_retry(
            lambda: self._get_response(self._files_api, get_runtime_setting("PROXY"), 30),
            "读取站点资源目录",
        )
        try:
            remote_files = response.json()
        finally:
            response.close()
        files_by_name = {
            item.get("name"): item
            for item in remote_files
            if isinstance(item, dict) and item.get("name")
        }
        download_files: list[dict[str, Any]] = []
        for resource in update_info.get("files") or []:
            name = str(resource.get("name") or "")
            remote = files_by_name.get(name) or {}
            if not remote.get("download_url"):
                raise RuntimeError(f"资源仓库缺少下载地址：{name}")
            download_files.append(
                {
                    **resource,
                    "download_url": str(remote["download_url"]),
                    "size": int(remote.get("size") or 0),
                }
            )
        return download_files

    def download_files(
        self,
        files: list[dict[str, Any]],
        destination_dir: Path,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        把资源文件下载到暂存目录并返回带 SHA256 的安装清单。

        :param on_progress: 已写入字节增量及文件总大小；重试前用负增量撤回失败字节
        """
        destination_dir.mkdir(parents=True, exist_ok=True)
        prepared: list[dict[str, Any]] = []
        for resource in files:
            resource_name = str(resource.get("name") or "")
            name = Path(resource_name)
            if name.name != resource_name:
                raise RuntimeError(f"资源文件名不安全：{resource_name}")
            destination = destination_dir / name.name
            checksum = self._download_resource(resource, destination, on_progress)
            prepared.append(
                {
                    "name": resource_name,
                    "type": resource.get("type"),
                    "version": resource.get("version"),
                    "path": str(destination),
                    "sha256": checksum,
                    "target": resource.get("target"),
                }
            )
        return prepared

    def _download_resource(
        self,
        resource: dict[str, Any],
        destination: Path,
        on_progress: Callable[[int, int], None] | None,
    ) -> str:
        """
        重试单个完整文件，校验长度后原子替换，始终清理未完成内容。

        :return: 最后一次成功传输的 SHA256；失败尝试不计入摘要与进度
        """
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = int(resource.get("size") or 0)

        def download() -> str:
            """
            单次流式传输失败时撤回进度，保证重试从空文件和新摘要开始。
            """
            current = 0
            digest = hashlib.sha256()
            try:
                with RequestUtils(
                    proxies=self.proxies,
                    headers=get_runtime_setting("GITHUB_HEADERS"),
                    timeout=180,
                ).get_stream(self._proxied(str(resource["download_url"])), raise_exception=True) as response:
                    stream = self._check_response(response)
                    with temporary.open("wb") as output:
                        for chunk in stream.iter_content(chunk_size=256 * 1024):
                            if not chunk:
                                continue
                            output.write(chunk)
                            digest.update(chunk)
                            current += len(chunk)
                            if on_progress:
                                on_progress(len(chunk), total)
                if total > 0 and current != total:
                    raise HttpStreamError(f"资源文件长度不符：预期 {total} 字节，收到 {current} 字节")
                temporary.replace(destination)
                return digest.hexdigest()
            except HttpRequestError:
                if on_progress and current:
                    on_progress(-current, total)
                raise
            finally:
                temporary.unlink(missing_ok=True)

        return self._request_with_retry(download, f"下载资源文件 {destination.name}")

    def check(
        self,
        *,
        auth_version: str | None = None,
        indexer_version: str | None = None,
    ) -> bool:
        """
        检查是否存在资源更新，不在运行目录直接安装资源。
        """
        try:
            return self.get_update_info(
                auth_version=auth_version,
                indexer_version=indexer_version,
            ) is not None
        # 定时检查失败必须保持静默
        except Exception as error:  # noqa: BLE001
            logger.warning(f"检查站点资源更新失败：{error}")
            return False

    @staticmethod
    def _proxied(url: str) -> str:
        """
        请求时规范化代理前缀，兼容带路径及省略尾斜杠的配置。
        """
        proxy = str(get_runtime_setting("GITHUB_PROXY") or "").strip()
        return f"{UrlUtils.standardize_base_url(proxy)}{url}" if proxy else url
