from pathlib import Path
from typing import Set, Tuple, Optional, Union, List, Dict

from transmission_rpc import File

from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.runtime.config import settings
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.modules._base import _DownloaderModuleBase
from app.modules.transmission.transmission import Transmission
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import (
    DownloadTaskState,
    TorrentQueryStatus,
    TorrentStatus,
)
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools

_TRANSMISSION_DOWNLOADING_STATES = {
    "download_pending",
    "downloading",
}
_TRANSMISSION_PAUSED_STATES = {
    "stopped",
}


class TransmissionModule(_DownloaderModuleBase[Transmission]):

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=Transmission.__name__.lower(),
                             service_type=Transmission)

    @staticmethod
    def get_name() -> str:
        return "Transmission"

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 2

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def download(self, content: Union[Path, str, bytes], download_dir: Path, cookie: str,
                 episodes: Set[int] = None, category: Optional[str] = None, label: Optional[str] = None,
                 downloader: Optional[str] = None) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接或种子内容
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  分类，TR中未使用
        :param label:  标签
        :param downloader:  下载器
        :return: 下载器名称、种子Hash、种子文件布局、错误原因
        """

        if not content:
            return None, None, None, "下载内容为空"

        # 读取种子的名称
        torrent_from_file, content = self._get_torrent_info(content)
        # 检查是否为磁力链接
        is_magnet = isinstance(content, str) and content.startswith("magnet:") or isinstance(content,
                                                                                             bytes) and content.startswith(
            b"magnet:")
        if not torrent_from_file and not is_magnet:
            return None, None, None, f"添加种子任务失败：无法读取种子文件"

        # 获取下载器
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None

        # 如果要选择文件则先暂停
        is_paused = True if episodes else False

        # 标签
        if label:
            labels = label.split(',')
        elif settings.TORRENT_TAG:
            labels = settings.TORRENT_TAG.split(',')
        else:
            labels = None
        # 添加任务
        added_torrent = server.add_torrent(
            content=content,
            download_dir=self.normalize_path(download_dir, downloader),
            is_paused=is_paused,
            labels=labels,
            cookie=cookie
        )
        # TR 始终使用原始种子布局, 返回"Original"
        torrent_layout = "Original"

        if not added_torrent:
            # 查询所有下载器的种子
            torrents, error = server.get_torrents()
            if error:
                return None, None, None, "无法连接transmission下载器"
            if torrents:
                try:
                    for torrent in torrents:
                        # 名称与大小相等则认为是同一个种子
                        if torrent.name == getattr(torrent_from_file, 'name', '') and torrent.total_size == getattr(torrent_from_file, 'total_size', 0):
                            torrent_hash = torrent.hashString
                            logger.warn(f"下载器中已存在该种子任务：{torrent_hash} - {torrent.name}")
                            # 给种子打上标签
                            if settings.TORRENT_TAG:
                                logger.info(f"给种子 {torrent_hash} 打上标签：{settings.TORRENT_TAG}")
                                # 种子标签
                                labels = [str(tag).strip()
                                          for tag in torrent.labels] if hasattr(torrent, "labels") else []
                                if "已整理" in labels:
                                    labels.remove("已整理")
                                    server.set_torrent_tag(ids=torrent_hash, tags=labels)
                                if settings.TORRENT_TAG and settings.TORRENT_TAG not in labels:
                                    labels.append(settings.TORRENT_TAG)
                                    server.set_torrent_tag(ids=torrent_hash, tags=labels)
                            return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, f"下载任务已存在"
                finally:
                    torrents.clear()
                    del torrents
            return None, None, None, f"添加种子任务失败：{content}"
        else:
            torrent_hash = added_torrent.hashString
            if is_paused:
                # 选择文件
                torrent_files = server.get_files(torrent_hash)
                if not torrent_files:
                    return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, "获取种子文件失败，下载任务可能在暂停状态"
                # 需要的文件信息
                file_ids = []
                unwanted_file_ids = []
                try:
                    for torrent_file in torrent_files:
                        file_id = torrent_file.id
                        file_name = torrent_file.name
                        meta_info = MetaInfo(file_name)
                        if not meta_info.episode_list:
                            unwanted_file_ids.append(file_id)
                            continue
                        selected = set(meta_info.episode_list).issubset(set(episodes))
                        if not selected:
                            unwanted_file_ids.append(file_id)
                            continue
                        file_ids.append(file_id)
                    # 选择文件
                    server.set_files(torrent_hash, file_ids)
                    server.set_unwanted_files(torrent_hash, unwanted_file_ids)
                    # 开始任务
                    server.start_torrents(torrent_hash)
                finally:
                    torrent_files.clear()
                    del torrent_files
                return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, "添加下载任务成功"
            else:
                return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, "添加下载任务成功"

    def list_torrents(self, status: TorrentStatus = None,
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
            server: Transmission = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers: Dict[str, Transmission] = self.get_instances()
        ret_torrents = []
        query_status = self._normalize_query_status(status)
        query_tags = None if include_all_tags else settings.TORRENT_TAG

        def __get_torrent_attr(torrent_data, *attr_names):
            """
            兼容 transmission-rpc 新旧字段名。
            """
            for attr_name in attr_names:
                try:
                    return getattr(torrent_data, attr_name)
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
            return None

        def __get_torrent_progress(torrent_data) -> float:
            """
            获取任务进度。
            """
            return __get_torrent_attr(torrent_data, "progress", "percent_done") or 0

        def __get_torrent_size(torrent_data) -> int:
            """
            获取任务大小。
            """
            return __get_torrent_attr(torrent_data, "total_size", "totalSize") or 0

        def __get_torrent_labels(torrent_data) -> str:
            """
            获取任务标签。
            """
            return ",".join(getattr(torrent_data, "labels", None) or [])

        def __get_torrent_path(torrent_data) -> Path:
            """
            获取任务内容路径。
            """
            return Path(torrent_data.download_dir) / torrent_data.name

        def __build_torrent(downloader_name: str, torrent_data) -> DownloaderTorrent:
            """
            构造统一下载器任务对象。
            """
            meta = MetaInfo(torrent_data.name)
            dlspeed = __get_torrent_attr(
                torrent_data, "rate_download", "rateDownload"
            ) or 0
            upspeed = __get_torrent_attr(
                torrent_data, "rate_upload", "rateUpload"
            ) or 0
            left_until_done = __get_torrent_attr(
                torrent_data, "left_until_done", "leftUntilDone"
            ) or 0
            torrent_path = __get_torrent_path(torrent_data)
            ratio_limit = __get_torrent_attr(torrent_data, "seed_ratio_limit", "seedRatioLimit")
            seeding_time_limit = __get_torrent_attr(torrent_data, "seed_idle_limit", "seedIdleLimit")
            return DownloaderTorrent(
                downloader=downloader_name,
                hash=torrent_data.hashString,
                title=torrent_data.name,
                name=meta.name,
                year=meta.year,
                season_episode=meta.season_episode,
                path=Path(self.normalize_return_path(torrent_path, downloader_name)),
                save_path=self.normalize_return_path(
                    Path(torrent_data.download_dir), downloader_name
                ) if getattr(torrent_data, "download_dir", None) else None,
                content_path=self.normalize_return_path(torrent_path, downloader_name),
                progress=__get_torrent_progress(torrent_data),
                size=__get_torrent_size(torrent_data),
                state=self.__normalize_torrent_state(torrent_data.status),
                dlspeed=size_tools.format_compact_size(dlspeed),
                upspeed=size_tools.format_compact_size(upspeed),
                tags=__get_torrent_labels(torrent_data),
                download_limit=__get_torrent_attr(torrent_data, "download_limit", "downloadLimit"),
                upload_limit=__get_torrent_attr(torrent_data, "upload_limit", "uploadLimit"),
                ratio_limit=ratio_limit,
                seeding_time_limit=seeding_time_limit,
                left_time=time_tools.format_duration(
                    left_until_done / dlspeed
                ) if dlspeed > 0 else ''
            )

        if hashs:
            # 按Hash获取
            for name, server in servers.items():
                torrents, _ = server.get_torrents(ids=hashs, tags=query_tags) or []
                try:
                    for torrent_info in torrents:
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.TRANSFER:
            # 获取已完成且未整理的
            for name, server in servers.items():
                torrents = server.get_completed_torrents(tags=query_tags) or []
                try:
                    for torrent_info in torrents:
                        # 含"已整理"tag的不处理
                        if "已整理" in torrent_info.labels or []:
                            continue
                        # 下载路径
                        path = torrent_info.download_dir
                        # 无法获取下载路径的不处理
                        if not path:
                            logger.debug(f"未获取到 {torrent_info.name} 下载保存路径")
                            continue
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.DOWNLOADING:
            # 获取正在下载的任务
            for name, server in servers.items():
                torrents = server.get_downloading_torrents(tags=query_tags) or []
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
                        torrent_state = self.__normalize_torrent_state(torrent_info.status)
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
    def __normalize_torrent_state(status: Optional[str]) -> str:
        """
        归一 Transmission 原始任务状态。
        """
        status_text = str(status or "").strip().lower()
        if status_text in _TRANSMISSION_PAUSED_STATES:
            return DownloadTaskState.PAUSED.value
        if status_text in _TRANSMISSION_DOWNLOADING_STATES:
            return DownloadTaskState.DOWNLOADING.value
        return DownloadTaskState.COMPLETED.value

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param downloader:  下载器
        """
        # 获取下载器
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None
        # 获取原标签
        org_tags = server.get_torrent_tags(ids=hashs)
        # 种子打上已整理标签
        if org_tags:
            tags = org_tags + ['已整理']
        else:
            tags = ['已整理']
        server.set_torrent_tag(ids=hashs, tags=tags)
        return None

    def remove_torrents(self, hashs: Union[str, list], delete_file: Optional[bool] = True,
                        downloader: Optional[str] = None) -> Optional[bool]:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file:  是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None
        return server.delete_torrents(delete_file=delete_file, ids=hashs)

    def set_torrents_tag(self, hashs: Union[str, list], tags: list,
                        downloader: Optional[str] = None) -> Optional[bool]:
        """
        设置种子标签
        :param hashs:  种子Hash
        :param tags:  标签列表
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None
        # 获取原标签，TR默认会覆盖，需追加
        org_tags = server.get_torrent_tags(ids=hashs)
        return server.set_torrent_tag(ids=hashs, tags=tags, org_tags=org_tags)

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
        :param category: 分类，Transmission 不支持
        :param ratio_limit: 分享率限制
        :param seeding_time_limit: 做种时间限制，单位分钟
        :return: 各项修改结果
        """
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None
        results = {}
        if any(
                value is not None
                for value in (download_limit, upload_limit, ratio_limit, seeding_time_limit)
        ):
            change_result = server.change_torrent(
                hash_string=hash_string,
                download_limit=download_limit,
                upload_limit=upload_limit,
                ratio_limit=ratio_limit,
                seeding_time_limit=seeding_time_limit,
            )
            results["limits"] = change_result
        if save_path is not None:
            results["save_path"] = server.set_torrent_location(
                hash_string=hash_string,
                location=self.normalize_path(Path(save_path), downloader),
            )
        if tracker_list is not None:
            results["trackers"] = server.update_tracker(
                hash_string=hash_string, tracker_list=tracker_list
            )
        if category is not None:
            results["category"] = False
        return results

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
        if downloader:
            server: Transmission = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers: Dict[str, Transmission] = self.get_instances()
        ret_trackers = {}
        for name, server in servers.items():
            trackers = server.get_trackers(hash_string)
            if trackers is not None:
                ret_trackers[name] = trackers
        return ret_trackers

    def start_torrents(self, hashs: Union[list, str],
                       downloader: Optional[str] = None) -> Optional[bool]:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def stop_torrents(self, hashs: Union[list, str],
                      downloader: Optional[str] = None) -> Optional[bool]:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None
        return server.stop_torrents(ids=hashs)

    def torrent_files(self, tid: str, downloader: Optional[str] = None) -> Optional[List[File]]:
        """
        获取种子文件列表
        """
        # 获取下载器
        server: Transmission = self.get_instance(downloader)
        if not server:
            return None
        return server.get_files(tid=tid)

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        下载器信息
        """
        if downloader:
            server: Transmission = self.get_instance(downloader)
            if not server:
                return None
            servers = [server]
        else:
            servers = self.get_instances().values()
        # 调用Qbittorrent API查询实时信息
        ret_info = []
        for server in servers:
            info = server.transfer_info()
            if not info:
                continue
            ret_info.append(_SchemaDownloaderInfo(
                download_speed=info.download_speed,
                upload_speed=info.upload_speed,
                download_size=info.current_stats.downloaded_bytes,
                upload_size=info.current_stats.uploaded_bytes
            ))
        return ret_info
