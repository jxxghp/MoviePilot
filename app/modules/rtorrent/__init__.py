"""
rTorrent 下载模块，负责实例路由、任务投影与下载文件选择。
"""

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union, cast

from app.domain import torrent as torrent_rules
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.modules._base.downloader import _DownloaderModuleBase
from app.modules.rtorrent.rtorrent import Rtorrent, TorrentData
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


class RtorrentModule(_DownloaderModuleBase[Rtorrent]):
    """
    rTorrent 下载器模块，负责任务添加、标签和文件状态转换。
    """

    def init_module(self) -> None:
        """
        根据系统下载器配置创建并注册本类型的客户端实例。
        """
        super().init_service(
            service_name=Rtorrent.__name__.lower(), service_type=Rtorrent
        )

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "Rtorrent"

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
        return DownloaderType.Rtorrent

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 3

    def stop(self) -> None:
        """
        下载器客户端由服务基类管理，本模块无额外停止动作。
        """
        pass

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        下载器实例由系统配置管理，不声明独立模块开关。
        """
        pass

    def download(
        self,
        content: Union[Path, str, bytes],
        download_dir: Path,
        cookie: str,
        episodes: Optional[Set[int]] = None,
        category: Optional[str] = None,
        label: Optional[str] = None,
        downloader: Optional[str] = None,
    ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        读取种子内容并添加下载任务，按需选择剧集文件。

        :param content: 本地种子路径、磁力链接或种子文件字节内容；普通 URL 不会由本方法下载
        :param download_dir: MoviePilot 侧的下载目录，会映射为下载器路径
        :param cookie: 传给下载器的站点 Cookie，rTorrent 客户端当前忽略此参数
        :param episodes: 目标集数集合；非空时以暂停状态添加并执行文件选择
        :param category: 下载器任务分类，当前下载器不使用
        :param label: 逗号分隔的任务标签；省略时使用系统种子标签
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 下载器名称、任务哈希、内容布局和结果说明；无适用实例时返回 None，
            添加失败时前三项为空，说明包含失败原因
        """

        if not content:
            return None, None, None, "下载内容为空"

        torrent_from_file, resolved_content = self._get_torrent_info(content)
        is_magnet = (
            isinstance(resolved_content, str)
            and resolved_content.startswith("magnet:")
            or isinstance(resolved_content, bytes)
            and resolved_content.startswith(b"magnet:")
        )
        if not torrent_from_file and not is_magnet:
            return None, None, None, "添加种子任务失败：无法读取种子文件"
        resolved_content = cast(Union[str, bytes], resolved_content)
        expected_hashes = torrent_rules.download_hashes(resolved_content)

        # 失败提示使用种子名称（磁力链接保留原文），不拼接种子文件的二进制内容
        torrent_desc = torrent_from_file.name if torrent_from_file else resolved_content
        torrent_desc_text = (
            torrent_desc.decode(errors="replace")
            if isinstance(torrent_desc, bytes)
            else str(torrent_desc)
        )

        server = self.get_instance(downloader)
        if not server:
            return None

        # 生成随机Tag
        tag = text_tools.random_string(10)
        if label:
            tags = label.split(",") + [tag]
        elif get_runtime_setting('TORRENT_TAG'):
            tags = [tag, get_runtime_setting('TORRENT_TAG')]
        else:
            tags = [tag]
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        state = server.add_torrent(
            content=resolved_content,
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
                        candidate_hash = str(torrent.get("hash") or "").lower()
                        if candidate_hash in expected_hashes:
                            torrent_hash = cast(str, torrent.get("hash"))
                            torrent_tags = [
                                str(t).strip()
                                for t in torrent.get("tags", "").split(",")
                                if t.strip()
                            ]
                            logger.warn(
                                f"下载器中已存在该种子任务：{torrent_hash} - {torrent.get('name')}"
                            )
                            if "已整理" in torrent_tags:
                                server.remove_torrents_tag(
                                    ids=torrent_hash, tag=["已整理"]
                                )
                            if (
                                get_runtime_setting('TORRENT_TAG')
                                and get_runtime_setting('TORRENT_TAG') not in torrent_tags
                            ):
                                logger.info(
                                    f"给种子 {torrent_hash} 打上标签：{get_runtime_setting('TORRENT_TAG')}"
                                )
                                server.set_torrents_tag(
                                    ids=torrent_hash, tags=[get_runtime_setting('TORRENT_TAG')]
                                )
                            return (
                                downloader or self.get_default_config_name(),
                                torrent_hash,
                                torrent_layout,
                                DOWNLOAD_TASK_ALREADY_EXISTS,
                            )
                finally:
                    torrents.clear()
                    del torrents
            return None, None, None, f"添加种子任务失败：{torrent_desc_text}"
        else:
            added_torrent_hash = server.get_torrent_id_by_tag(tags=tag)
            if not added_torrent_hash:
                return (
                    None,
                    None,
                    None,
                    f"下载任务添加成功，但获取rTorrent任务信息失败：{torrent_desc_text}",
                )
            else:
                torrent_hash = added_torrent_hash
                if episodes:
                    selected, message = self._select_episode_files(server, torrent_hash, episodes)
                    if not selected:
                        return None, None, None, message
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        message,
                    )
                else:
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        "添加下载成功",
                    )

    @staticmethod
    def _select_episode_files(server: Rtorrent, torrent_hash: str, episodes: Set[int]) -> Tuple[bool, str]:
        """
        对暂停添加的任务完成选集，所有RPC均成功才允许下载链确认任务。

        :param server: 当前下载器客户端
        :param torrent_hash: 已暂停添加的任务Hash
        :param episodes: 本次允许下载的目标集数
        :return: 是否确认选集启动成功及结果说明
        """
        torrent_files = server.get_files(torrent_hash)
        if not torrent_files:
            return False, "获取种子文件失败，下载任务保留暂停状态，请检查下载器"
        file_ids, selected_episodes = [], set()
        for torrent_file in torrent_files:
            meta = MetaInfo(torrent_file["name"])
            if meta.episode_list and set(meta.episode_list).issubset(episodes):
                selected_episodes.update(meta.episode_list)
            else:
                file_ids.append(torrent_file["id"])
        if not selected_episodes:
            return False, "未匹配到目标集数，下载任务保留暂停状态"
        if file_ids and not server.set_files(torrent_hash=torrent_hash, file_ids=file_ids, priority=0):
            return False, "设置选集优先级失败，下载任务保留暂停状态"
        if not server.start_torrents(torrent_hash):
            stopped = server.stop_torrents(torrent_hash)
            message = "启动下载失败，已请求暂停，请检查下载器确认状态" if stopped else "启动下载状态未知，请检查下载器"
            return False, message
        return True, f"添加下载成功，已选择集数：{sorted(selected_episodes)}"

    def list_torrents(
        self,
        status: Optional[TorrentStatus] = None,
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
        servers: Dict[Optional[str], Rtorrent]
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

        def __get_torrent_path(torrent_data: TorrentData) -> Path:
            """
            获取种子内容路径。
            """
            content_path = torrent_data.get("content_path")
            if content_path:
                return Path(content_path)
            return Path(torrent_data["save_path"]) / torrent_data["name"]

        def __build_torrent(downloader_name: Optional[str], torrent_data: TorrentData) -> DownloaderTorrent:
            """
            构造统一下载器任务对象。
            """
            meta = MetaInfo(torrent_data["name"])
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
                year=str(meta.year) if meta.year is not None else None,
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
        # 这里返回统一模型列表，保留既有的下载器接口兼容检查豁免。
        return ret_torrents  # noqa

    @staticmethod
    def __normalize_torrent_state(
            state: Optional[Union[int, float, str]],
            complete: Optional[Union[int, float, str]],
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
        self, hashs: Union[str, list[str]], downloader: Optional[str] = None
    ) -> None:
        """
        为整理完成的种子添加“已整理”标签。

        找不到适用实例时直接返回；标签写入失败不向调用方返回确认。

        :param hashs: 已完成整理的种子哈希
        :param downloader: 下载器配置名称，省略时使用默认实例
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        for torrent_hash in [hashs] if isinstance(hashs, str) else hashs:
            org_tags = server.get_torrent_tags(ids=torrent_hash)
            tags = org_tags + ["已整理"] if org_tags else ["已整理"]
            # 每个任务独立保留原标签，避免批量整理时覆盖其他任务的标签。
            server.set_torrents_tag(ids=torrent_hash, tags=tags, overwrite=True)
        return None

    def remove_torrents(
        self,
        hashs: Union[str, list[str]],
        delete_file: Optional[bool] = True,
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
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
        return server.delete_torrents(
            delete_file=delete_file if delete_file is not None else True,
            ids=hashs,
            path_mapper=lambda path: self.normalize_return_path(path, downloader),
        )

    def set_torrents_tag(
        self, hashs: Union[str, list[str]], tags: list[str],
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """
        为指定下载任务追加标签。

        :param hashs: 种子哈希或哈希列表
        :param tags: 需要追加的标签列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
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
        :param tracker_list: Tracker URL列表，rTorrent 当前封装不支持
        :param save_path: 保存目录
        :param category: 分类，rTorrent 当前封装不支持
        :param ratio_limit: 分享率限制，rTorrent 当前封装不支持
        :param seeding_time_limit: 做种时间限制，rTorrent 当前封装不支持
        :return: 各项请求的执行结果；未提供的项不写入结果，找不到适用实例时返回 None
        """
        server = self.get_instance(downloader)
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
        self, hashs: Union[list[str], str], downloader: Optional[str] = None
    ) -> Optional[bool]:
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

    def stop_torrents(
        self, hashs: Union[list[str], str], downloader: Optional[str] = None
    ) -> Optional[bool]:
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

    def downloader_info(
        self, downloader: Optional[str] = None
    ) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        汇总下载器的上下行速度和累计传输量。

        :param downloader: 下载器配置名称；省略时查询本类型的所有实例
        :return: 各可用实例的统计列表，速度单位为字节/秒、传输量为字节；
            指定实例不可用时返回 None，统计查询失败的实例被跳过
        """
        servers: list[Rtorrent]
        if downloader:
            server = self.get_instance(downloader)
            if not server:
                return None
            servers = [server]
        else:
            servers = list(self.get_instances().values())
        ret_info = []
        for server in servers:
            info = server.transfer_info()
            if not info:
                continue
            download_speed = info.get("dl_info_speed")
            upload_speed = info.get("up_info_speed")
            download_size = info.get("dl_info_data")
            upload_size = info.get("up_info_data")
            ret_info.append(
                _SchemaDownloaderInfo(
                    download_speed=float(download_speed) if isinstance(download_speed, (int, float)) else None,
                    upload_speed=float(upload_speed) if isinstance(upload_speed, (int, float)) else None,
                    download_size=float(download_size) if isinstance(download_size, (int, float)) else None,
                    upload_size=float(upload_size) if isinstance(upload_size, (int, float)) else None,
                )
            )
        return ret_info
