from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Literal, Optional, Set, Tuple, Union, overload

from app.domain import torrent as torrent_rules
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.modules._base.downloader import _DownloaderModuleBase
from app.modules.transmission.transmission import Transmission
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.transfer import DownloaderFile, DownloaderTorrent
from app.schemas.types import (
    DownloaderType,
    DownloadTaskState,
    ModuleType,
    TorrentQueryStatus,
    TorrentStatus,
)

if TYPE_CHECKING:
    from transmission_rpc import Torrent

# 只描述 SDK 已公开的数值字段及旧版别名；不转换原始返回值。
_IntTorrentAttribute = Literal[
    "total_size", "totalSize", "rate_download", "rateDownload", "rate_upload", "rateUpload",
    "left_until_done", "leftUntilDone", "seed_idle_limit", "seedIdleLimit",
    "download_limit", "downloadLimit", "upload_limit", "uploadLimit",
]
_FloatTorrentAttribute = Literal["progress", "percent_done", "seed_ratio_limit", "seedRatioLimit"]

_TRANSMISSION_DOWNLOADING_STATES = {
    "download pending",
    "downloading",
}
_TRANSMISSION_PAUSED_STATES = {
    "stopped",
}


@overload
def _get_torrent_attr(torrent_data: Torrent, *attr_names: _IntTorrentAttribute) -> Optional[int]:
    """读取 SDK 整数计数或限额，字段均不可用时返回空值。"""
    ...

@overload
def _get_torrent_attr(torrent_data: Torrent, *attr_names: _FloatTorrentAttribute) -> Optional[float]:
    """读取 SDK 比例或进度，字段均不可用时返回空值。"""
    ...

def _get_torrent_attr(
    torrent_data: Torrent, *attr_names: Union[_IntTorrentAttribute, _FloatTorrentAttribute],
) -> Optional[Union[int, float]]:
    """
    兼容 transmission-rpc 新旧字段名。
    """
    for attr_name in attr_names:
        try:
            value: Optional[Union[int, float]] = getattr(torrent_data, attr_name)
            return value
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return None


def _normalize_transmission_progress(
    torrent_data: Torrent, attr_getter: Callable[[Torrent, _FloatTorrentAttribute], Optional[float]]
) -> float:
    """读取 Transmission 任务进度并统一为百分比。"""
    progress = attr_getter(torrent_data, "progress")
    if progress is not None:
        return float(progress or 0)
    percent_done = attr_getter(torrent_data, "percent_done")
    if percent_done is None:
        return 0
    # transmission-rpc 的 percent_done 原始值范围是 0..1；
    # 兼容旧适配器时仍可能收到已经归一化的百分比。
    percent_value = float(percent_done)
    return percent_value * 100 if 0 <= percent_value <= 1 else percent_value


