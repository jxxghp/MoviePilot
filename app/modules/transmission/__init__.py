from pathlib import Path
from typing import Set, Tuple, Optional, Union, List

from app.core import settings, MetaInfo
from app.modules import _ModuleBase
from app.modules.transmission.transmission import Transmission
from app.utils.types import TorrentStatus


class TransmissionModule(_ModuleBase):

    transmission: Transmission = None

    def init_module(self) -> None:
        self.transmission = Transmission()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "DOWNLOADER", "transmission"

    def download(self, torrent_path: Path, cookie: str,
                 episodes: Set[int] = None) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param torrent_path:  种子文件地址
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
                                                download_dir=settings.DOWNLOAD_PATH,
                                                is_paused=is_paused,
                                                labels=labels,
                                                cookie=cookie)
        if not torrent:
            return None, f"添加种子任务失败：{torrent_path}"
        else:
            torrent_hash = torrent.hashString
            torrent_id = torrent.id
            if is_paused:
                # 选择文件
                torrent_files = self.transmission.get_files(torrent_hash)
                if not torrent_files:
                    return torrent_hash, "获取种子文件失败，下载任务可能在暂停状态"

                # 需要的文件信息
                files_info = {}
                # 需要的集清单
                sucess_epidised = []

                for torrent_file in torrent_files:
                    file_id = torrent_file.id
                    file_name = torrent_file.name
                    meta_info = MetaInfo(file_name)
                    if not meta_info.get_episode_list():
                        selected = False
                    else:
                        selected = set(meta_info.get_episode_list()).issubset(set(episodes))
                        if selected:
                            sucess_epidised = list(set(sucess_epidised).union(set(meta_info.get_episode_list())))
                    if not files_info.get(torrent_id):
                        files_info[torrent_id] = {file_id: {'priority': 'normal', 'selected': selected}}
                    else:
                        files_info[torrent_id][file_id] = {'priority': 'normal', 'selected': selected}
                if sucess_epidised and files_info:
                    self.transmission.set_files(file_info=files_info)
                # 开始任务
                self.transmission.start_torrents(torrent_hash)
            else:
                return torrent_hash, "添加下载任务成功"

    def transfer_completed(self, hashs: Union[str, list]) -> bool:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :return: 处理状态
        """
        return self.transmission.set_torrent_tag(ids=hashs, tag=['已整理'])

    def list_torrents(self, status: TorrentStatus) -> Optional[List[dict]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :return: 下载器中符合状态的种子列表
        """
        if status == TorrentStatus.TRANSFER:
            torrents = self.transmission.get_transfer_torrents(tag=settings.TORRENT_TAG)
        else:
            return None
        return torrents

    def remove_torrents(self, hashs: Union[str, list]) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :return: bool
        """
        return self.transmission.delete_torrents(delete_file=True, ids=hashs)
