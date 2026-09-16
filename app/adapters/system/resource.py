"""站点认证与索引资源的版本检查和离线下载适配器。"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import sysconfig
from pathlib import Path
from typing import Any, Callable

from app.adapters.network.http import RequestUtils
from app.adapters.system.host import SystemUtils
from app.foundation.version import compare_version
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

ResourceVersionProvider = Callable[[], tuple[str, str]]


def _unavailable_resource_versions() -> tuple[str, str]:
    """站点能力尚未装配时返回可安全比较的空版本。"""
    return "0", "0"


_resource_version_provider: ResourceVersionProvider = _unavailable_resource_versions


def configure_resource_version_provider(provider: ResourceVersionProvider) -> None:
    """由组合根注入已加载的认证与索引版本，保持适配器不依赖应用层。"""
    global _resource_version_provider
    _resource_version_provider = provider


def reset_resource_version_provider() -> None:
    """撤销当前 lifespan 的资源版本来源，恢复安全空版本。"""
    global _resource_version_provider
    _resource_version_provider = _unavailable_resource_versions


def get_resource_versions() -> tuple[str, str]:
    """读取当前进程已加载的认证资源版本和索引资源版本。"""
    try:
        return _resource_version_provider()
    except Exception as error:  # noqa: BLE001  版本读取失败时不能阻断系统状态接口
        logger.warning(f"读取站点资源版本失败：{error}")
        return _unavailable_resource_versions()


class ResourceHelper:
    """检查站点资源版本，并把待更新文件下载到外部暂存目录。"""

    _base_dir: Path = get_runtime_setting("ROOT_PATH")
    _resource_target = Path("app/application/site")
    _version_flag = get_runtime_setting("RESOURCE_VERSION_FLAG")
    _repo = (
        f"{get_runtime_setting('GITHUB_PROXY')}https://raw.githubusercontent.com/"
        f"jxxghp/MoviePilot-Resources/main/package.{_version_flag}.json"
    )
    _files_api = (
        "https://api.github.com/repos/jxxghp/"
        f"MoviePilot-Resources/contents/resources.{_version_flag}"
    )

    @property
    def proxies(self):
        """返回访问 GitHub 资源时应使用的代理配置。"""
        return (
            None
            if get_runtime_setting("GITHUB_PROXY")
            else get_runtime_setting("PROXY")
        )

    @staticmethod
    def _get_python_version_tag() -> str:
        """返回资源文件名使用的 CPython ABI 标签。"""
        version = sys.version_info
        free_threaded = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
        return f"cp{version.major}{version.minor}{free_threaded}"

    @staticmethod
    def _get_machine_tag() -> str:
        """将系统架构名称归一为资源文件使用的标签。"""
        machine = platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            return "aarch64"
        if machine in {"x86_64", "amd64"}:
            return "x86_64"
        return machine

    @classmethod
    def _get_needed_files(cls) -> list[str]:
        """返回 V3 资源在当前平台需要下载的文件名。"""
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

    def _load_resource_info(self):
        """读取 V3 资源清单。"""
        response = RequestUtils(
            proxies=self.proxies,
            headers=get_runtime_setting("GITHUB_HEADERS"),
            timeout=10,
        ).get_res(self._repo)
        return response if response and response.status_code == 200 else None

    def get_update_info(
        self,
        *,
        auth_version: str | None = None,
        indexer_version: str | None = None,
    ) -> dict[str, Any] | None:
        """读取当前平台的资源更新元数据，但不写入运行目录。

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
        """解析资源目录 API，补齐每个待下载文件的地址和大小。"""
        response = RequestUtils(
            proxies=get_runtime_setting("PROXY"),
            headers=get_runtime_setting("GITHUB_HEADERS"),
            timeout=30,
        ).get_res(self._files_api)
        if response is None or response.status_code != 200:
            raise RuntimeError(
                f"连接资源仓库失败：HTTP {getattr(response, 'status_code', '无响应')}"
            )
        files_by_name = {
            item.get("name"): item
            for item in response.json()
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
        """把资源文件下载到暂存目录并返回带 SHA256 的安装清单。"""
        destination_dir.mkdir(parents=True, exist_ok=True)
        prepared: list[dict[str, Any]] = []
        for resource in files:
            resource_name = str(resource.get("name") or "")
            name = Path(resource_name)
            if name.name != resource_name:
                raise RuntimeError(f"资源文件名不安全：{resource_name}")
            destination = destination_dir / name.name
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.unlink(missing_ok=True)
            current = 0
            digest = hashlib.sha256()
            with RequestUtils(
                proxies=self.proxies,
                headers=get_runtime_setting("GITHUB_HEADERS"),
                timeout=180,
            ).get_stream(self._proxied(str(resource["download_url"]))) as response:
                if response is None or response.status_code != 200:
                    raise RuntimeError(
                        f"下载资源文件失败：{resource_name}，HTTP {getattr(response, 'status_code', '无响应')}"
                    )
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        digest.update(chunk)
                        current += len(chunk)
                        if on_progress:
                            on_progress(len(chunk), int(resource.get("size") or 0))
            temporary.replace(destination)
            prepared.append(
                {
                    "name": resource_name,
                    "type": resource.get("type"),
                    "version": resource.get("version"),
                    "path": str(destination),
                    "sha256": digest.hexdigest(),
                    "target": resource.get("target"),
                }
            )
        return prepared

    def check(
        self,
        *,
        auth_version: str | None = None,
        indexer_version: str | None = None,
    ) -> bool:
        """检查是否存在资源更新，不在运行目录直接安装资源。"""
        try:
            return self.get_update_info(
                auth_version=auth_version,
                indexer_version=indexer_version,
            ) is not None
        except Exception as error:  # noqa: BLE001  定时检查失败必须保持静默
            logger.warning(f"检查站点资源更新失败：{error}")
            return False

    @staticmethod
    def _proxied(url: str) -> str:
        """为资源直链添加 GitHub 代理前缀。"""
        proxy = str(get_runtime_setting("GITHUB_PROXY") or "").strip()
        return f"{proxy}{url}" if proxy else url
