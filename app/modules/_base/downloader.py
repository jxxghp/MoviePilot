"""下载器模块业务样板基类。

沉淀三个内置下载器模块（qbittorrent/transmission/rtorrent）逐字复制的样板：
连接测试、定时重连、种子信息读取与查询状态归一。差异化逻辑
（任务添加、原始状态映射、任务列表构建）仍留在各模块。
"""
from pathlib import Path
from collections.abc import Callable
from typing import Any, List, Optional, Tuple, TypeVar, Union

from torrentool.torrent import Torrent

from app.domain import torrent as torrent_rules
from app.modules import _DownloaderBase, _ModuleBase, TService
from app.runtime.cache import FileCache
from app.runtime.log import logger
from app.schemas.types import TorrentQueryStatus, TorrentStatus


TFile = TypeVar("TFile")


class _DownloaderModuleBase(_ModuleBase, _DownloaderBase[TService]):
    """
    下载器模块业务样板基类。
    """

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
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
        定时任务，每10分钟调用一次
        """
        for name, server in self.get_instances().items():
            if server.is_inactive():
                logger.info(f"{self.get_name()}下载器 {name} 连接断开，尝试重连 ...")
                server.reconnect()

    @staticmethod
    def _normalize_torrent_files(
        files: Any, item_factory: Callable[[Any], TFile]
    ) -> Optional[List[TFile]]:
        """把 provider 文件集合统一投影为不依赖外部 SDK 的宿主 DTO。"""
        if files is None:
            return None
        source = getattr(files, "data", files)
        return [item_factory(item) for item in source]

    def _get_torrent_info(self, content: Union[Path, str, bytes]) \
            -> Tuple[Optional[Torrent], Optional[bytes]]:
        """
        读取种子内容，返回解析后的种子信息与原始内容，磁力链接不解析
        """
        torrent_info, torrent_content = None, None
        try:
            if isinstance(content, Path):
                if content.exists():
                    torrent_content = content.read_bytes()
                else:
                    # 读取缓存的种子文件
                    torrent_content = FileCache().get(
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
        归一任务查询状态。
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
