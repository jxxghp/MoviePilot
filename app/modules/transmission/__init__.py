import shutil
from pathlib import Path
from typing import Set, Tuple, Optional, Union, List

from transmission_rpc import File

from app import schemas
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.transmission.transmission import Transmission
from app.schemas import TransferInfo, TransferTorrent, DownloadingTorrent
from app.schemas.types import TorrentStatus
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class TransmissionModule(_ModuleBase):
    transmission: Transmission = None

    def init_module(self) -> None:
        self.transmission = Transmission()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "DOWNLOADER", "transmission"

    def download(self, torrent_path: Path, download_dir: Path, cookie: str,
                 episodes: Set[int] = None) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param torrent_path:  种子文件地址
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :return: 种子Hash
        """
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        # 标签
        if settings.TORRENT_TAG:
            labels = [settings.TORRENT_TAG]
        else:
            labels = None
        # 添加任务
        torrent = self.transmission.add_torrent(content=torrent_path.read_bytes(),
                                                download_dir=str(download_dir),
                                                is_paused=is_paused,
                                                labels=labels,
                                                cookie=cookie)
        if not torrent:
            return None, f"添加种子任务失败：{torrent_path}"
        else:
            torrent_hash = torrent.hashString
            if is_paused:
                # 选择文件
                torrent_files = self.transmission.get_files(torrent_hash)
                if not torrent_files:
                    return torrent_hash, "获取种子文件失败，下载任务可能在暂停状态"
                # 需要的文件信息
                file_ids = []
                for torrent_file in torrent_files:
                    file_id = torrent_file.id
                    file_name = torrent_file.name
                    meta_info = MetaInfo(file_name)
                    if not meta_info.episode_list:
                        continue
                    selected = set(meta_info.episode_list).issubset(set(episodes))
                    if not selected:
                        continue
                    file_ids.append(file_id)
                # 选择文件
                self.transmission.set_files(torrent_hash, file_ids)
                # 开始任务
                self.transmission.start_torrents(torrent_hash)
            else:
                return torrent_hash, "添加下载任务成功"

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
            torrents, _ = self.transmission.get_torrents(ids=hashs, tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                ret_torrents.append(TransferTorrent(
                    title=torrent.name,
                    path=Path(torrent.download_dir) / torrent.name,
                    hash=torrent.hashString,
                    tags=torrent.labels
                ))
        elif status == TorrentStatus.TRANSFER:
            # 获取已完成且未整理的
            torrents = self.transmission.get_completed_torrents(tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                # 含"已整理"tag的不处理
                if "已整理" in torrent.labels or []:
                    continue
                # 下载路径
                path = torrent.download_dir
                # 无法获取下载路径的不处理
                if not path:
                    logger.debug(f"未获取到 {torrent.name} 下载保存路径")
                    continue
                ret_torrents.append(TransferTorrent(
                    title=torrent.name,
                    path=Path(torrent.download_dir) / torrent.name,
                    hash=torrent.hashString,
                    tags=torrent.labels
                ))
        elif status == TorrentStatus.DOWNLOADING:
            # 获取正在下载的任务
            torrents = self.transmission.get_downloading_torrents(tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                meta = MetaInfo(torrent.name)
                dlspeed = torrent.rate_download if hasattr(torrent, "rate_download") else torrent.rateDownload
                upspeed = torrent.rate_upload if hasattr(torrent, "rate_upload") else torrent.rateUpload
                ret_torrents.append(DownloadingTorrent(
                    hash=torrent.hashString,
                    title=torrent.name,
                    name=meta.name,
                    year=meta.year,
                    season_episode=meta.season_episode,
                    progress=torrent.progress,
                    size=torrent.total_size,
                    state="paused" if torrent.status == "stopped" else "downloading",
                    dlspeed=StringUtils.str_filesize(dlspeed),
                    upspeed=StringUtils.str_filesize(upspeed),
                ))
        else:
            return None
        return ret_torrents

    def transfer_completed(self, hashs: Union[str, list],
                           transinfo: TransferInfo = None) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param transinfo:  转移信息
        :return: None
        """
        self.transmission.set_torrent_tag(ids=hashs, tags=['已整理'])
        # 移动模式删除种子
        if settings.TRANSFER_TYPE == "move":
            if self.remove_torrents(hashs):
                logger.info(f"移动模式删除种子成功：{hashs} ")
            # 删除残留文件
            if transinfo and transinfo.path and transinfo.path.exists():
                files = SystemUtils.list_files(transinfo.path, settings.RMT_MEDIAEXT)
                if not files:
                    logger.warn(f"删除残留文件夹：{transinfo.path}")
                    shutil.rmtree(transinfo.path, ignore_errors=True)

    def remove_torrents(self, hashs: Union[str, list]) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :return: bool
        """
        return self.transmission.delete_torrents(delete_file=True, ids=hashs)

    def start_torrents(self, hashs: Union[list, str]) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :return: bool
        """
        return self.transmission.start_torrents(ids=hashs)

    def stop_torrents(self, hashs: Union[list, str]) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :return: bool
        """
        return self.transmission.start_torrents(ids=hashs)

    def torrent_files(self, tid: str) -> Optional[List[File]]:
        """
        获取种子文件列表
        """
        return self.transmission.get_files(tid=tid)

    def downloader_info(self) -> schemas.DownloaderInfo:
        """
        下载器信息
        """
        info = self.transmission.transfer_info()
        return schemas.DownloaderInfo(
            download_speed=info.download_speed,
            upload_speed=info.upload_speed,
            download_size=info.current_stats.downloaded_bytes,
            upload_size=info.current_stats.uploaded_bytes
        )
