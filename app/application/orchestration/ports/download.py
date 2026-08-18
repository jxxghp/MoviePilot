"""下载器域的能力端口客户端。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from app.application.orchestration.ports.dispatch import CapabilityPorts
from app.domain.context import Context
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import TorrentStatus


class DownloadPorts(CapabilityPorts):
    """下载任务添加、查询与状态控制的能力端口。"""

    def download(
            self,
            content: Union[Path, str, bytes],
            download_dir: Path,
            cookie: str,
            episodes: Set[int] = None,
            category: Optional[str] = None,
            label: Optional[str] = None,
            downloader: Optional[str] = None,
    ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接或者种子内容
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  种子分类
        :param label:  标签
        :param downloader:  下载器
        :return: 下载器名称、种子Hash、种子文件布局、错误原因
        """
        return self._dispatch.unicast(
            "download",
            content=content,
            download_dir=download_dir,
            cookie=cookie,
            episodes=episodes,
            category=category,
            label=label,
            downloader=downloader,
        )

    def download_added(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Union[str, bytes] = None,
    ) -> None:
        """
        添加下载任务成功后的模块附加处理分发，站点字幕下载由 DownloadChain 另行编排
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param download_dir:  下载目录
        :param torrent_content: 种子内容，如果有则直接使用该内容，否则从 context 中获取种子文件路径
        :return: None，该方法可被多个模块同时处理
        """
        self._dispatch.broadcast(
            "download_added",
            context=context,
            torrent_content=torrent_content,
            download_dir=download_dir,
        )

    def list_torrents(
            self,
            status: TorrentStatus = None,
            hashs: Union[list, str] = None,
            downloader: Optional[str] = None,
            include_all_tags: bool = False,
    ) -> Optional[List[DownloaderTorrent]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :param downloader:  下载器
        :param include_all_tags:  是否包含未打内置标签的下载任务
        :return: 下载器中符合状态的种子列表
        """
        return [
            torrent
            for torrents in self._dispatch.multicast(
                "list_torrents",
                status=status,
                hashs=hashs,
                downloader=downloader,
                include_all_tags=include_all_tags,
            )
            for torrent in torrents
        ]

    def remove_torrents(
            self,
            hashs: Union[str, list],
            delete_file: bool = True,
            downloader: Optional[str] = None,
    ) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file: 是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        return self._dispatch.unicast(
            "remove_torrents",
            hashs=hashs,
            delete_file=delete_file,
            downloader=downloader,
        )

    def start_torrents(
            self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self._dispatch.unicast(
            "start_torrents", hashs=hashs, downloader=downloader
        )

    def stop_torrents(
            self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self._dispatch.unicast(
            "stop_torrents", hashs=hashs, downloader=downloader
        )

    def set_torrents_tag(
            self, hashs: Union[list, str], tags: list, downloader: Optional[str] = None
    ) -> bool:
        """
        设置种子标签
        :param hashs:  种子Hash
        :param tags:  标签列表
        :param downloader:  下载器
        :return: bool
        """
        return self._dispatch.unicast(
            "set_torrents_tag", hashs=hashs, tags=tags, downloader=downloader
        )

    def update_torrent(
            self,
            hash_string: str,
            downloader: Optional[str] = None,
            download_limit: Optional[float] = None,
            upload_limit: Optional[float] = None,
            tracker_list: Optional[list] = None,
            save_path: Optional[str] = None,
            category: Optional[str] = None,
            ratio_limit: Optional[float] = None,
            seeding_time_limit: Optional[int] = None,
    ) -> Optional[Dict[str, bool]]:
        """
        修改下载任务属性。
        :param hash_string: 种子Hash
        :param downloader: 下载器
        :param download_limit: 下载限速，单位 KB/s
        :param upload_limit: 上传限速，单位 KB/s
        :param tracker_list: Tracker URL列表
        :param save_path: 保存目录
        :param category: 分类
        :param ratio_limit: 分享率限制
        :param seeding_time_limit: 做种时间限制，单位分钟
        :return: 各项修改结果
        """
        return self._dispatch.unicast(
            "update_torrent",
            hash_string=hash_string,
            downloader=downloader,
            download_limit=download_limit,
            upload_limit=upload_limit,
            tracker_list=tracker_list,
            save_path=save_path,
            category=category,
            ratio_limit=ratio_limit,
            seeding_time_limit=seeding_time_limit,
        )

    def get_torrent_trackers(
            self,
            hash_string: str,
            downloader: Optional[str] = None,
    ) -> Optional[Dict[str, List[str]]]:
        """
        查询下载任务Tracker列表。
        :param hash_string: 种子Hash
        :param downloader: 下载器
        :return: 下载器名称到Tracker列表的映射
        """
        return self._dispatch.unicast(
            "get_torrent_trackers",
            hash_string=hash_string,
            downloader=downloader,
        )

    def torrent_files(
            self, tid: str, downloader: Optional[str] = None
    ) -> Optional[Any]:
        """
        获取种子文件
        :param tid:  种子Hash
        :param downloader:  下载器
        :return: 种子文件，具体类型由下载器实现决定（链层不引入下载器协议类型）
        """
        return self._dispatch.unicast("torrent_files", tid=tid, downloader=downloader)
