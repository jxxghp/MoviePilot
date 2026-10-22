"""
Qbittorrent 下载模块，负责实例路由、任务投影与下载文件选择。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set, Tuple, Union

from app.domain import torrent as torrent_rules
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.modules._base.downloader import _DownloaderModuleBase
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.transfer import DownloaderFile, DownloaderTorrent
from app.schemas.types import (
    DownloaderType,
    DownloadTaskState,
    ModuleType,
    TorrentQueryStatus,
    TorrentStatus,
)

if TYPE_CHECKING:
    from qbittorrentapi import TorrentDictionary

_QBITTORRENT_DOWNLOADING_STATES = {
    "allocating",
    "checkingdl",
    "downloading",
    "forceddl",
    "metadl",
    "queueddl",
    "stalleddl",
}
_QBITTORRENT_PAUSED_STATES = {
    "paused",
    "pauseddl",
    "pausedup",
    "stoppeddl",
    "stoppedup",
}
# 新任务文件索引最多查询 5 次，确认可选文件后才恢复下载。
_TORRENT_FILES_RETRY_TIMES = 5
# 文件索引查询之间的等待秒数。
_TORRENT_FILES_RETRY_INTERVAL = 1


class QbittorrentModule(_DownloaderModuleBase[Qbittorrent]):
    """
    qBittorrent 下载器模块，负责下载任务添加、文件选择和任务管理。
    """

    def init_module(self) -> None:
        """
        根据系统下载器配置创建并注册本类型的客户端实例。
        """
        super().init_service(service_name=Qbittorrent.__name__.lower(),
                             service_type=Qbittorrent)

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "Qbittorrent"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Downloader

    @staticmethod
    def get_subtype() -> DownloaderType:
        """
        获取模块子类型。
        """
        return DownloaderType.Qbittorrent

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 1

    def stop(self) -> None:
        """
        保留模块停止入口；本实现没有额外的停止动作。
        """
        pass

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        由下载器实例配置控制启用状态，不声明独立的模块开关。
        """
        pass

    def download(self, content: Union[Path, str, bytes], download_dir: Path, cookie: str,
                 episodes: Optional[Set[int]] = None, category: Optional[str] = None, label: Optional[str] = None,
                 downloader: Optional[str] = None,
                 custom_words: Optional[str] = None) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        读取种子内容并添加下载任务，按需选择剧集文件。

        无论添加或后续处理是否成功，都会尝试清理随机临时标签。

        :param content: 本地种子路径、磁力链接或种子文件字节内容；普通 URL 不会由本方法下载
        :param download_dir: MoviePilot 侧的下载目录，会映射为下载器路径
        :param cookie: 传给下载器的站点 Cookie
        :param episodes: 目标集数集合；非空时以暂停状态添加并执行文件选择
        :param category: 下载器任务分类，仅在启用自动管理时传给客户端
        :param label: 逗号分隔的任务标签；省略时使用系统种子标签
        :param downloader: 下载器配置名称，省略时使用默认实例
        :param custom_words: 来源识别词，按行参与文件名识别，使选集结果与订阅一致
        :return: 下载器名称、任务哈希、内容布局和结果说明；无适用实例时返回 None，
            添加失败时前三项为空，说明包含失败原因
        """
        if not content:
            return None, None, None, "下载内容为空"

        torrent_from_file, torrent_content = self._get_torrent_info(content)
        expected_hashes = torrent_rules.download_hashes(torrent_content) if isinstance(torrent_content, (str, bytes)) else frozenset()
        is_magnet = isinstance(torrent_content, str) and torrent_content.startswith("magnet:") or isinstance(torrent_content,
                                                                                             bytes) and torrent_content.startswith(
            b"magnet:")
        if not torrent_content or (not torrent_from_file and not is_magnet):
            return None, None, None, "添加种子任务失败：无法读取种子文件"

        # 失败提示使用种子名称（磁力链接保留原文），不拼接种子文件的二进制内容
        torrent_desc = str(torrent_from_file.name if torrent_from_file else torrent_content)

        server = self.get_instance(downloader)
        if not server:
            return None

        # 生成随机Tag
        tag = text_tools.random_string(10)
        if label:
            tags = label.split(',') + [tag]
        elif get_runtime_setting('TORRENT_TAG'):
            tags = [tag, get_runtime_setting('TORRENT_TAG')]
        else:
            tags = [tag]
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        torrent_hash = None
        temporary_tag_cleaned = False
        try:
            state, added_torrent_ids = server.add_torrent(
                content=torrent_content,
                download_dir=self.normalize_path(download_dir, downloader),
                is_paused=is_paused,
                tag=tags,
                cookie=cookie,
                category=category,
                ignore_category_check=False
            )

            if not state:
                # 查询所有下载器的种子
                torrents, error = server.get_torrents()
                if error:
                    return None, None, None, "无法连接qbittorrent下载器"
                if torrents:
                    try:
                        for torrent in torrents:
                            candidate_hashes = {
                                str(torrent.get(key) or "").lower()
                                for key in ("hash", "infohash_v1", "infohash_v2")
                            }
                            if expected_hashes.intersection(candidate_hashes):
                                torrent_hash = torrent.get("hash")
                                temporary_tag_cleaned = server.delete_torrents_tag(torrent_hash, tag)
                                torrent_tags = [str(tag).strip() for tag in (torrent.get("tags") or "").split(',')]
                                logger.warn(f"下载器中已存在该种子任务：{torrent_hash} - {torrent.get('name')}")
                                if "已整理" in torrent_tags:
                                    server.remove_torrents_tag(ids=torrent_hash, tag=['已整理'])
                                if get_runtime_setting('TORRENT_TAG') and get_runtime_setting('TORRENT_TAG') not in torrent_tags:
                                    logger.info(f"给种子 {torrent_hash} 打上标签：{get_runtime_setting('TORRENT_TAG')}")
                                    server.set_torrents_tag(ids=torrent_hash, tags=[get_runtime_setting('TORRENT_TAG')])
                                # 获取种子内容布局: `Original: 原始, Subfolder: 创建子文件夹, NoSubfolder: 不创建子文件夹`
                                torrent_layout = server.get_content_layout()
                                if is_paused:
                                    return self._finish_selected_download(
                                        server, torrent_hash, episodes or set(), custom_words, downloader, torrent_layout,
                                        already_exists=True,
                                    )
                                return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, DOWNLOAD_TASK_ALREADY_EXISTS
                    finally:
                        torrents.clear()
                        del torrents
                return None, None, None, f"添加种子任务失败：{torrent_desc}"
            else:
                torrent_hash = next(iter(added_torrent_ids), None)
                if not torrent_hash:
                    torrent_hash = server.get_torrent_id_by_tag(tags=tag)
                if torrent_hash:
                    temporary_tag_cleaned = server.delete_torrents_tag(torrent_hash, tag)
                if not torrent_hash:
                    return None, None, None, f"下载任务添加成功，但获取Qbittorrent任务信息失败：{torrent_desc}"
                else:
                    # 获取种子内容布局: `Original: 原始, Subfolder: 创建子文件夹, NoSubfolder: 不创建子文件夹`
                    torrent_layout = server.get_content_layout()
                    if is_paused:
                        return self._finish_selected_download(
                            server, torrent_hash, episodes or set(), custom_words, downloader, torrent_layout,
                        )
                    else:
                        if server.is_force_resume():
                            server.torrents_set_force_start(torrent_hash)
                        return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, "添加下载成功"

        finally:
            # qBittorrent 可能在添加或查询链路中途失败，仍需清理已注册的临时标签。
            if not temporary_tag_cleaned:
                server.delete_torrents_tag(torrent_hash, tag)

    def _finish_selected_download(
            self, server: Qbittorrent, torrent_hash: str, episodes: Set[int], custom_words: Optional[str],
            downloader: Optional[str], torrent_layout: Optional[str], already_exists: bool = False,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        统一选集任务结果；已有任务先确认暂停，失败不向下载链返回成功 hash。
        """
        if already_exists and not server.stop_torrents(torrent_hash):
            return None, None, None, "已有下载任务无法暂停，未执行选集"
        selected, message = self._select_download_files(server, torrent_hash, episodes, custom_words)
        if not selected:
            return None, None, None, message
        if already_exists:
            message = DOWNLOAD_TASK_ALREADY_EXISTS
        return downloader or self.get_default_config_name(), torrent_hash, torrent_layout, message

    @staticmethod
    def _select_download_files(
            server: Qbittorrent, torrent_hash: str, episodes: Set[int], custom_words: Optional[str],
    ) -> Tuple[bool, str]:
        """
        验证全部目标集数及优先级后启动；失败不确认成功，并明确暂停或未知状态。
        """
        failure = f"选集失败，任务 {torrent_hash} 保持暂停，请在下载器检查"
        torrent_files = server.get_files(
            torrent_hash, retry=_TORRENT_FILES_RETRY_TIMES, interval=_TORRENT_FILES_RETRY_INTERVAL,
        )
        if not torrent_files:
            return False, f"{failure}：获取种子文件失败"
        wanted: list[int] = []
        unwanted: list[int] = []
        matched: set[int] = set()
        try:
            for item in torrent_files:
                meta = MetaInfo(Path(item.get("name")).name,
                                custom_words=custom_words.splitlines() if custom_words else [])
                file_episodes = set(meta.episode_list)
                if file_episodes and file_episodes.issubset(episodes):
                    wanted.append(item.get("id"))
                    matched.update(file_episodes)
                else:
                    unwanted.append(item.get("id"))
        finally:
            torrent_files.clear()
        if not set(episodes).issubset(matched):
            return False, f"{failure}：未找到目标集数 {sorted(set(episodes) - matched)}"
        if unwanted and not server.set_files(torrent_hash=torrent_hash, file_ids=unwanted, priority=0):
            return False, f"{failure}：文件优先级设置未确认"
        if not server.set_files(torrent_hash=torrent_hash, file_ids=wanted, priority=1):
            return False, f"{failure}：目标文件优先级设置未确认"
        started = (
            server.torrents_set_force_start(torrent_hash)
            if server.is_force_resume() else server.start_torrents(torrent_hash)
        )
        if not started:
            if server.stop_torrents(torrent_hash):
                return False, f"{failure}：启动任务未确认，未记录成功"
            return False, f"选集失败，任务 {torrent_hash} 启动任务未确认，请在下载器核实，未记录成功"
        return True, f"添加下载成功，已选择集数：{sorted(matched)}"

    def list_torrents(self, status: Optional[Union[TorrentStatus, TorrentQueryStatus, str]] = None,
                      hashs: Optional[Union[list[str], str]] = None,
                      downloader: Optional[str] = None,
                      include_all_tags: bool = False,
                      ) -> Optional[List[DownloaderTorrent]]:
        """
        将下载器任务转换为统一列表，并按状态和标签筛选。

        :param status: 任务查询状态；空值或未知值归一为查询全部
        :param hashs: 指定任务哈希或列表；提供时优先于状态筛选
        :param downloader: 下载器配置名称；省略时汇总本类型的所有实例
        :param include_all_tags: 是否包含未设置系统种子标签的任务，默认只查询系统标签范围
        :return: 匹配任务列表；指定实例不可用时返回 None，查询失败的实例不贡献结果
        """
        servers: Dict[Optional[str], Qbittorrent]
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

        def __get_torrent_path(torrent_data: TorrentDictionary) -> Path:
            """
            获取种子内容路径。
            """
            content_path = torrent_data.get("content_path")
            if content_path:
                return Path(content_path)
            torrent_path: Path = Path(torrent_data.get('save_path')) / torrent_data.get('name')
            return torrent_path

        def __build_torrent(downloader_name: Optional[str], torrent_data: TorrentDictionary) -> DownloaderTorrent:
            """
            构造统一下载器任务对象。
            """
            meta = MetaInfo(torrent_data.get('name'))
            torrent_path = __get_torrent_path(torrent_data)
            dlspeed = torrent_data.get('dlspeed') or 0
            total_size = torrent_data.get('total_size') or 0
            completed_size = torrent_data.get('completed') or 0
            return DownloaderTorrent(
                downloader=downloader_name,
                title=torrent_data.get('name'),
                name=meta.name,
                year=str(meta.year) if meta.year is not None else None,
                season_episode=meta.season_episode,
                path=Path(self.normalize_return_path(torrent_path, downloader_name)),
                save_path=self.normalize_return_path(
                    Path(torrent_data.get('save_path') or ""), downloader_name
                ) if torrent_data.get('save_path') else None,
                content_path=self.normalize_return_path(torrent_path, downloader_name),
                hash=torrent_data.get('hash'),
                size=total_size,
                tags=torrent_data.get('tags'),
                category=torrent_data.get('category'),
                download_limit=(torrent_data.get('dl_limit') or 0) / 1024,
                upload_limit=(torrent_data.get('up_limit') or 0) / 1024,
                ratio_limit=torrent_data.get('ratio_limit'),
                seeding_time_limit=torrent_data.get('seeding_time_limit'),
                progress=(torrent_data.get('progress') or 0) * 100,
                state=self.__normalize_torrent_state(torrent_data.get('state')),
                dlspeed=size_tools.format_compact_size(dlspeed),
                upspeed=size_tools.format_compact_size(torrent_data.get('upspeed')),
                left_time=time_tools.format_duration(
                    (total_size - completed_size) / dlspeed
                ) if dlspeed > 0 else '',
            )

        if hashs:
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
                        tags = torrent_info.get("tags") or []
                        if "已整理" in tags:
                            continue
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.DOWNLOADING:
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
                        torrent_state = self.__normalize_torrent_state(torrent_info.get('state'))
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
        # 这里返回统一模型列表，保留既有的下载器接口兼容检查豁免。
        return ret_torrents  # noqa

    @staticmethod
    def __normalize_torrent_state(state: Optional[Union[str, int]]) -> str:
        """
        归一 qBittorrent 原始任务状态。
        """
        state_text = str(state or "").strip().lower()
        if state_text in _QBITTORRENT_PAUSED_STATES:
            return DownloadTaskState.PAUSED.value
        if state_text in _QBITTORRENT_DOWNLOADING_STATES:
            return DownloadTaskState.DOWNLOADING.value
        return DownloadTaskState.COMPLETED.value

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        为整理完成的种子添加“已整理”标签。

        找不到适用实例时直接返回；标签写入失败不向调用方返回确认。

        :param hashs: 已完成整理的种子哈希
        :param downloader: 下载器配置名称，省略时使用默认实例
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        server.set_torrents_tag(ids=hashs, tags=['已整理'])
        return None

    def remove_torrents(self, hashs: Union[str, list[str]], delete_file: Optional[bool] = True,
                        downloader: Optional[str] = None) -> Optional[bool]:
        """
        删除指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param delete_file: 是否同时删除下载文件，默认删除
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.delete_torrents(delete_file=delete_file, ids=hashs)

    def set_torrents_tag(self, hashs: Union[str, list[str]], tags: list[str],
                         downloader: Optional[str] = None) -> Optional[bool]:
        """
        为指定下载任务追加标签。

        :param hashs: 种子哈希或哈希列表
        :param tags: 需要追加的标签列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端接受标签写入时返回 True，RPC失败时返回 False；找不到实例时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.set_torrents_tag(ids=hashs, tags=tags)

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
        :param downloader: 下载器配置名称，省略时使用默认实例
        :param download_limit: 下载限速，单位 KB/s；0 表示不限速，None 表示不修改
        :param upload_limit: 上传限速，单位 KB/s；0 表示不限速，None 表示不修改
        :param tracker_list: Tracker URL列表
        :param save_path: 保存目录
        :param category: 分类
        :param ratio_limit: 分享率限制
        :param seeding_time_limit: 做种时间限制，单位分钟
        :return: 各项请求的执行结果；未提供的项不写入结果，找不到适用实例时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        results = {}
        if any(
                value is not None
                for value in (download_limit, upload_limit, ratio_limit, seeding_time_limit)
        ):
            results["limits"] = server.change_torrent(
                hash_string=hash_string,
                download_limit=download_limit,
                upload_limit=upload_limit,
                ratio_limit=ratio_limit,
                seeding_time_limit=seeding_time_limit,
            )
        if tracker_list is not None:
            results["trackers"] = server.update_tracker(
                hash_string=hash_string, tracker_list=tracker_list
            )
        if save_path is not None:
            results["save_path"] = server.set_torrent_location(
                hash_string=hash_string,
                location=self.normalize_path(Path(save_path), downloader),
            )
        if category is not None:
            results["category"] = server.set_torrent_category(
                hash_string=hash_string, category=category
            )
        return results

    def get_torrent_trackers(
            self,
            hash_string: str,
            downloader: Optional[str] = None,
    ) -> Optional[Dict[Optional[str], List[str]]]:
        """
        查询指定种子在下载器中的 Tracker 地址。

        :param hash_string: 种子哈希
        :param downloader: 下载器配置名称；省略时查询本类型的所有实例
        :return: 实例名称到 Tracker 地址列表的映射；指定实例不可用时返回 None，查询失败的实例被跳过
        """
        servers: Dict[Optional[str], Qbittorrent]
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
        启动指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def stop_torrents(self, hashs: Union[list[str], str], downloader: Optional[str] = None) -> Optional[bool]:
        """
        暂停指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.stop_torrents(ids=hashs)

    def torrent_files(
        self, tid: str, downloader: Optional[str] = None
    ) -> Optional[List[DownloaderFile]]:
        """
        将下载器文件列表转换为统一的文件模型。

        :param tid: 种子哈希
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 统一文件列表；找不到适用实例或客户端没有返回文件清单时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return self._normalize_torrent_files(
            server.get_files(tid=tid), DownloaderFile.model_validate
        )

    def downloader_info(self, downloader: Optional[str] = None) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        汇总下载器的上下行速度和累计传输量。

        :param downloader: 下载器配置名称；省略时查询本类型的所有实例
        :return: 各可用实例的统计列表，速度单位为字节/秒、传输量为字节；
            指定实例不可用时返回 None，统计查询失败的实例被跳过
        """
        servers: Iterable[Qbittorrent]
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
                download_speed=info.get("dl_info_speed"),
                upload_speed=info.get("up_info_speed"),
                download_size=info.get("dl_info_data"),
                upload_size=info.get("up_info_data")
            ))
        return ret_info
