"""
下载器模块业务样板基类。

沉淀三个内置下载器模块（qbittorrent/transmission/rtorrent）逐字复制的样板：
连接测试、定时重连、种子信息读取与查询状态归一。差异化逻辑
（任务添加、原始状态映射、任务列表构建）仍留在各模块。
"""
from collections.abc import Callable
from pathlib import Path
from typing import Any, List, Optional, Protocol, Tuple, TypeVar, Union

from torrentool.torrent import Torrent

from app.domain import torrent as torrent_rules
from app.modules import _DownloaderBase, _ModuleBase
from app.runtime.cache import FileCache
from app.runtime.log import logger
from app.schemas.types import TorrentQueryStatus, TorrentStatus


class _DownloaderClient(Protocol):
    """
    下载器连接巡检依赖的最小同步客户端接口。
    """

    def is_inactive(self) -> bool:
        """
        判断已配置的客户端是否需要重新建立连接。

        :return: 连接未就绪时返回 True，否则返回 False
        """
        ...

    def reconnect(self) -> None:
        """
        按下载器的登录策略刷新连接。

        实现方负责替换连接状态；调用结束不代表连接一定成功，需继续检查服务状态。
        """
        ...

    def transfer_info(self) -> object:
        """
        读取下载器的原始传输状态。

        :return: 下载器状态对象；不可用时返回空值，巡检调用方仅检查其真假值
        """
        ...


DownloaderServiceT = TypeVar("DownloaderServiceT", bound=_DownloaderClient)
TFile = TypeVar("TFile")


class _DownloaderModuleBase(_ModuleBase, _DownloaderBase[DownloaderServiceT]):
    """
    下载器模块业务样板基类。
    """

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检查全部已启用下载器的连接状态，必要时先尝试重连。

        :return: 首个失败的说明或整体成功结果；没有实例时返回 None
        """
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            if server.is_inactive():
                server.reconnect()
            if not server.transfer_info():
                return False, f"无法连接{self.get_name()}下载器：{name}"
        return True, ""

    def scheduler_job(self) -> None:
        """
        巡检已配置下载器，对失活连接发起重连。

        由模块调度器每十分钟调用一次。
        """
        for name, server in self.get_instances().items():
            if server.is_inactive():
                logger.info(f"{self.get_name()}下载器 {name} 连接断开，尝试重连 ...")
                server.reconnect()

    @staticmethod
    def _normalize_torrent_files(
        files: Any, item_factory: Callable[[Any], TFile]
    ) -> Optional[List[TFile]]:
        """
        将下载器文件集合投影为宿主数据对象。

        :param files: 下载器返回的集合；带 data 属性时读取该属性
        :param item_factory: 把单个下载器文件转换为宿主文件对象的函数
        :return: 转换后的文件列表；下载器返回 None 时仍返回 None
        """
        if files is None:
            return None
        source = getattr(files, "data", files)
        return [item_factory(item) for item in source]

    def _get_torrent_info(self, content: Union[Path, str, bytes]) \
            -> Tuple[Optional[Torrent], Optional[Union[str, bytes]]]:
        """
        读取种子内容并解析元信息，磁力链接保持原样。

        路径不存在时查询本地种子缓存，不向外部站点重新下载。

        :param content: 本地路径、种子内容或磁力链接
        :return: 种子对象与原始内容；磁力链接的对象为空，读取或解析异常时两项均为空
        """
        torrent_content: Optional[Union[str, bytes]]
        torrent_info, torrent_content = None, None
        try:
            if isinstance(content, Path):
                if content.exists():
                    torrent_content = content.read_bytes()
                else:
                    # 读取缓存的种子文件；与 TorrentHelper 写入路由一致，只在本地文件系统
                    torrent_content = FileCache(local_only=True).get(
                        content.as_posix(), region="torrents"
                    )
            else:
                torrent_content = content

            if torrent_content:
                # 检查是否为磁力链接
                if torrent_rules.is_magnet_link(torrent_content):
                    return None, torrent_content
                else:
                    torrent_info = Torrent.from_string(torrent_content)

            return torrent_info, torrent_content
        except Exception as e:
            logger.error(f"获取种子名称失败：{e}")
            return None, None

    @staticmethod
    def _normalize_query_status(
            status: Optional[Union[TorrentStatus, TorrentQueryStatus, str]]
    ) -> TorrentQueryStatus:
        """
        把旧状态枚举、文本别名和新查询状态归一。

        :param status: 下载状态枚举或字符串，兼容中文与旧名称
        :return: 规范查询状态；空值或未知值回退为查询全部
        """
        status_value = getattr(status, "value", status)
        status_text = str(status_value or "").strip().lower()
        if not status_text or status_text in {"all", "全部"}:
            return TorrentQueryStatus.ALL
        if status_text in {
            TorrentStatus.TRANSFER.value,
            TorrentQueryStatus.TRANSFER.value,
            "transfer",
        }:
            return TorrentQueryStatus.TRANSFER
        if status_text in {
            TorrentStatus.DOWNLOADING.value,
            TorrentQueryStatus.DOWNLOADING.value,
            "downloading",
        }:
            return TorrentQueryStatus.DOWNLOADING
        if status_text in {
            TorrentQueryStatus.COMPLETED.value,
            "complete",
            "seeding",
            "完成",
            "已完成",
        }:
            return TorrentQueryStatus.COMPLETED
        if status_text in {TorrentQueryStatus.PAUSED.value, "pause", "暂停", "已暂停"}:
            return TorrentQueryStatus.PAUSED
        return TorrentQueryStatus.ALL
