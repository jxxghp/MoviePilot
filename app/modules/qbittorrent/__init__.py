import shutil
from pathlib import Path
from typing import Set, Tuple, Optional, Union, List

from qbittorrentapi import TorrentFilesList

from app import schemas
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.schemas import TransferTorrent, DownloadingTorrent
from app.schemas.types import TorrentStatus
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class QbittorrentModule(_ModuleBase):
    qbittorrent: Qbittorrent = None

    def init_module(self) -> None:
        self.qbittorrent = Qbittorrent()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "DOWNLOADER", "qbittorrent"

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        if not self.qbittorrent.qbc:
            self.qbittorrent = Qbittorrent()

    def download(self, torrent_path: Path, download_dir: Path, cookie: str,
                 episodes: Set[int] = None) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param torrent_path:  种子文件地址
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :return: 种子Hash，错误信息
        """
        if not torrent_path or not torrent_path.exists():
            return None, f"种子文件不存在：{torrent_path}"
        # 生成随机Tag
        tag = StringUtils.generate_random_str(10)
        if settings.TORRENT_TAG:
            tags = [tag, settings.TORRENT_TAG]
        else:
            tags = [tag]
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        # 添加任务
        state = self.qbittorrent.add_torrent(content=torrent_path.read_bytes(),
                                             download_dir=str(download_dir),
                                             is_paused=is_paused,
                                             tag=tags,
                                             cookie=cookie)
        if not state:
            return None, f"添加种子任务失败：{torrent_path}"
        else:
            # 获取种子Hash
            torrent_hash = self.qbittorrent.get_torrent_id_by_tag(tags=tag)
            if not torrent_hash:
                return None, f"获取种子Hash失败：{torrent_path}"
            else:
                if is_paused:
                    # 种子文件
                    torrent_files = self.qbittorrent.get_files(torrent_hash)
                    if not torrent_files:
                        return torrent_hash, "获取种子文件失败，下载任务可能在暂停状态"

                    # 不需要的文件ID
                    file_ids = []
                    # 需要的集清单
                    sucess_epidised = []

                    for torrent_file in torrent_files:
                        file_id = torrent_file.get("id")
                        file_name = torrent_file.get("name")
                        meta_info = MetaInfo(file_name)
                        if not meta_info.episode_list \
                                or not set(meta_info.episode_list).issubset(episodes):
                            file_ids.append(file_id)
                        else:
                            sucess_epidised = list(set(sucess_epidised).union(set(meta_info.episode_list)))
                    if sucess_epidised and file_ids:
                        # 选择文件
                        self.qbittorrent.set_files(torrent_hash=torrent_hash, file_ids=file_ids, priority=0)
                    # 开始任务
                    self.qbittorrent.start_torrents(torrent_hash)
                    return torrent_hash, f"添加下载成功，已选择集数：{sucess_epidised}"
                else:
                    return torrent_hash, "添加下载成功"

    def list_torrents(self, status: TorrentStatus = None,
                      hashs: Union[list, str] = None) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :return: 下载器中符合状态的种子列表
        """
        ret_torrents = []
        if hashs:
            # 按Hash获取
            torrents, _ = self.qbittorrent.get_torrents(ids=hashs, tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                content_path = torrent.get("content_path")
                if content_path:
                    torrent_path = Path(content_path)
                else:
                    torrent_path = Path(settings.DOWNLOAD_PATH) / torrent.get('name')
                ret_torrents.append(TransferTorrent(
                    title=torrent.get('name'),
                    path=torrent_path,
                    hash=torrent.get('hash'),
                    tags=torrent.get('tags')
                ))
        elif status == TorrentStatus.TRANSFER:
            # 获取已完成且未整理的
            torrents = self.qbittorrent.get_completed_torrents(tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                tags = torrent.get("tags") or []
                if "已整理" in tags:
                    continue
                # 内容路径
                content_path = torrent.get("content_path")
                if content_path:
                    torrent_path = Path(content_path)
                else:
                    torrent_path = Path(settings.DOWNLOAD_PATH) / torrent.get('name')
                ret_torrents.append(TransferTorrent(
                    title=torrent.get('name'),
                    path=torrent_path,
                    hash=torrent.get('hash'),
                    tags=torrent.get('tags')
                ))
        elif status == TorrentStatus.DOWNLOADING:
            # 获取正在下载的任务
            torrents = self.qbittorrent.get_downloading_torrents(tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                meta = MetaInfo(torrent.get('name'))
                ret_torrents.append(DownloadingTorrent(
                    hash=torrent.get('hash'),
                    title=torrent.get('name'),
                    name=meta.name,
                    year=meta.year,
                    season_episode=meta.season_episode,
                    progress=torrent.get('progress') * 100,
                    size=torrent.get('total_size'),
                    state="paused" if torrent.get('state') == "paused" else "downloading",
                    dlspeed=StringUtils.str_filesize(torrent.get('dlspeed')),
                    upspeed=StringUtils.str_filesize(torrent.get('upspeed')),
                ))
        else:
            return None
        return ret_torrents

    def transfer_completed(self, hashs: Union[str, list],
                           path: Path = None) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param path:  源目录
        """
        self.qbittorrent.set_torrents_tag(ids=hashs, tags=['已整理'])
        # 移动模式删除种子
        if settings.TRANSFER_TYPE == "move":
            if self.remove_torrents(hashs):
                logger.info(f"移动模式删除种子成功：{hashs} ")
            # 删除残留文件
            if path and path.exists():
                files = SystemUtils.list_files(path, settings.RMT_MEDIAEXT)
                if not files:
                    logger.warn(f"删除残留文件夹：{path}")
                    shutil.rmtree(path, ignore_errors=True)

    def remove_torrents(self, hashs: Union[str, list]) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :return: bool
        """
        return self.qbittorrent.delete_torrents(delete_file=True, ids=hashs)

    def start_torrents(self, hashs: Union[list, str]) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :return: bool
        """
        return self.qbittorrent.start_torrents(ids=hashs)

    def stop_torrents(self, hashs: Union[list, str]) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :return: bool
        """
        return self.qbittorrent.start_torrents(ids=hashs)

    def torrent_files(self, tid: str) -> Optional[TorrentFilesList]:
        """
        获取种子文件列表
        """
        return self.qbittorrent.get_files(tid=tid)

    def downloader_info(self) -> schemas.DownloaderInfo:
        """
        下载器信息
        """
        # 调用Qbittorrent API查询实时信息
        info = self.qbittorrent.transfer_info()
        return schemas.DownloaderInfo(
            download_speed=info.get("dl_info_speed"),
            upload_speed=info.get("up_info_speed"),
            download_size=info.get("dl_info_data"),
            upload_size=info.get("up_info_data")
        )
