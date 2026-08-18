from pathlib import Path
from typing import Set, Tuple, Optional, Union, List, Dict

from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.runtime.config import settings
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.modules._base import _DownloaderModuleBase
from app.modules.rtorrent.rtorrent import Rtorrent
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import (
    DownloadTaskState,
    TorrentQueryStatus,
    TorrentStatus,
)
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools


class RtorrentModule(_DownloaderModuleBase[Rtorrent]):
    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(
            service_name=Rtorrent.__name__.lower(), service_type=Rtorrent
        )

    @staticmethod
    def get_name() -> str:
        return "Rtorrent"

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 3

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

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
        :param content:  种子文件地址或者磁力链接或种子内容
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  分类，rTorrent中未使用
        :param label:  标签
        :param downloader:  下载器
        :return: 下载器名称、种子Hash、种子文件布局、错误原因
        """

        if not content:
            return None, None, None, "下载内容为空"

        # 读取种子的名称
        torrent_from_file, content = self._get_torrent_info(content)
        # 检查是否为磁力链接
        is_magnet = (
            isinstance(content, str)
            and content.startswith("magnet:")
            or isinstance(content, bytes)
            and content.startswith(b"magnet:")
        )
        if not torrent_from_file and not is_magnet:
            return None, None, None, f"添加种子任务失败：无法读取种子文件"

        # 获取下载器
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None

        # 生成随机Tag
        tag = text_tools.random_string(10)
        if label:
            tags = label.split(",") + [tag]
        elif settings.TORRENT_TAG:
            tags = [tag, settings.TORRENT_TAG]
        else:
            tags = [tag]
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        # 添加任务
        state = server.add_torrent(
            content=content,
            download_dir=self.normalize_path(download_dir, downloader),
            is_paused=is_paused,
            tags=tags,
            cookie=cookie,
        )

        # rTorrent 始终使用原始种子布局
        torrent_layout = "Original"

        if not state:
            # 查询所有下载器的种子
            torrents, error = server.get_torrents()
            if error:
                return None, None, None, "无法连接rTorrent下载器"
            if torrents:
                try:
                    for torrent in torrents:
                        # 名称与大小相等则认为是同一个种子
                        if torrent.get("name") == getattr(
                            torrent_from_file, "name", ""
                        ) and torrent.get("total_size") == getattr(
                            torrent_from_file, "total_size", 0
                        ):
                            torrent_hash = torrent.get("hash")
                            torrent_tags = [
                                str(t).strip()
                                for t in torrent.get("tags", "").split(",")
                                if t.strip()
                            ]
                            logger.warn(
                                f"下载器中已存在该种子任务：{torrent_hash} - {torrent.get('name')}"
                            )
                            # 给种子打上标签
                            if "已整理" in torrent_tags:
                                server.remove_torrents_tag(
                                    ids=torrent_hash, tag=["已整理"]
                                )
                            if (
                                settings.TORRENT_TAG
                                and settings.TORRENT_TAG not in torrent_tags
                            ):
                                logger.info(
                                    f"给种子 {torrent_hash} 打上标签：{settings.TORRENT_TAG}"
                                )
                                server.set_torrents_tag(
                                    ids=torrent_hash, tags=[settings.TORRENT_TAG]
                                )
                            return (
                                downloader or self.get_default_config_name(),
                                torrent_hash,
                                torrent_layout,
                                f"下载任务已存在",
                            )
                finally:
                    torrents.clear()
                    del torrents
            return None, None, None, f"添加种子任务失败：{content}"
        else:
            # 获取种子Hash
            torrent_hash = server.get_torrent_id_by_tag(tags=tag)
            if not torrent_hash:
                return (
                    None,
                    None,
                    None,
                    f"下载任务添加成功，但获取rTorrent任务信息失败：{content}",
                )
            else:
                if is_paused:
                    # 种子文件
                    torrent_files = server.get_files(torrent_hash)
                    if not torrent_files:
                        return (
                            downloader or self.get_default_config_name(),
                            torrent_hash,
                            torrent_layout,
                            "获取种子文件失败，下载任务可能在暂停状态",
                        )

                    # 不需要的文件ID
                    file_ids = []
                    # 需要的集清单
                    sucess_epidised = set()
                    try:
                        for torrent_file in torrent_files:
                            file_id = torrent_file.get("id")
                            file_name = torrent_file.get("name")
                            meta_info = MetaInfo(file_name)
                            if not meta_info.episode_list or not set(
                                meta_info.episode_list
                            ).issubset(episodes):
                                file_ids.append(file_id)
                            else:
                                sucess_epidised.update(meta_info.episode_list)
                    finally:
                        torrent_files.clear()
                        del torrent_files
                    sucess_epidised = list(sucess_epidised)
                    if sucess_epidised and file_ids:
                        # 设置不需要的文件优先级为0（不下载）
                        server.set_files(
                            torrent_hash=torrent_hash, file_ids=file_ids, priority=0
                        )
                    # 开始任务
                    server.start_torrents(torrent_hash)
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        f"添加下载成功，已选择集数：{sucess_epidised}",
                    )
                else:
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        "添加下载成功",
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
        # 获取下载器
        if downloader:
            server: Rtorrent = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers: Dict[str, Rtorrent] = self.get_instances()
        ret_torrents = []
        query_status = self._normalize_query_status(status)
        query_tags = None if include_all_tags else settings.TORRENT_TAG

        def __get_torrent_path(torrent_data: dict) -> Path:
            """
            获取种子内容路径。
            """
            content_path = torrent_data.get("content_path")
            if content_path:
                return Path(content_path)
            return Path(torrent_data.get("save_path")) / torrent_data.get("name")

        def __build_torrent(downloader_name: str, torrent_data: dict) -> DownloaderTorrent:
            """
            构造统一下载器任务对象。
            """
            meta = MetaInfo(torrent_data.get("name"))
            dlspeed = torrent_data.get("dlspeed") or 0
            upspeed = torrent_data.get("upspeed") or 0
            total_size = torrent_data.get("total_size") or 0
            completed_size = torrent_data.get("completed") or 0
            torrent_path = __get_torrent_path(torrent_data)
            return DownloaderTorrent(
                downloader=downloader_name,
                hash=torrent_data.get("hash"),
                title=torrent_data.get("name"),
                name=meta.name,
                year=meta.year,
                season_episode=meta.season_episode,
                path=Path(self.normalize_return_path(torrent_path, downloader_name)),
                save_path=self.normalize_return_path(
                    Path(torrent_data.get("save_path") or ""), downloader_name
                ) if torrent_data.get("save_path") else None,
                content_path=self.normalize_return_path(torrent_path, downloader_name),
                progress=torrent_data.get("progress", 0),
                size=total_size,
                state=self.__normalize_torrent_state(
                    torrent_data.get("state"), torrent_data.get("complete")
                ),
                dlspeed=size_tools.format_compact_size(dlspeed),
                upspeed=size_tools.format_compact_size(upspeed),
                tags=torrent_data.get("tags"),
                left_time=time_tools.format_duration((total_size - completed_size) / dlspeed)
                if dlspeed > 0
                else "",
            )

        if hashs:
            # 按Hash获取
            for name, server in servers.items():
                torrents, _ = (
                    server.get_torrents(ids=hashs, tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.TRANSFER:
            # 获取已完成且未整理的
            for name, server in servers.items():
                torrents = (
                    server.get_completed_torrents(tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        tags = torrent_info.get("tags") or ""
                        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
                        if "已整理" in tag_list:
                            continue
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.DOWNLOADING:
            # 获取正在下载的任务
            for name, server in servers.items():
                torrents = (
                    server.get_downloading_torrents(tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status in (
                TorrentQueryStatus.ALL,
                TorrentQueryStatus.COMPLETED,
                TorrentQueryStatus.PAUSED,
        ):
            # 获取完整任务列表，由 MoviePilot 统一归一实际下载器状态。
            for name, server in servers.items():
                torrents, _ = server.get_torrents(tags=query_tags) or []
                try:
                    for torrent_info in torrents:
                        torrent_state = self.__normalize_torrent_state(
                            torrent_info.get("state"), torrent_info.get("complete")
                        )
                        if (
                                query_status != TorrentQueryStatus.ALL
                                and torrent_state != query_status.value
                        ):
                            continue
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        else:
            return None
        return ret_torrents  # noqa

    @staticmethod
    def __normalize_torrent_state(
            state: Optional[Union[int, str]],
            complete: Optional[Union[int, str]],
    ) -> str:
        """
        归一 rTorrent 原始任务状态。
        """
        if str(state) == "0":
            return DownloadTaskState.PAUSED.value
        if str(complete) == "0":
            return DownloadTaskState.DOWNLOADING.value
        return DownloadTaskState.COMPLETED.value

    def transfer_completed(
        self, hashs: Union[str, list], downloader: Optional[str] = None
    ) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param downloader:  下载器
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        # 获取原标签
        org_tags = server.get_torrent_tags(ids=hashs)
        # 种子打上已整理标签
        if org_tags:
            tags = org_tags + ["已整理"]
        else:
            tags = ["已整理"]
        # 直接设置完整标签（覆盖）
        server.set_torrents_tag(ids=hashs, tags=tags, overwrite=True)
        return None

    def remove_torrents(
        self,
        hashs: Union[str, list],
        delete_file: Optional[bool] = True,
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file:  是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.delete_torrents(delete_file=delete_file, ids=hashs)

    def set_torrents_tag(
        self, hashs: Union[str, list], tags: list,
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """
        设置种子标签
        :param hashs:  种子Hash
        :param tags:  标签列表
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.set_torrents_tag(ids=hashs, tags=tags)

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
        :param tracker_list: Tracker URL列表，rTorrent 当前封装不支持
        :param save_path: 保存目录
        :param category: 分类，rTorrent 当前封装不支持
        :param ratio_limit: 分享率限制，rTorrent 当前封装不支持
        :param seeding_time_limit: 做种时间限制，rTorrent 当前封装不支持
        :return: 各项修改结果
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        results = {}
        if download_limit is not None or upload_limit is not None:
            results["limits"] = server.change_torrent(
                hash_string=hash_string,
                download_limit=download_limit,
                upload_limit=upload_limit,
            )
        if ratio_limit is not None or seeding_time_limit is not None:
            results["seeding_limits"] = False
        if tracker_list is not None:
            results["trackers"] = False
        if save_path is not None:
            results["save_path"] = server.set_torrent_location(
                hash_string=hash_string,
                location=self.normalize_path(Path(save_path), downloader),
            )
        if category is not None:
            results["category"] = False
        return results

    def start_torrents(
        self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> Optional[bool]:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def stop_torrents(
        self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> Optional[bool]:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.stop_torrents(ids=hashs)

    def torrent_files(
        self, tid: str, downloader: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """
        获取种子文件列表
        """
        server: Rtorrent = self.get_instance(downloader)
        if not server:
            return None
        return server.get_files(tid=tid)

    def downloader_info(
        self, downloader: Optional[str] = None
    ) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        下载器信息
        """
        if downloader:
            server: Rtorrent = self.get_instance(downloader)
            if not server:
                return None
            servers = [server]
        else:
            servers = self.get_instances().values()
        ret_info = []
        for server in servers:
            info = server.transfer_info()
            if not info:
                continue
            ret_info.append(
                _SchemaDownloaderInfo(
                    download_speed=info.get("dl_info_speed"),
                    upload_speed=info.get("up_info_speed"),
                    download_size=info.get("dl_info_data"),
                    upload_size=info.get("up_info_data"),
                )
            )
        return ret_info
