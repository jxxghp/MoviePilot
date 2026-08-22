import json
import platform
import sys
from pathlib import Path
from typing import Callable

from app.runtime import config as _runtime_config
from app.runtime.log import logger
from app.adapters.network.http import RequestUtils
from app.foundation.version import compare_version
from app.adapters.system.host import SystemUtils
from app.runtime.settings import get_runtime_setting


# 保留模块级旧 Settings 入口，旧插件和测试可能仍会对其做运行时覆盖；实现读取统一走 runtime 端口。
settings = _runtime_config.settings


ResourceVersionProvider = Callable[[], tuple[str, str]]


def _unavailable_resource_versions() -> tuple[str, str]:
    """站点能力尚未装配时返回可安全比较的空版本。"""
    return "0", "0"


_resource_version_provider: ResourceVersionProvider = _unavailable_resource_versions


def configure_resource_version_provider(provider: ResourceVersionProvider) -> None:
    """由组合根注入已加载的认证与索引版本，保持适配器不依赖应用层。"""
    global _resource_version_provider
    _resource_version_provider = provider


class ResourceHelper:
    """
    检测和更新资源包
    """

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
        return f"cp{version.major}{version.minor}"

    @staticmethod
    def _get_machine_tag() -> str:
        """将系统架构名称归一为资源文件使用的标签。"""
        machine = platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            return "aarch64"
        elif machine in {"x86_64", "amd64"}:
            return "x86_64"
        return machine

    @classmethod
    def _get_needed_files(cls) -> list[str]:
        """返回 V3 资源在当前平台需要下载的文件名。"""
        python_version = ResourceHelper._get_python_version_tag()
        python_ver = python_version.replace("cp", "")
        system = platform.system().lower()
        machine = ResourceHelper._get_machine_tag()
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

    def check(
        self,
        *,
        auth_version: str | None = None,
        indexer_version: str | None = None,
    ) -> bool:
        """
        检测并安装当前平台的资源更新。

        :param auth_version: 当前已加载的站点认证资源版本；省略时使用组合根注入值
        :param indexer_version: 当前已加载的站点索引资源版本；省略时使用组合根注入值
        :return: 是否成功安装了需要由上层处理重启的新资源
        """
        if not get_runtime_setting("AUTO_UPDATE_RESOURCE"):
            return False
        if SystemUtils.is_frozen():
            return False
        if auth_version is None or indexer_version is None:
            configured_auth_version, configured_indexer_version = (
                _resource_version_provider()
            )
            auth_version = auth_version or configured_auth_version
            indexer_version = indexer_version or configured_indexer_version
        logger.info("开始检测资源包版本...")
        res = self._load_resource_info()
        if res:
            try:
                resource_info = json.loads(res.text)
                online_version = resource_info.get("version")
                if online_version:
                    logger.info(f"最新资源包版本：v{online_version}")
                    # 需要更新的资源包
                    need_updates = {}
                    # 资源明细
                    resources: dict = resource_info.get("resources") or {}
                    for rname, resource in resources.items():
                        rtype = resource.get("type")
                        platform = resource.get("platform")
                        declared_target = Path(str(resource.get("target") or ""))
                        version = resource.get("version")
                        # 判断平台
                        if platform and platform != SystemUtils.platform():
                            continue
                        # 判断版本号
                        if rtype == "auth":
                            # 站点认证资源
                            local_version = auth_version
                        elif rtype == "sites":
                            # 站点索引资源
                            local_version = indexer_version
                        else:
                            continue
                        if compare_version(version, ">", local_version):
                            logger.info(f"{rname} 资源包有更新，最新版本：v{version}")
                        else:
                            continue
                        # 需要安装
                        if declared_target != self._resource_target:
                            logger.warning(
                                "忽略资源 %s 的非 canonical 目标目录：%s",
                                rname,
                                declared_target,
                            )
                            continue
                        need_updates[rname] = self._resource_target
                    if need_updates:
                        # 下载文件信息列表
                        r = RequestUtils(
                            proxies=get_runtime_setting("PROXY"),
                            headers=get_runtime_setting("GITHUB_HEADERS"),
                            timeout=30,
                        ).get_res(self._files_api)
                        if r and not r.ok:
                            logger.error(
                                f"连接仓库失败：{r.status_code} - {r.reason}"
                            )
                            return False
                        elif not r:
                            logger.error("连接仓库失败")
                            return False
                        files_info = r.json()
                        # 下载资源文件
                        needed_files = self._get_needed_files()
                        logger.info(f"需要下载的资源文件：{needed_files}")
                        success = True
                        for item in files_info:
                            file_name = item.get("name")
                            if file_name not in needed_files:
                                continue
                            save_path = need_updates.get(file_name)
                            if not save_path:
                                continue
                            if item.get("download_url"):
                                logger.info(f"开始更新资源文件：{file_name} ...")
                                download_url = (
                                    f"{get_runtime_setting('GITHUB_PROXY')}{item.get('download_url')}"
                                )
                                res = RequestUtils(
                                    proxies=self.proxies,
                                    headers=get_runtime_setting("GITHUB_HEADERS"),
                                    timeout=180,
                                ).get_res(download_url)
                                if not res:
                                    logger.error(f"文件 {file_name} 下载失败！")
                                    success = False
                                    break
                                elif res.status_code != 200:
                                    logger.error(
                                        f"下载文件 {file_name} 失败：{res.status_code} - {res.reason}"
                                    )
                                    success = False
                                    break
                                file_path = self._base_dir / save_path / file_name
                                if not file_path.parent.exists():
                                    file_path.parent.mkdir(parents=True, exist_ok=True)
                                file_path.write_bytes(res.content)
                        if success:
                            logger.info("资源包更新完成，等待启动层处理后续重启")
                            return True
                        else:
                            logger.warning("资源包更新失败，跳过升级！")
                    else:
                        logger.info("所有资源已最新，无需更新")
            except json.JSONDecodeError:
                logger.error("资源包仓库数据解析失败！")
                return False
        else:
            logger.warning("无法连接资源包仓库！")
        return False
