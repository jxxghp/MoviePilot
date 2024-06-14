import shutil
from pathlib import Path
from typing import Set, Tuple, Optional, Union, List

from qbittorrentapi import TorrentFilesList
from torrentool.torrent import Torrent

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

    @staticmethod
    def get_name() -> str:
        return "Qbittorrent"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        if self.qbittorrent.is_inactive():
            self.qbittorrent.reconnect()
        if not self.qbittorrent.transfer_info():
            return False, "无法获取Qbittorrent状态，请检查参数配置"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "DOWNLOADER", "qbittorrent"

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        if self.qbittorrent.is_inactive():
            self.qbittorrent.reconnect()

    def download(self, content: Union[Path, str], download_dir: Path, cookie: str,
                 episodes: Set[int] = None, category: str = None,
                 downloader: str = settings.DEFAULT_DOWNLOADER) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  分类
        :param downloader:  下载器
        :return: 种子Hash，错误信息
        """

        def __get_torrent_info() -> Tuple[str, int]:
            """
            获取种子名称
            """
            try:
                if isinstance(content, Path):
                    torrentinfo = Torrent.from_file(content)
                else:
                    torrentinfo = Torrent.from_string(content)
                return torrentinfo.name, torrentinfo.total_size
            except Exception as e:
                logger.error(f"获取种子名称失败：{e}")
                return "", 0

        # 不是默认下载器不处理
        if downloader != "qbittorrent":
            return None

        if not content:
            return None
        if isinstance(content, Path) and not content.exists():
            return None, f"种子文件不存在：{content}"

        # 生成随机Tag
        tag = StringUtils.generate_random_str(10)
        if settings.TORRENT_TAG:
            tags = [tag, settings.TORRENT_TAG]
        else:
            tags = [tag]
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        # 添加任务
        state = self.qbittorrent.add_torrent(
            content=content.read_bytes() if isinstance(content, Path) else content,
            download_dir=str(download_dir),
            is_paused=is_paused,
            tag=tags,
            cookie=cookie,
            category=category
        )
        if not state:
            # 读取种子的名称
            torrent_name, torrent_size = __get_torrent_info()
            if not torrent_name:
                return None, f"添加种子任务失败：无法读取种子文件"
            # 查询所有下载器的种子
            torrents, error = self.qbittorrent.get_torrents()
            if error:
                return None, "无法连接qbittorrent下载器"
            if torrents:
                for torrent in torrents:
                    # 名称与大小相等则认为是同一个种子
                    if torrent.get("name") == torrent_name and torrent.get("total_size") == torrent_size:
                        torrent_hash = torrent.get("hash")
                        torrent_tags = [str(tag).strip() for tag in torrent.get("tags").split(',')]
                        logger.warn(f"下载器中已存在该种子任务：{torrent_hash} - {torrent.get('name')}")
                        # 给种子打上标签
                        if "已整理" in torrent_tags:
                            self.qbittorrent.remove_torrents_tag(ids=torrent_hash, tag=['已整理'])
                        if settings.TORRENT_TAG and settings.TORRENT_TAG not in torrent_tags:
                            logger.info(f"给种子 {torrent_hash} 打上标签：{settings.TORRENT_TAG}")
                            self.qbittorrent.set_torrents_tag(ids=torrent_hash, tags=[settings.TORRENT_TAG])
                        return torrent_hash, f"下载任务已存在"
            return None, f"添加种子任务失败：{content}"
        else:
            # 获取种子Hash
            torrent_hash = self.qbittorrent.get_torrent_id_by_tag(tags=tag)
            if not torrent_hash:
                return None, f"下载任务添加成功，但获取Qbittorrent任务信息失败：{content}"
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
                    if settings.QB_FORCE_RESUME:
                        # 强制继续
                        self.qbittorrent.torrents_set_force_start(torrent_hash)
                    else:
                        self.qbittorrent.start_torrents(torrent_hash)
                    return torrent_hash, f"添加下载成功，已选择集数：{sucess_epidised}"
                else:
                    if settings.QB_FORCE_RESUME:
                        self.qbittorrent.torrents_set_force_start(torrent_hash)
                    return torrent_hash, "添加下载成功"

    def list_torrents(self, status: TorrentStatus = None,
                      hashs: Union[list, str] = None,
                      downloader: str = settings.DEFAULT_DOWNLOADER
                      ) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: 下载器中符合状态的种子列表
        """
        if downloader != "qbittorrent":
            return None
        ret_torrents = []
        if hashs:
            # 按Hash获取
            torrents, _ = self.qbittorrent.get_torrents(ids=hashs, tags=settings.TORRENT_TAG)
            for torrent in torrents or []:
                content_path = torrent.get("content_path")
                if content_path:
                    torrent_path = Path(content_path)
                else:
                    torrent_path = torrent.get('save_path') / torrent.get('name')
                ret_torrents.append(TransferTorrent(
                    title=torrent.get('name'),
                    path=torrent_path,
                    hash=torrent.get('hash'),
                    size=torrent.get('total_size'),
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
                    torrent_path = torrent.get('save_path') / torrent.get('name')
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
                    state="paused" if torrent.get('state') in ("paused", "pausedDL") else "downloading",
                    dlspeed=StringUtils.str_filesize(torrent.get('dlspeed')),
                    upspeed=StringUtils.str_filesize(torrent.get('upspeed')),
                    left_time=StringUtils.str_secends(
                        (torrent.get('total_size') - torrent.get('completed')) / torrent.get('dlspeed')) if torrent.get(
                        'dlspeed') > 0 else ''
                ))
        else:
            return None
        return ret_torrents

    def transfer_completed(self, hashs: str, path: Path = None,
                           downloader: str = settings.DEFAULT_DOWNLOADER) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param path:  源目录
        :param downloader:  下载器
        """
        if downloader != "qbittorrent":
            return
        self.qbittorrent.set_torrents_tag(ids=hashs, tags=['已整理'])
        # 移动模式删除种子
        if settings.TRANSFER_TYPE in ["move", "rclone_move"]:
            if self.remove_torrents(hashs):
                logger.info(f"移动模式删除种子成功：{hashs} ")
            # 删除残留文件
            if path and path.exists():
                files = SystemUtils.list_files(path, settings.RMT_MEDIAEXT)
                if not files:
                    logger.warn(f"删除残留文件夹：{path}")
                    shutil.rmtree(path, ignore_errors=True)

    def remove_torrents(self, hashs: Union[str, list], delete_file: bool = True,
                        downloader: str = settings.DEFAULT_DOWNLOADER) -> Optional[bool]:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file:  是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        if downloader != "qbittorrent":
            return None
        return self.qbittorrent.delete_torrents(delete_file=delete_file, ids=hashs)

    def start_torrents(self, hashs: Union[list, str],
                       downloader: str = settings.DEFAULT_DOWNLOADER) -> Optional[bool]:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        if downloader != "qbittorrent":
            return None
        return self.qbittorrent.start_torrents(ids=hashs)

    def stop_torrents(self, hashs: Union[list, str], downloader: str = settings.DEFAULT_DOWNLOADER) -> Optional[bool]:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        if downloader != "qbittorrent":
            return None
        return self.qbittorrent.stop_torrents(ids=hashs)

    def torrent_files(self, tid: str, downloader: str = settings.DEFAULT_DOWNLOADER) -> Optional[TorrentFilesList]:
        """
        获取种子文件列表
        """
        if downloader != "qbittorrent":
            return None
        return self.qbittorrent.get_files(tid=tid)

    def downloader_info(self) -> [schemas.DownloaderInfo]:
        """
        下载器信息
        """
        # 调用Qbittorrent API查询实时信息
        info = self.qbittorrent.transfer_info()
        if not info:
            return [schemas.DownloaderInfo()]
        return [schemas.DownloaderInfo(
            download_speed=info.get("dl_info_speed"),
            upload_speed=info.get("up_info_speed"),
            download_size=info.get("dl_info_data"),
            upload_size=info.get("up_info_data")
        )]
