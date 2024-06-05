import json
from pathlib import Path

from app.core.config import settings
from app.helper.sites import SitesHelper
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class ResourceHelper(metaclass=Singleton):
    """
    检测和更新资源包
    """
    # 资源包的git仓库地址
    _repo = f"{settings.GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/package.json"
    _files_api = f"https://api.github.com/repos/jxxghp/MoviePilot-Resources/contents/resources"
    _base_dir: Path = settings.ROOT_PATH

    def __init__(self):
        self.siteshelper = SitesHelper()
        self.check()

    @property
    def proxies(self):
        return None if settings.GITHUB_PROXY else settings.PROXY

    def check(self):
        """
        检测是否有更新，如有则下载安装
        """
        if not settings.AUTO_UPDATE_RESOURCE:
            return
        if SystemUtils.is_frozen():
            return
        logger.info("开始检测资源包版本...")
        res = RequestUtils(proxies=self.proxies, headers=settings.GITHUB_HEADERS, timeout=10).get_res(self._repo)
        if res:
            try:
                resource_info = json.loads(res.text)
            except json.JSONDecodeError:
                logger.error("资源包仓库数据解析失败！")
                return
        else:
            logger.warn("无法连接资源包仓库！")
            return
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
            target = resource.get("target")
            version = resource.get("version")
            # 判断平台
            if platform and platform != SystemUtils.platform():
                continue
            # 判断版本号
            if rtype == "auth":
                # 站点认证资源
                local_version = self.siteshelper.auth_version
            elif rtype == "sites":
                # 站点索引资源
                local_version = self.siteshelper.indexer_version
            else:
                continue
            if StringUtils.compare_version(version, local_version) > 0:
                logger.info(f"{rname} 资源包有更新，最新版本：v{version}")
            else:
                continue
            # 需要安装
            need_updates[rname] = target
        if need_updates:
            # 下载文件信息列表
            r = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS,
                             timeout=30).get_res(self._files_api)
            if r and not r.ok:
                return None, f"连接仓库失败：{r.status_code} - {r.reason}"
            elif not r:
                return None, "连接仓库失败"
            files_info = r.json()
            for item in files_info:
                save_path = need_updates.get(item.get("name"))
                if not save_path:
                    continue
                if item.get("download_url"):
                    logger.info(f"开始更新资源文件：{item.get('name')} ...")
                    download_url = f"{settings.GITHUB_PROXY}{item.get('download_url')}"
                    # 下载资源文件
                    res = RequestUtils(proxies=self.proxies, headers=settings.GITHUB_HEADERS,
                                       timeout=180).get_res(download_url)
                    if not res:
                        logger.error(f"文件 {item.get('name')} 下载失败！")
                    elif res.status_code != 200:
                        logger.error(f"下载文件 {item.get('name')} 失败：{res.status_code} - {res.reason}")
                    # 创建插件文件夹
                    file_path = self._base_dir / save_path / item.get("name")
                    if not file_path.parent.exists():
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                    # 写入文件
                    file_path.write_bytes(res.content)
            logger.info("资源包更新完成，开始重启服务...")
            SystemUtils.restart()
        else:
            logger.info("所有资源已最新，无需更新")