class TransmissionModule(_DownloaderModuleBase[Transmission]):
    """Transmission 下载器模块，负责任务添加、标签和文件选择。"""

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=Transmission.__name__.lower(),
                             service_type=Transmission)

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "Transmission"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Downloader

    @staticmethod
    def get_subtype() -> DownloaderType:
        """
        获取模块子类型
        """
        return DownloaderType.Transmission

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 2

    def stop(self) -> None:
        """下载器客户端由服务基类管理，本模块无额外停止动作。"""
        pass

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """下载器实例由系统配置管理，不声明独立模块开关。"""
        pass

    def download(self, content: Union[Path, str, bytes], download_dir: Path, cookie: str,
                 episodes: Optional[Set[int]] = None, category: Optional[str] = None, label: Optional[str] = None,
                 downloader: Optional[str] = None,
                 custom_words: Optional[str] = None) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接或种子内容
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  分类，TR中未使用
        :param label:  标签
        :param downloader:  下载器
        :param custom_words: 来源识别词，保持订阅与下载器文件选择的集数一致
        :return: 下载器名称、种子Hash、种子文件布局、错误原因
        """

        if not content:
            return None, None, None, "下载内容为空"

        # 读取种子的名称
        torrent_from_file, torrent_content = self._get_torrent_info(content)
        expected_hashes = torrent_rules.download_hashes(torrent_content) if isinstance(torrent_content, (str, bytes)) else frozenset()
        # 检查是否为磁力链接
        is_magnet = isinstance(torrent_content, str) and torrent_content.startswith("magnet:") or isinstance(torrent_content,
                                                                                             bytes) and torrent_content.startswith(
            b"magnet:")
        if not torrent_content or (not torrent_from_file and not is_magnet):
            return None, None, None, "添加种子任务失败：无法读取种子文件"

        # 失败提示使用种子名称（磁力链接保留原文），不拼接种子文件的二进制内容
        torrent_desc = str(torrent_from_file.name if torrent_from_file else torrent_content)

        # 获取下载器
        server = self.get_instance(downloader)
        if not server:
            return None

        # 如果要选择文件则先暂停
        is_paused = True if episodes else False

        # 标签
        if label:
            labels = label.split(',')
        elif get_runtime_setting('TORRENT_TAG'):
            labels = get_runtime_setting('TORRENT_TAG').split(',')
        else:
            labels = None
        # 添加任务
        added_torrent = server.add_torrent(
            content=torrent_content,
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
                        candidate_hashes = {
                            str(getattr(torrent, key, None) or "").lower()
                            for key in ("hashString", "hash_string", "infohash_v1", "infohash_v2")
                        }
                        if expected_hashes.intersection(candidate_hashes):
                            torrent_hash = torrent.hashString
                            logger.warn(f"下载器中已存在该种子任务：{torrent_hash} - {torrent.name}")
                            # 给种子打上标签
                            if get_runtime_setting('TORRENT_TAG'):
                                logger.info(f"给种子 {torrent_hash} 打上标签：{get_runtime_setting('TORRENT_TAG')}")
                                # 种子标签
                                labels = [str(tag).strip()
                                          for tag in torrent.labels] if hasattr(torrent, "labels") else []
                                if "已整理" in labels:
                                    labels.remove("已整理")
                                    server.set_torrent_tag(ids=torrent_hash, tags=labels)
                                if get_runtime_setting('TORRENT_TAG') and get_runtime_setting('TORRENT_TAG') not in labels:
                                    labels.append(get_runtime_setting('TORRENT_TAG'))
                                    server.set_torrent_tag(ids=torrent_hash, tags=labels)
                            if is_paused:
                                selected, message = self._select_download_files(
                                    server, torrent_hash, episodes or set(), custom_words,
                                )
                                if not selected:
                                    return None, None, None, message
                            return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, "下载任务已存在"
                finally:
                    torrents.clear()
                    del torrents
            return None, None, None, f"添加种子任务失败：{torrent_desc}"
        else:
            torrent_hash = added_torrent.hashString
            if is_paused:
                selected, message = self._select_download_files(
                    server, torrent_hash, episodes or set(), custom_words,
                )
                if not selected:
                    return None, None, None, message
                return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, "添加下载任务成功"
            else:
                return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, "添加下载任务成功"

    @staticmethod
    def _select_download_files(
            server: Transmission, torrent_hash: str, episodes: Set[int], custom_words: Optional[str],
    ) -> Tuple[bool, str]:
        """先确认任务暂停再按来源集数映射选集，兼容 RPC 返回正在运行的重复任务。"""
        if not server.stop_torrents(torrent_hash):
            return False, f"选集失败，任务 {torrent_hash} 无法确认暂停，未执行选集，请在下载器核实"
        failure = f"选集失败，任务 {torrent_hash} 保持暂停，请在下载器检查"
        torrent_files = server.get_files(torrent_hash)
        if not torrent_files:
            return False, f"{failure}：获取种子文件失败"
        wanted: list[int] = []
        unwanted: list[int] = []
        matched: set[int] = set()
        try:
            for item in torrent_files:
                meta = MetaInfo(Path(item.name).name, custom_words=custom_words.splitlines() if custom_words else [])
                file_episodes = set(meta.episode_list)
                if file_episodes and file_episodes.issubset(episodes):
                    wanted.append(item.id)
                    matched.update(file_episodes)
                else:
                    unwanted.append(item.id)
        finally:
            torrent_files.clear()
        if not set(episodes).issubset(matched):
            return False, f"{failure}：未找到目标集数 {sorted(set(episodes) - matched)}"
        if not server.set_files(torrent_hash, wanted) or not server.set_unwanted_files(torrent_hash, unwanted):
            return False, f"{failure}：文件选择设置未确认"
        if not server.start_torrents(torrent_hash):
            if server.stop_torrents(torrent_hash):
                return False, f"{failure}：启动任务未确认，未记录成功"
            return False, f"选集失败，任务 {torrent_hash} 启动任务未确认，请在下载器核实，未记录成功"
        return True, "添加下载任务成功"

    def list_torrents(self, status: Optional[Union[TorrentStatus, TorrentQueryStatus, str]] = None,
                      hashs: Optional[Union[list[str], str]] = None,
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
        servers: Dict[Optional[str], Transmission]
        if downloader:
            server = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers = self.get_instances()
        ret_torrents = []
        query_status = self._normalize_query_status(status)
        query_tags = None if include_all_tags else get_runtime_setting('TORRENT_TAG')

        def __get_torrent_size(torrent_data: Torrent) -> int:
            """
            获取任务大小。
            """
            return _get_torrent_attr(torrent_data, "total_size", "totalSize") or 0

        def __get_torrent_labels(torrent_data: Torrent) -> str:
            """
            获取任务标签。
            """
            return ",".join(getattr(torrent_data, "labels", None) or [])

        def __get_torrent_path(torrent_data: Torrent) -> Path:
            """
            获取任务内容路径。
            """
            torrent_path: Path = Path(torrent_data.download_dir) / torrent_data.name
            return torrent_path

        def __build_torrent(downloader_name: Optional[str], torrent_data: Torrent) -> DownloaderTorrent:
            """
            构造统一下载器任务对象。
            """
            meta = MetaInfo(torrent_data.name)
            dlspeed = _get_torrent_attr(
                torrent_data, "rate_download", "rateDownload"
            ) or 0
            upspeed = _get_torrent_attr(
                torrent_data, "rate_upload", "rateUpload"
            ) or 0
            left_until_done = _get_torrent_attr(
                torrent_data, "left_until_done", "leftUntilDone"
            ) or 0
            torrent_path = __get_torrent_path(torrent_data)
            ratio_limit = _get_torrent_attr(torrent_data, "seed_ratio_limit", "seedRatioLimit")
            seeding_time_limit = _get_torrent_attr(torrent_data, "seed_idle_limit", "seedIdleLimit")
            return DownloaderTorrent(
                downloader=downloader_name,
                hash=torrent_data.hashString,
                title=torrent_data.name,
                name=meta.name,
                year=str(meta.year) if meta.year is not None else None,
                season_episode=meta.season_episode,
                path=Path(self.normalize_return_path(torrent_path, downloader_name)),
                save_path=self.normalize_return_path(
                    Path(torrent_data.download_dir), downloader_name
                ) if getattr(torrent_data, "download_dir", None) else None,
                content_path=self.normalize_return_path(torrent_path, downloader_name),
                progress=_normalize_transmission_progress(torrent_data, _get_torrent_attr),
                size=__get_torrent_size(torrent_data),
                state=self.__normalize_torrent_state(torrent_data.status),
                dlspeed=size_tools.format_compact_size(dlspeed),
                upspeed=size_tools.format_compact_size(upspeed),
                tags=__get_torrent_labels(torrent_data),
                download_limit=_get_torrent_attr(torrent_data, "download_limit", "downloadLimit"),
                upload_limit=_get_torrent_attr(torrent_data, "upload_limit", "uploadLimit"),
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
        server = self.get_instance(downloader)
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

    def remove_torrents(self, hashs: Union[str, list[str]], delete_file: Optional[bool] = True,
                        downloader: Optional[str] = None) -> Optional[bool]:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file:  是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.delete_torrents(delete_file=delete_file, ids=hashs)

    def set_torrents_tag(self, hashs: Union[str, list[str]], tags: list[str],
                         downloader: Optional[str] = None) -> Optional[bool]:
        """
        设置种子标签
        :param hashs:  种子Hash
        :param tags:  标签列表
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server = self.get_instance(downloader)
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
            tracker_list: Optional[list[str]] = None,
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
        server = self.get_instance(downloader)
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
    ) -> Optional[Dict[Optional[str], List[str]]]:
        """
        查询下载任务Tracker列表。
        :param hash_string: 种子Hash
        :param downloader: 下载器
        :return: 下载器名称到Tracker列表的映射
        """
        servers: Dict[Optional[str], Transmission]
        if downloader:
            server = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers = self.get_instances()
        ret_trackers = {}
        for name, server in servers.items():
            trackers = server.get_trackers(hash_string)
            if trackers is not None:
                ret_trackers[name] = trackers
        return ret_trackers

    def start_torrents(self, hashs: Union[list[str], str],
                       downloader: Optional[str] = None) -> Optional[bool]:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def stop_torrents(self, hashs: Union[list[str], str],
                      downloader: Optional[str] = None) -> Optional[bool]:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        # 获取下载器
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.stop_torrents(ids=hashs)

    def torrent_files(
        self, tid: str, downloader: Optional[str] = None
    ) -> Optional[List[DownloaderFile]]:
        """
        获取种子文件列表，并在模块边界隔离 Transmission SDK 对象。
        """
        # 获取下载器
        server = self.get_instance(downloader)
        if not server:
            return None
        return self._normalize_torrent_files(
            server.get_files(tid=tid), DownloaderFile.model_validate
        )

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        下载器信息
        """
        servers: Iterable[Transmission]
        if downloader:
            server = self.get_instance(downloader)
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
