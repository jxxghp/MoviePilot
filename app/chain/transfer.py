import json
import shutil
import threading
from pathlib import Path
from typing import List, Optional, Tuple, Union

from sqlalchemy.orm import Session

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.progress import ProgressHelper
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent, Notification, EpisodeFormat
from app.schemas.types import TorrentStatus, EventType, MediaType, ProgressKey, NotificationType, MessageChannel
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class TransferChain(ChainBase):
    """
    文件转移处理链
    """

    def __init__(self, db: Session = None):
        super().__init__(db)
        self.downloadhis = DownloadHistoryOper(self._db)
        self.transferhis = TransferHistoryOper(self._db)
        self.progress = ProgressHelper()
        self.mediachain = MediaChain(self._db)

    def process(self) -> bool:
        """
        获取下载器中的种子列表，并执行转移
        """

        # 全局锁，避免重复处理
        with lock:
            logger.info("开始执行下载器文件转移 ...")
            # 从下载器获取种子列表
            torrents: Optional[List[TransferTorrent]] = self.list_torrents(status=TorrentStatus.TRANSFER)
            if not torrents:
                logger.info("没有获取到已完成的下载任务")
                return False

            logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")

            for torrent in torrents:
                # 识别元数据
                meta: MetaBase = MetaInfo(title=torrent.title)
                if not meta.name:
                    logger.error(f'未识别到元数据，标题：{torrent.title}')
                    continue

                # 查询下载记录识别情况
                downloadhis: DownloadHistory = self.downloadhis.get_by_hash(torrent.hash)
                if downloadhis:
                    # 类型
                    mtype = MediaType(downloadhis.type)
                    # 补充剧集信息
                    if mtype == MediaType.TV \
                            and ((not meta.season_list and downloadhis.seasons)
                                 or (not meta.episode_list and downloadhis.episodes)):
                        meta = MetaInfo(f"{torrent.title} {downloadhis.seasons} {downloadhis.episodes}")
                    # 按TMDBID识别
                    mediainfo = self.recognize_media(mtype=mtype,
                                                     tmdbid=downloadhis.tmdbid)
                else:
                    mediainfo = self.recognize_media(meta=meta)

                if not mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{torrent.title}')
                    # 新增转移失败历史记录
                    his = self.__insert_fail_history(
                        src_path=torrent.path,
                        download_hash=torrent.hash,
                        meta=meta
                    )
                    self.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{torrent.title} 未识别到媒体信息，无法入库！\n"
                              f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别转移。"
                    ))
                    # 设置种子状态，避免一直报错
                    self.transfer_completed(hashs=torrent.hash)
                    continue

                logger.info(f"{torrent.title} 识别为：{mediainfo.type.value} {mediainfo.title_year}")

                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)

                # 获取待转移路径清单
                trans_paths = self.__get_trans_paths(torrent.path)
                if not trans_paths:
                    logger.warn(f"{torrent.title} 对应目录没有找到媒体文件")
                    continue

                # 转移所有文件
                for trans_path in trans_paths:
                    transferinfo: TransferInfo = self.transfer(mediainfo=mediainfo,
                                                               path=trans_path,
                                                               transfer_type=settings.TRANSFER_TYPE)
                    if not transferinfo:
                        logger.error("文件转移模块运行失败")
                        continue
                    if not transferinfo.target_path:
                        # 转移失败
                        logger.warn(f"{torrent.title} 入库失败：{transferinfo.message}")
                        # 新增转移失败历史记录
                        self.__insert_fail_history(
                            src_path=trans_path,
                            download_hash=torrent.hash,
                            meta=meta,
                            mediainfo=mediainfo,
                            transferinfo=transferinfo
                        )
                        # 发送消息
                        self.post_message(Notification(
                            title=f"{mediainfo.title_year} {meta.season_episode} 入库失败！",
                            text=f"原因：{transferinfo.message or '未知'}",
                            image=mediainfo.get_message_image()
                        ))
                        continue

                    # 新增转移成功历史记录
                    self.__insert_sucess_history(
                        src_path=trans_path,
                        download_hash=torrent.hash,
                        meta=meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo
                    )
                    # 刮削元数据
                    self.scrape_metadata(path=transferinfo.target_path, mediainfo=mediainfo)
                    # 刷新媒体库
                    self.refresh_mediaserver(mediainfo=mediainfo, file_path=transferinfo.target_path)
                    # 发送通知
                    self.send_transfer_message(meta=meta, mediainfo=mediainfo, transferinfo=transferinfo)
                    # 广播事件
                    self.eventmanager.send_event(EventType.TransferComplete, {
                        'meta': meta,
                        'mediainfo': mediainfo,
                        'transferinfo': transferinfo
                    })

                # 转移完成
                self.transfer_completed(hashs=torrent.hash, transinfo=transferinfo)
            # 结束
            logger.info("下载器文件转移执行完成")
            return True

    @staticmethod
    def __get_trans_paths(directory: Path):
        """
        获取转移目录列表
        """

        if not directory.exists():
            logger.warn(f"目录不存在：{directory}")
            return []

        # 单文件
        if directory.is_file():
            return [directory]

        # 蓝光原盘
        if SystemUtils.is_bluray_dir(directory):
            return [directory]

        # 需要转移的路径列表
        trans_paths = []

        # 先检查当前目录的下级目录，以支持合集的情况
        for sub_dir in SystemUtils.list_sub_directory(directory):
            # 没有媒体文件的目录跳过
            if not SystemUtils.list_files(sub_dir, extensions=settings.RMT_MEDIAEXT):
                continue
            trans_paths.append(sub_dir)

        if not trans_paths:
            # 没有有效子目录，直接转移当前目录
            trans_paths.append(directory)
        else:
            # 有子目录时，把当前目录的文件添加到转移任务中
            trans_paths.extend(
                SystemUtils.list_sub_files(directory, extensions=settings.RMT_MEDIAEXT)
            )
        return trans_paths

    def remote_transfer(self, arg_str: str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        远程重新转移，参数 历史记录ID TMDBID|类型
        """

        def args_error():
            self.post_message(Notification(channel=channel,
                                           title="请输入正确的命令格式：/redo [id] [tmdbid]|[类型]，"
                                                 "[id]历史记录编号", userid=userid))

        if not arg_str:
            args_error()
            return
        arg_strs = str(arg_str).split()
        if len(arg_strs) != 2:
            args_error()
            return
        # 历史记录ID
        logid = arg_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # TMDB ID
        tmdb_strs = arg_strs[1].split('|')
        tmdbid = tmdb_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # 类型
        type_str = tmdb_strs[1] if len(tmdb_strs) > 1 else None
        if not type_str or type_str not in [MediaType.MOVIE.value, MediaType.TV.value]:
            args_error()
            return
        state, errmsg = self.re_transfer(logid=int(logid),
                                         mtype=MediaType(type_str), tmdbid=int(tmdbid))
        if not state:
            self.post_message(Notification(channel=channel, title="手动整理失败",
                                           text=errmsg, userid=userid))
            return

    def re_transfer(self, logid: int, mtype: MediaType, tmdbid: int) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别转移，只处理对应的src目录
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param tmdbid: TMDB ID
        """
        # 查询历史记录
        history: TransferHistory = self.transferhis.get(logid)
        if not history:
            logger.error(f"历史记录不存在，ID：{logid}")
            return False, "历史记录不存在"
        # 没有下载记录，按源目录路径重新转移
        src_path = Path(history.src)
        if not src_path.exists():
            return False, f"源目录不存在：{src_path}"
        # 识别元数据
        meta = MetaInfo(title=src_path.stem)
        if not meta.name:
            return False, f"未识别到元数据，标题：{src_path.stem}"
        # 查询媒体信息
        mediainfo = self.recognize_media(mtype=mtype, tmdbid=tmdbid)
        if not mediainfo:
            return False, f"未识别到媒体信息，类型：{mtype.value}，tmdbid：{tmdbid}"
        # 重新执行转移
        logger.info(f"{mtype.value} {tmdbid} 识别为：{mediainfo.title_year}")
        # 更新媒体图片
        self.obtain_images(mediainfo=mediainfo)

        # 转移
        transferinfo: TransferInfo = self.transfer(mediainfo=mediainfo,
                                                   path=src_path,
                                                   transfer_type=settings.TRANSFER_TYPE)
        if not transferinfo:
            logger.error("文件转移模块运行失败")
            return False, "文件转移模块运行失败"

        # 删除旧历史记录
        self.transferhis.delete(logid)

        if not transferinfo.target_path:
            # 转移失败
            logger.warn(f"{src_path} 入库失败：{transferinfo.message}")
            # 新增转移失败历史记录
            self.__insert_fail_history(
                src_path=src_path,
                download_hash=history.download_hash,
                meta=meta,
                mediainfo=mediainfo,
                transferinfo=transferinfo
            )
            return False, transferinfo.message

        # 新增转移成功历史记录
        self.__insert_sucess_history(
            src_path=src_path,
            download_hash=history.download_hash,
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo
        )
        # 刮削元数据
        self.scrape_metadata(path=transferinfo.target_path, mediainfo=mediainfo)
        # 刷新媒体库
        self.refresh_mediaserver(mediainfo=mediainfo, file_path=transferinfo.target_path)
        # 发送通知
        self.send_transfer_message(meta=meta, mediainfo=mediainfo, transferinfo=transferinfo)
        # 广播事件
        self.eventmanager.send_event(EventType.TransferComplete, {
            'meta': meta,
            'mediainfo': mediainfo,
            'transferinfo': transferinfo
        })

        return True, ""

    def manual_transfer(self, in_path: Path,
                        target: Path = None,
                        tmdbid: int = None,
                        mtype: MediaType = None,
                        season: int = None,
                        transfer_type: str = None,
                        epformat: EpisodeFormat = None,
                        min_filesize: int = 0) -> Tuple[bool, Union[str, list]]:
        """
        手动转移
        :param in_path: 源文件路径
        :param target: 目标路径
        :param tmdbid: TMDB ID
        :param mtype: 媒体类型
        :param season: 季度
        :param transfer_type: 转移类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        """
        logger.info(f"手动转移：{in_path} ...")

        # 默认转移类型
        if not transfer_type:
            transfer_type = settings.TRANSFER_TYPE

        if tmdbid:
            # 有输入TMDBID时单个识别
            meta = MetaInfo(in_path.stem)
            # 整合数据
            if mtype:
                meta.type = mtype
            if season is not None:
                meta.begin_season = season
            # 识别媒体信息
            mediainfo: MediaInfo = self.mediachain.recognize_media(tmdbid=tmdbid, mtype=mtype)
            if not mediainfo:
                return False, f"媒体信息识别失败，tmdbid: {tmdbid}, type: {mtype.value}"
            # 开始进度
            self.progress.start(ProgressKey.FileTransfer)
            self.progress.update(value=0,
                                 text=f"开始转移 {in_path} ...",
                                 key=ProgressKey.FileTransfer)
            # 开始转移
            transferinfo: TransferInfo = self.transfer(
                path=in_path,
                mediainfo=mediainfo,
                transfer_type=transfer_type,
                target=target,
                epformat=epformat,
                min_filesize=min_filesize
            )
            if not transferinfo:
                return False, "文件转移模块运行失败"
            if not transferinfo.target_path:
                return False, transferinfo.message

            # 新增转移成功历史记录
            self.__insert_sucess_history(
                src_path=in_path,
                meta=meta,
                mediainfo=mediainfo,
                transferinfo=transferinfo
            )
            # 刮削元数据
            self.scrape_metadata(path=transferinfo.target_path, mediainfo=mediainfo)
            # 刷新媒体库
            self.refresh_mediaserver(mediainfo=mediainfo, file_path=transferinfo.target_path)
            # 发送通知
            self.send_transfer_message(meta=meta, mediainfo=mediainfo, transferinfo=transferinfo)
            # 广播事件
            self.eventmanager.send_event(EventType.TransferComplete, {
                'meta': meta,
                'mediainfo': mediainfo,
                'transferinfo': transferinfo
            })
            self.progress.end(ProgressKey.FileTransfer)
            logger.info(f"{in_path} 转移完成")
            return True, ""
        else:
            # 错误信息
            errmsgs = []
            # 自动识别所有文件
            transfer_files = SystemUtils.list_files(directory=in_path,
                                                    extensions=settings.RMT_MEDIAEXT,
                                                    min_filesize=min_filesize)
            if not transfer_files:
                return False, "没有找到可转移的文件"
            # 开始进度
            self.progress.start(ProgressKey.FileTransfer)
            # 总数
            total_num = len(transfer_files)
            # 已处理数量
            processed_num = 0
            self.progress.update(value=0,
                                 text=f"开始转移 {in_path}，共 {total_num} 个文件 ...",
                                 key=ProgressKey.FileTransfer)
            for transfer_file in transfer_files:
                # 更新进度
                self.progress.update(value=processed_num / total_num * 100,
                                     text=f"正在转移 {transfer_file.name} ...",
                                     key=ProgressKey.FileTransfer)
                # 上级目录元数据
                meta = MetaInfo(title=transfer_file.parent.name)
                # 文件元数据，不包含后缀
                file_meta = MetaInfo(title=transfer_file.stem)
                # 合并元数据
                file_meta.merge(meta)

                if not file_meta.name:
                    logger.error(f"{transfer_file} 无法识别有效信息")
                    errmsgs.append(f"{transfer_file.name} 无法识别有效信息")
                    # 更新进度
                    processed_num += 1
                    self.progress.update(value=processed_num / total_num * 100,
                                         text=f"{transfer_file.name} 无法识别有效信息",
                                         key=ProgressKey.FileTransfer)
                    continue
                # 整合数据
                if mtype:
                    file_meta.type = mtype
                if season:
                    file_meta.begin_season = season
                # 识别媒体信息
                mediainfo: MediaInfo = self.mediachain.recognize_media(meta=file_meta)
                if not mediainfo:
                    logger.error(f"{transfer_file} 媒体信息识别失败")
                    errmsgs.append(f"{transfer_file.name} 媒体信息识别失败")
                    # 更新进度
                    processed_num += 1
                    self.progress.update(value=processed_num / total_num * 100,
                                         text=f"{transfer_file.name} 媒体信息识别失败！",
                                         key=ProgressKey.FileTransfer)
                    continue
                # 开始转移
                transferinfo: TransferInfo = self.transfer(
                    path=in_path,
                    mediainfo=mediainfo,
                    transfer_type=transfer_type,
                    target=target,
                    meta=file_meta,
                    epformat=epformat,
                    min_filesize=min_filesize
                )
                if not transferinfo:
                    return False, "文件转移模块运行失败"
                if not transferinfo.target_path:
                    logger.error(f"{transfer_file} 转移失败：{transferinfo.message}")
                    errmsgs.append(f"{transfer_file.name} 转移失败：{transferinfo.message}")
                    # 更新进度
                    processed_num += 1
                    self.progress.update(value=processed_num / total_num * 100,
                                         text=f"{transfer_file.name} 转移失败：{transferinfo.message}",
                                         key=ProgressKey.FileTransfer)
                    continue

                # 新增转移成功历史记录
                self.__insert_sucess_history(
                    src_path=transfer_file,
                    meta=file_meta,
                    mediainfo=mediainfo,
                    transferinfo=transferinfo
                )
                # 刮削元数据
                self.scrape_metadata(path=transferinfo.target_path, mediainfo=mediainfo)
                # 刷新媒体库
                self.refresh_mediaserver(mediainfo=mediainfo, file_path=transferinfo.target_path)
                # 发送通知
                self.send_transfer_message(meta=file_meta, mediainfo=mediainfo, transferinfo=transferinfo)
                # 广播事件
                self.eventmanager.send_event(EventType.TransferComplete, {
                    'meta': file_meta,
                    'mediainfo': mediainfo,
                    'transferinfo': transferinfo
                })
                # 更新进度
                processed_num += 1
                self.progress.update(value=processed_num / total_num * 100,
                                     text=f"{transfer_file.name} 转移完成",
                                     key=ProgressKey.FileTransfer)
            # 结束进度
            logger.info(f"转移完成，共 {total_num} 个文件，成功 {total_num - len(errmsgs)} 个，失败 {len(errmsgs)} 个")
            self.progress.end(ProgressKey.FileTransfer)
            if errmsgs:
                return False, errmsgs
            return True, ""

    def __insert_sucess_history(self, src_path: Path, meta: MetaBase,
                                mediainfo: MediaInfo, transferinfo: TransferInfo,
                                download_hash: str = None):
        """
        新增转移成功历史记录
        """
        self.transferhis.add(
            src=str(src_path),
            dest=str(transferinfo.target_path),
            mode=settings.TRANSFER_TYPE,
            type=mediainfo.type.value,
            category=mediainfo.category,
            title=mediainfo.title,
            year=mediainfo.year,
            tmdbid=mediainfo.tmdb_id,
            imdbid=mediainfo.imdb_id,
            tvdbid=mediainfo.tvdb_id,
            doubanid=mediainfo.douban_id,
            seasons=meta.season,
            episodes=meta.episode,
            image=mediainfo.get_poster_image(),
            download_hash=download_hash,
            status=1,
            files=json.dumps(transferinfo.file_list)
        )

    def __insert_fail_history(self, src_path: Path, download_hash: str, meta: MetaBase,
                              transferinfo: TransferInfo = None, mediainfo: MediaInfo = None):
        """
        新增转移失败历史记录，不能按download_hash判重
        """
        if mediainfo and transferinfo:
            his = self.transferhis.add(
                src=str(src_path),
                dest=str(transferinfo.target_path),
                mode=settings.TRANSFER_TYPE,
                type=mediainfo.type.value,
                category=mediainfo.category,
                title=mediainfo.title or meta.name,
                year=mediainfo.year or meta.year,
                tmdbid=mediainfo.tmdb_id,
                imdbid=mediainfo.imdb_id,
                tvdbid=mediainfo.tvdb_id,
                doubanid=mediainfo.douban_id,
                seasons=meta.season,
                episodes=meta.episode,
                image=mediainfo.get_poster_image(),
                download_hash=download_hash,
                status=0,
                errmsg=transferinfo.message or '未知错误',
                files=json.dumps(transferinfo.file_list)
            )
        else:
            his = self.transferhis.add(
                title=meta.name,
                year=meta.year,
                src=str(src_path),
                mode=settings.TRANSFER_TYPE,
                seasons=meta.season,
                episodes=meta.episode,
                download_hash=download_hash,
                status=0,
                errmsg="未识别到媒体信息"
            )
        return his

    def send_transfer_message(self, meta: MetaBase, mediainfo: MediaInfo, transferinfo: TransferInfo,
                              season_episode: str = None):
        """
        发送入库成功的消息
        """
        msg_title = f"{mediainfo.title_year} {meta.season_episode if not season_episode else season_episode} 已入库"
        if mediainfo.vote_average:
            msg_str = f"评分：{mediainfo.vote_average}，类型：{mediainfo.type.value}"
        else:
            msg_str = f"类型：{mediainfo.type.value}"
        if mediainfo.category:
            msg_str = f"{msg_str}，类别：{mediainfo.category}"
        if meta.resource_term:
            msg_str = f"{msg_str}，质量：{meta.resource_term}"
        msg_str = f"{msg_str}，共{transferinfo.file_count}个文件，" \
                  f"大小：{StringUtils.str_filesize(transferinfo.total_size)}"
        if transferinfo.message:
            msg_str = f"{msg_str}，以下文件处理失败：\n{transferinfo.message}"
        # 发送
        self.post_message(Notification(
            mtype=NotificationType.Organize,
            title=msg_title, text=msg_str, image=mediainfo.get_message_image()))

    @staticmethod
    def delete_files(path: Path):
        """
        删除转移后的文件以及空目录
        """
        logger.info(f"开始删除文件以及空目录：{path} ...")
        if not path.exists():
            logger.error(f"{path} 不存在")
            return
        elif path.is_file():
            # 删除文件
            path.unlink()
            logger.warn(f"文件 {path} 已删除")
            # 判断目录是否为空, 为空则删除
            if str(path.parent.parent) != str(path.root):
                # 父目录非根目录，才删除父目录
                files = SystemUtils.list_files(path.parent, settings.RMT_MEDIAEXT)
                if not files:
                    shutil.rmtree(path.parent)
                    logger.warn(f"目录 {path.parent} 已删除")
        else:
            if str(path.parent) != str(path.root):
                # 父目录非根目录，才删除目录
                shutil.rmtree(path)
                # 删除目录
                logger.warn(f"目录 {path} 已删除")
