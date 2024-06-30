import re
import shutil
import threading
from pathlib import Path
from typing import List, Optional, Tuple, Union, Dict

from app import schemas
from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath, MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.aliyun import AliyunHelper
from app.helper.directory import DirectoryHelper
from app.helper.format import FormatParser
from app.helper.progress import ProgressHelper
from app.helper.u115 import U115Helper
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent, Notification, EpisodeFormat
from app.schemas.types import TorrentStatus, EventType, MediaType, ProgressKey, NotificationType, MessageChannel, \
    SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class TransferChain(ChainBase):
    """
    文件转移处理链
    """

    def __init__(self):
        super().__init__()
        self.downloadhis = DownloadHistoryOper()
        self.transferhis = TransferHistoryOper()
        self.progress = ProgressHelper()
        self.mediachain = MediaChain()
        self.tmdbchain = TmdbChain()
        self.systemconfig = SystemConfigOper()
        self.directoryhelper = DirectoryHelper()
        self.all_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIO_TRACK_EXT

    def recommend_name(self, meta: MetaBase, mediainfo: MediaInfo) -> Optional[str]:
        """
        获取重命名后的名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :return: 重命名后的名称（含目录）
        """
        return self.run_module("recommend_name", meta=meta, mediainfo=mediainfo)

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
                # 查询下载记录识别情况
                downloadhis: DownloadHistory = self.downloadhis.get_by_hash(torrent.hash)
                if downloadhis:
                    # 类型
                    try:
                        mtype = MediaType(downloadhis.type)
                    except ValueError:
                        mtype = MediaType.TV
                    # 按TMDBID识别
                    mediainfo = self.recognize_media(mtype=mtype,
                                                     tmdbid=downloadhis.tmdbid,
                                                     doubanid=downloadhis.doubanid)
                    if mediainfo:
                        # 补充图片
                        self.obtain_images(mediainfo)
                else:
                    # 非MoviePilot下载的任务，按文件识别
                    mediainfo = None

                # 执行转移
                self.__do_transfer(storage="local", path=torrent.path,
                                   mediainfo=mediainfo, download_hash=torrent.hash)

                # 设置下载任务状态
                self.transfer_completed(hashs=torrent.hash, path=torrent.path)
            # 结束
            logger.info("下载器文件转移执行完成")
            return True

    def __do_transfer(self, storage: str, path: Path, drive_id: str = None, fileid: str = None, filetype: str = None,
                      meta: MetaBase = None, mediainfo: MediaInfo = None,
                      download_hash: str = None,
                      target: Path = None, transfer_type: str = None,
                      season: int = None, epformat: EpisodeFormat = None,
                      min_filesize: int = 0, scrape: bool = None,
                      force: bool = False) -> Tuple[bool, str]:
        """
        执行一个复杂目录的转移操作
        :param storage: 存储器
        :param path: 待转移目录或文件
        :param drive_id: 网盘ID
        :param fileid: 文件ID
        :param filetype: 文件类型
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param download_hash: 下载记录hash
        :param target: 目标路径
        :param transfer_type: 转移类型
        :param season: 季
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param force: 是否强制转移
        返回：成功标识，错误信息
        """
        if not transfer_type:
            transfer_type = settings.TRANSFER_TYPE

        # 自定义格式
        formaterHandler = FormatParser(eformat=epformat.format,
                                       details=epformat.detail,
                                       part=epformat.part,
                                       offset=epformat.offset) if epformat else None

        # 整理屏蔽词
        transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)

        # 开始进度
        self.progress.start(ProgressKey.FileTransfer)

        # 本地存储
        if storage == "local":
            # 本地整理
            result = self.__transfer_local(path=path, meta=meta, mediainfo=mediainfo,
                                           formaterHandler=formaterHandler,
                                           transfer_exclude_words=transfer_exclude_words,
                                           min_filesize=min_filesize, transfer_type=transfer_type,
                                           target=target, season=season, scrape=scrape,
                                           download_hash=download_hash, force=force)
        else:
            # 网盘整理
            result = self.__transfer_online(storage=storage,
                                            fileitem=schemas.FileItem(
                                                path=str(path) + ("/" if filetype == "dir" else ""),
                                                type=filetype,
                                                drive_id=drive_id,
                                                fileid=fileid,
                                                name=path.name
                                            ),
                                            meta=meta,
                                            mediainfo=mediainfo)
            if result and result[0] and scrape:
                # 刮削元数据
                self.progress.update(value=0,
                                     text=f"正在刮削 {path} ...",
                                     key=ProgressKey.FileTransfer)
                self.mediachain.manual_scrape(storage=storage,
                                              fileitem=schemas.FileItem(
                                                  path=str(path) + ("/" if filetype == "dir" else ""),
                                                  type=filetype,
                                                  drive_id=drive_id,
                                                  fileid=fileid,
                                                  name=path.name
                                              ),
                                              meta=meta,
                                              mediainfo=mediainfo)
        # 结速进度
        self.progress.end(ProgressKey.FileTransfer)
        return result

    def __transfer_local(self, path: Path, meta: MetaBase = None, mediainfo: MediaInfo = None,
                         formaterHandler: FormatParser = None, transfer_exclude_words: List[str] = None,
                         min_filesize: int = 0, transfer_type: str = None, target: Path = None,
                         season: int = None, scrape: bool = None, download_hash: str = None,
                         force: bool = False) -> Tuple[bool, str]:
        """
        整理一个本地目录
        """

        # 汇总错误信息
        err_msgs: List[str] = []
        # 已处理数量
        processed_num = 0
        # 失败数量
        fail_num = 0
        # 跳过数量
        skip_num = 0

        # 获取待转移路径清单
        trans_paths = self.__get_trans_paths(path)
        if not trans_paths:
            logger.warn(f"{path.name} 没有找到可转移的媒体文件")
            return False, f"{path.name} 没有找到可转移的媒体文件"
        # 目录所有文件清单
        transfer_files = SystemUtils.list_files(directory=path,
                                                extensions=settings.RMT_MEDIAEXT,
                                                min_filesize=min_filesize)
        if formaterHandler:
            # 有集自定义格式，过滤文件
            transfer_files = [f for f in transfer_files if formaterHandler.match(f.name)]

        # 总文件数
        total_num = len(transfer_files)
        self.progress.update(value=0,
                             text=f"开始转移 {path}，共 {total_num} 个文件 ...",
                             key=ProgressKey.FileTransfer)

        # 处理所有待转移目录或文件，默认一个转移路径或文件只有一个媒体信息
        for trans_path in trans_paths:
            # 汇总季集清单
            season_episodes: Dict[Tuple, List[int]] = {}
            # 汇总元数据
            metas: Dict[Tuple, MetaBase] = {}
            # 汇总媒体信息
            medias: Dict[Tuple, MediaInfo] = {}
            # 汇总转移信息
            transfers: Dict[Tuple, TransferInfo] = {}

            # 如果是目录且不是⼀蓝光原盘，获取所有文件并转移
            if (not trans_path.is_file()
                    and not SystemUtils.is_bluray_dir(trans_path)):
                # 遍历获取下载目录所有文件
                file_paths = SystemUtils.list_files(directory=trans_path,
                                                    extensions=settings.RMT_MEDIAEXT,
                                                    min_filesize=min_filesize)
            else:
                file_paths = [trans_path]

            if formaterHandler:
                # 有集自定义格式，过滤文件
                file_paths = [f for f in file_paths if formaterHandler.match(f.name)]

            # 转移所有文件
            for file_path in file_paths:
                # 回收站及隐藏的文件不处理
                file_path_str = str(file_path)
                if file_path_str.find('/@Recycle/') != -1 \
                        or file_path_str.find('/#recycle/') != -1 \
                        or file_path_str.find('/.') != -1 \
                        or file_path_str.find('/@eaDir') != -1:
                    logger.debug(f"{file_path_str} 是回收站或隐藏的文件")
                    # 计数
                    processed_num += 1
                    skip_num += 1
                    continue

                # 整理屏蔽词不处理
                is_blocked = False
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(r"%s" % keyword, file_path_str, re.IGNORECASE):
                            logger.info(f"{file_path} 命中整理屏蔽词 {keyword}，不处理")
                            is_blocked = True
                            break
                if is_blocked:
                    err_msgs.append(f"{file_path.name} 命中整理屏蔽词")
                    # 计数
                    processed_num += 1
                    skip_num += 1
                    continue

                # 转移成功的不再处理
                if not force:
                    transferd = self.transferhis.get_by_src(file_path_str)
                    if transferd and transferd.status:
                        logger.info(f"{file_path} 已成功转移过，如需重新处理，请删除历史记录。")
                        # 计数
                        processed_num += 1
                        skip_num += 1
                        continue

                # 更新进度
                self.progress.update(value=processed_num / total_num * 100,
                                     text=f"正在转移 （{processed_num + 1}/{total_num}）{file_path.name} ...",
                                     key=ProgressKey.FileTransfer)

                if not meta:
                    # 文件元数据
                    file_meta = MetaInfoPath(file_path)
                else:
                    file_meta = meta

                # 合并季
                if season is not None:
                    file_meta.begin_season = season

                if not file_meta:
                    logger.error(f"{file_path} 无法识别有效信息")
                    err_msgs.append(f"{file_path} 无法识别有效信息")
                    # 计数
                    processed_num += 1
                    fail_num += 1
                    continue

                # 自定义识别
                if formaterHandler:
                    # 开始集、结束集、PART
                    begin_ep, end_ep, part = formaterHandler.split_episode(file_path.name)
                    if begin_ep is not None:
                        file_meta.begin_episode = begin_ep
                        file_meta.part = part
                    if end_ep is not None:
                        file_meta.end_episode = end_ep

                if not mediainfo:
                    # 识别媒体信息
                    file_mediainfo = self.mediachain.recognize_by_meta(file_meta)
                else:
                    file_mediainfo = mediainfo

                if not file_mediainfo:
                    logger.warn(f'{file_path} 未识别到媒体信息')
                    # 新增转移失败历史记录
                    his = self.transferhis.add_fail(
                        src_path=file_path,
                        mode=transfer_type,
                        meta=file_meta,
                        download_hash=download_hash
                    )
                    self.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{file_path.name} 未识别到媒体信息，无法入库！",
                        text=f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别转移。",
                        link=settings.MP_DOMAIN('#/history')
                    ))
                    # 计数
                    processed_num += 1
                    fail_num += 1
                    continue

                # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
                if not settings.SCRAP_FOLLOW_TMDB:
                    transfer_history = self.transferhis.get_by_type_tmdbid(tmdbid=file_mediainfo.tmdb_id,
                                                                           mtype=file_mediainfo.type.value)
                    if transfer_history:
                        file_mediainfo.title = transfer_history.title

                logger.info(f"{file_path.name} 识别为：{file_mediainfo.type.value} {file_mediainfo.title_year}")

                # 获取集数据
                if file_mediainfo.type == MediaType.TV:
                    if file_meta.begin_season is None:
                        file_meta.begin_season = 1
                    file_mediainfo.season = file_mediainfo.season or file_meta.begin_season
                    episodes_info = self.tmdbchain.tmdb_episodes(
                        tmdbid=file_mediainfo.tmdb_id,
                        season=file_mediainfo.season
                    )
                else:
                    episodes_info = None

                # 获取下载hash
                if not download_hash:
                    download_file = self.downloadhis.get_file_by_fullpath(file_path_str)
                    if download_file:
                        download_hash = download_file.download_hash

                # 执行转移
                transferinfo: TransferInfo = self.transfer(meta=file_meta,
                                                           mediainfo=file_mediainfo,
                                                           path=file_path,
                                                           transfer_type=transfer_type,
                                                           target=target,
                                                           episodes_info=episodes_info,
                                                           scrape=scrape)
                if not transferinfo:
                    logger.error("文件转移模块运行失败")
                    return False, "文件转移模块运行失败"
                if not transferinfo.success:
                    # 转移失败
                    logger.warn(f"{file_path.name} 入库失败：{transferinfo.message}")
                    err_msgs.append(f"{file_path.name} {transferinfo.message}")
                    # 新增转移失败历史记录
                    self.transferhis.add_fail(
                        src_path=file_path,
                        mode=transfer_type,
                        download_hash=download_hash,
                        meta=file_meta,
                        mediainfo=file_mediainfo,
                        transferinfo=transferinfo
                    )
                    # 发送消息
                    self.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{file_mediainfo.title_year} {file_meta.season_episode} 入库失败！",
                        text=f"原因：{transferinfo.message or '未知'}",
                        image=file_mediainfo.get_message_image(),
                        link=settings.MP_DOMAIN('#/history')
                    ))
                    # 计数
                    processed_num += 1
                    fail_num += 1
                    continue

                # 汇总信息
                mkey = (file_mediainfo.tmdb_id, file_meta.begin_season)
                if mkey not in medias:
                    # 新增信息
                    metas[mkey] = file_meta
                    medias[mkey] = file_mediainfo
                    season_episodes[mkey] = file_meta.episode_list
                    transfers[mkey] = transferinfo
                else:
                    # 合并季集清单
                    season_episodes[mkey] = list(set(season_episodes[mkey] + file_meta.episode_list))
                    # 合并转移数据
                    transfers[mkey].file_count += transferinfo.file_count
                    transfers[mkey].total_size += transferinfo.total_size
                    transfers[mkey].file_list.extend(transferinfo.file_list)
                    transfers[mkey].file_list_new.extend(transferinfo.file_list_new)
                    transfers[mkey].fail_list.extend(transferinfo.fail_list)

                # 新增转移成功历史记录
                self.transferhis.add_success(
                    src_path=file_path,
                    mode=transfer_type,
                    download_hash=download_hash,
                    meta=file_meta,
                    mediainfo=file_mediainfo,
                    transferinfo=transferinfo
                )
                # 刮削单个文件
                if transferinfo.need_scrape:
                    self.scrape_metadata(path=transferinfo.target_path,
                                         mediainfo=file_mediainfo,
                                         transfer_type=transfer_type,
                                         metainfo=file_meta)
                # 更新进度
                processed_num += 1
                self.progress.update(value=processed_num / total_num * 100,
                                     text=f"{file_path.name} 转移完成",
                                     key=ProgressKey.FileTransfer)

            # 目录或文件转移完成
            self.progress.update(text=f"{trans_path} 转移完成，正在执行后续处理 ...",
                                 key=ProgressKey.FileTransfer)

            # 执行后续处理
            for mkey, media in medias.items():
                transfer_meta = metas[mkey]
                transfer_info = transfers[mkey]
                # 媒体目录
                if transfer_info.target_path.is_file():
                    transfer_info.target_path = transfer_info.target_path.parent
                # 发送通知
                se_str = None
                if media.type == MediaType.TV:
                    se_str = f"{transfer_meta.season} {StringUtils.format_ep(season_episodes[mkey])}"
                self.send_transfer_message(meta=transfer_meta,
                                           mediainfo=media,
                                           transferinfo=transfer_info,
                                           season_episode=se_str)
                # 广播事件
                self.eventmanager.send_event(EventType.TransferComplete, {
                    'meta': transfer_meta,
                    'mediainfo': media,
                    'transferinfo': transfer_info
                })
        # 结束进度
        logger.info(f"{path} 转移完成，共 {total_num} 个文件，"
                    f"失败 {fail_num} 个，跳过 {skip_num} 个")

        self.progress.update(value=100,
                             text=f"{path} 转移完成，共 {total_num} 个文件，"
                                  f"失败 {fail_num} 个，跳过 {skip_num} 个",
                             key=ProgressKey.FileTransfer)
        return True, "\n".join(err_msgs)

    def __transfer_online(self, storage: str, fileitem: schemas.FileItem,
                          meta: MetaBase, mediainfo: MediaInfo) -> Tuple[bool, str]:
        """
        整理一个远程目录
        """

        def __list_files(_storage: str, _fileid: str,
                         _path: str = None, _drive_id: str = None) -> List[schemas.FileItem]:
            """
            列出下级文件
            """
            if _storage == "aliyun":
                return AliyunHelper().list(drive_id=_drive_id, parent_file_id=_fileid, path=_path)
            elif _storage == "u115":
                return U115Helper().list(parent_file_id=_fileid, path=_path)
            return []

        def __rename_file(_storage: str, _deive_id: str, _fileid: str, _name: str) -> bool:
            """
            重命名文件
            """
            if _storage == "aliyun":
                return AliyunHelper().rename(drive_id=_deive_id, file_id=_fileid, name=_name)
            elif _storage == "u115":
                return U115Helper().rename(file_id=_fileid, name=_name)
            return False

        def __create_folder(_storage: str, _drive_id: str, _parent_fileid: str,
                            _name: str, _path: str) -> Optional[schemas.FileItem]:
            """
            创建目录
            """
            if _storage == "aliyun":
                return AliyunHelper().create_folder(drive_id=_drive_id, parent_file_id=_parent_fileid,
                                                    name=_name, path=_path)
            elif _storage == "u115":
                return U115Helper().create_folder(parent_file_id=_parent_fileid, name=_name, path=_path)
            return None

        def __move_file(_storage: str, _drive_id: str, _fileid: str, _target_fileid: str) -> bool:
            """
            移动文件
            """
            if _storage == "aliyun":
                return AliyunHelper().move(drive_id=_drive_id, file_id=_fileid, target_id=_target_fileid)
            elif _storage == "u115":
                return U115Helper().move(file_id=_fileid, target_id=_target_fileid)
            return False

        def __remove_dir(_storage: str, _drive_id: str, _fileid: str) -> bool:
            """
            删除目录
            """
            if _storage == "aliyun":
                return AliyunHelper().delete(drive_id=_drive_id, file_id=_fileid)
            elif _storage == "u115":
                return U115Helper().delete(file_id=_fileid)
            return False

        logger.info(f"开始整理 {fileitem.path} ...")
        self.progress.update(value=0,
                             text=f"正在整理 {fileitem.path} ...",
                             key=ProgressKey.FileTransfer)
        # 重新识别
        if not meta:
            # 文件元数据
            meta = MetaInfoPath(Path(fileitem.path))
        if not mediainfo:
            mediainfo = self.mediachain.recognize_by_meta(meta)
        if not mediainfo:
            logger.warn(f"{fileitem.name} 未识别到媒体信息")
            return False, f"{fileitem.name} 未识别到媒体信息"
        # 获取完整的路径命名
        full_names = self.recommend_name(meta=meta, mediainfo=mediainfo)
        if not full_names:
            logger.warn(f"{fileitem.path} 未获取到命名")
            return False, f"{fileitem.path} 未获取到命名"

        if mediainfo.type == MediaType.TV:
            # 电视剧
            [folder_name, season_name, file_name] = Path(full_names).parts
        else:
            # 电影
            season_name = None
            [folder_name, file_name] = Path(full_names).parts

        # 如果是单个文件，则直接重命名
        if fileitem.type == "file":
            # 重命名文件
            logger.info(f"正在整理 {fileitem.name} => {file_name} ...")
            if not __rename_file(_storage=storage, _deive_id=fileitem.drive_id, _fileid=fileitem.fileid, _name=file_name):
                logger.error(f"{fileitem.name} 重命名失败")
                return False, f"{fileitem.name} 重命名失败"
            logger.info(f"{fileitem.path} 整理完成")
        else:
            # 目录处理
            if mediainfo.type == MediaType.MOVIE:
                # 电影目录
                # 重命名当前目录
                logger.info(f"正在重命名 {fileitem.path} => {folder_name} ...")
                if not __rename_file(_storage=storage, _deive_id=fileitem.drive_id,
                                     _fileid=fileitem.fileid, _name=folder_name):
                    logger.error(f"{fileitem.path} 重命名失败")
                    return False, f"{fileitem.path} 重命名失败"
                logger.info(f"{fileitem.path} 重命名完成")
                # 处理所有子文件或目录
                files = __list_files(_storage=storage, _fileid=fileitem.fileid,
                                     _drive_id=fileitem.drive_id, _path=fileitem.path)
                if not files:
                    logger.info(f"{fileitem.path} 未找到文件，删除空目录")
                    if not __remove_dir(_storage=storage, _drive_id=fileitem.drive_id, _fileid=fileitem.fileid):
                        logger.error(f"{fileitem.path} 删除失败")
                        return False, f"{fileitem.path} 删除失败"
                    return True, ""
                for file in files:
                    # 过滤不处理的文件
                    if file.type == "file" and str(file.extension) in ['nfo', 'jpg', 'png']:
                        continue
                    # 重新识别文件或目录
                    file_meta = MetaInfoPath(Path(file.path))
                    if not file_meta.name:
                        # 过滤掉无效文件
                        continue
                    file_media = self.mediachain.recognize_by_meta(file_meta)
                    if not file_media:
                        logger.warn(f"{file.name} 未识别到媒体信息")
                        continue
                    # 整理这个文件或目录
                    self.__transfer_online(storage=storage, fileitem=file, meta=file_meta, mediainfo=file_media)
            else:
                # 电视剧目录
                # 判断当前目录类型
                folder_meta = MetaInfo(fileitem.name)
                if folder_meta.begin_season and not folder_meta.name:
                    # 季目录
                    logger.info(f"正在重命名 {fileitem.path} => {season_name} ...")
                    if not __rename_file(_storage=storage, _deive_id=fileitem.drive_id,
                                         _fileid=fileitem.fileid, _name=season_name):
                        logger.error(f"{fileitem.path} 重命名失败")
                        return False, f"{fileitem.path} 重命名失败"
                    logger.info(f"{fileitem.path} 重命名完成")
                elif folder_meta.name:
                    # 根目录，重命名当前目录
                    logger.info(f"正在重命名 {fileitem.path} => {folder_name} ...")
                    if not __rename_file(_storage=storage, _deive_id=fileitem.drive_id,
                                         _fileid=fileitem.fileid, _name=folder_name):
                        logger.error(f"{fileitem.path} 重命名失败")
                        return False, f"{fileitem.path} 重命名失败"
                    logger.info(f"{fileitem.path} 重命名完成")
                    # 是否有季
                    if folder_meta.begin_season:
                        # 创建季目录
                        logger.info(f"正在创建目录 {fileitem.path}{season_name} ...")
                        season_dir = __create_folder(_storage=storage, _drive_id=fileitem.drive_id,
                                                     _parent_fileid=fileitem.fileid, _name=season_name,
                                                     _path=fileitem.path)
                        if not season_dir:
                            logger.error(f"{fileitem.path}/{season_name} 创建失败")
                            return False, f"{fileitem.path}/{season_name} 创建失败"
                        logger.info(f"{fileitem.path}/{season_name} 创建完成")
                        # 移动当前目录下的所有文件到季目录
                        files = __list_files(_storage=storage, _fileid=fileitem.fileid,
                                             _drive_id=fileitem.drive_id, _path=fileitem.path)
                        if not files:
                            logger.error(f"{fileitem.path} 未找到文件，删除空目录")
                            if not __remove_dir(_storage=storage, _drive_id=fileitem.drive_id, _fileid=fileitem.fileid):
                                logger.error(f"{fileitem.path} 删除失败")
                                return False, f"{fileitem.path} 删除失败"
                            logger.info(f"{fileitem.path} 已删除")
                            return True, ""
                        for file in files:
                            if file.type == "dir":
                                continue
                            logger.info(f"正在移动 {file.path} => {season_dir.path}...")
                            if not __move_file(_storage=storage, _drive_id=fileitem.drive_id,
                                               _fileid=file.fileid, _target_fileid=season_dir.fileid):
                                logger.error(f"{file.name} 移动失败")
                                return False, f"{file.name} 移动失败"
                            logger.info(f"{file.path} 移动完成")
                        # 修改当前目录为季目录
                        fileitem = season_dir
                # 列出当前目录下所有的文件或目录，并进行重命名整理
                files = __list_files(_storage=storage, _fileid=fileitem.fileid,
                                     _drive_id=fileitem.drive_id, _path=fileitem.path)
                if not files:
                    logger.info(f"{fileitem.path} 未找到文件，删除空目录")
                    if not __remove_dir(_storage=storage, _drive_id=fileitem.drive_id, _fileid=fileitem.fileid):
                        logger.error(f"{fileitem.path} 删除失败")
                        return False, f"{fileitem.path} 删除失败"
                    logger.info(f"{fileitem.path} 已删除")
                    return True, ""
                for file in files:
                    # 过滤不处理的文件
                    if file.type == "file" and str(file.extension) in ['nfo', 'jpg', 'png']:
                        continue
                    # 重新识别文件或目录
                    file_meta = MetaInfoPath(Path(file.path))
                    file_media = self.mediachain.recognize_by_meta(file_meta)
                    if not file_media:
                        logger.warn(f"{file.name} 未识别到媒体信息")
                        continue
                    # 整理这个文件或目录
                    self.__transfer_online(storage=storage, fileitem=file, meta=file_meta, mediainfo=file_media)

        logger.info(f"{fileitem.path} 整理完成")
        self.progress.update(value=0,
                             text=f"{fileitem.path} 整理完成",
                             key=ProgressKey.FileTransfer)
        return True, ""

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
            # 如果是蓝光原盘
            if SystemUtils.is_bluray_dir(sub_dir):
                trans_paths.append(sub_dir)
            # 没有媒体文件的目录跳过
            elif SystemUtils.list_files(sub_dir, extensions=settings.RMT_MEDIAEXT):
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
                                           title="请输入正确的命令格式：/redo [id] [tmdbid/豆瓣id]|[类型]，"
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
        # TMDBID/豆瓣ID
        id_strs = arg_strs[1].split('|')
        media_id = id_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # 类型
        type_str = id_strs[1] if len(id_strs) > 1 else None
        if not type_str or type_str not in [MediaType.MOVIE.value, MediaType.TV.value]:
            args_error()
            return
        state, errmsg = self.__re_transfer(logid=int(logid),
                                           mtype=MediaType(type_str),
                                           mediaid=media_id)
        if not state:
            self.post_message(Notification(channel=channel, title="手动整理失败",
                                           text=errmsg, userid=userid, link=settings.MP_DOMAIN('#/history')))
            return

    def __re_transfer(self, logid: int, mtype: MediaType = None,
                      mediaid: str = None) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别转移，只支持简单条件
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param mediaid: TMDB ID/豆瓣ID
        """
        # 查询历史记录
        history: TransferHistory = self.transferhis.get(logid)
        if not history:
            logger.error(f"历史记录不存在，ID：{logid}")
            return False, "历史记录不存在"
        # 按源目录路径重新转移
        src_path = Path(history.src)
        if not src_path.exists():
            return False, f"源目录不存在：{src_path}"
        # 查询媒体信息
        if mtype and mediaid:
            mediainfo = self.recognize_media(mtype=mtype, tmdbid=int(mediaid) if str(mediaid).isdigit() else None,
                                             doubanid=mediaid)
            if mediainfo:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
        else:
            mediainfo = self.mediachain.recognize_by_path(str(src_path))
        if not mediainfo:
            return False, f"未识别到媒体信息，类型：{mtype.value}，id：{mediaid}"
        # 重新执行转移
        logger.info(f"{src_path.name} 识别为：{mediainfo.title_year}")

        # 删除旧的已整理文件
        if history.dest:
            self.delete_files(Path(history.dest))

        # 强制转移
        state, errmsg = self.__do_transfer(storage="local",
                                           path=src_path,
                                           mediainfo=mediainfo,
                                           download_hash=history.download_hash,
                                           force=True)
        if not state:
            return False, errmsg

        return True, ""

    def manual_transfer(self,
                        storage: str,
                        in_path: Path,
                        drive_id: str = None,
                        fileid: str = None,
                        filetype: str = None,
                        target: Path = None,
                        tmdbid: int = None,
                        doubanid: str = None,
                        mtype: MediaType = None,
                        season: int = None,
                        transfer_type: str = None,
                        epformat: EpisodeFormat = None,
                        min_filesize: int = 0,
                        scrape: bool = None,
                        force: bool = False) -> Tuple[bool, Union[str, list]]:
        """
        手动转移，支持复杂条件，带进度显示
        :param storage: 存储器
        :param in_path: 源文件路径
        :param drive_id: 网盘ID
        :param fileid: 文件ID
        :param filetype: 文件类型
        :param target: 目标路径
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :param season: 季度
        :param transfer_type: 转移类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param force: 是否强制转移
        """
        logger.info(f"手动转移：{in_path} ...")

        if tmdbid or doubanid:
            # 有输入TMDBID时单个识别
            # 识别媒体信息
            mediainfo: MediaInfo = self.mediachain.recognize_media(tmdbid=tmdbid, doubanid=doubanid, mtype=mtype)
            if not mediainfo:
                return False, f"媒体信息识别失败，tmdbid：{tmdbid}，doubanid：{doubanid}，type: {mtype.value}"
            else:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
            # 开始进度
            self.progress.start(ProgressKey.FileTransfer)
            self.progress.update(value=0,
                                 text=f"开始转移 {in_path} ...",
                                 key=ProgressKey.FileTransfer)
            # 开始转移
            state, errmsg = self.__do_transfer(
                storage=storage,
                path=in_path,
                drive_id=drive_id,
                fileid=fileid,
                filetype=filetype,
                mediainfo=mediainfo,
                target=target,
                transfer_type=transfer_type,
                season=season,
                epformat=epformat,
                min_filesize=min_filesize,
                scrape=scrape,
                force=force
            )
            if not state:
                return False, errmsg

            self.progress.end(ProgressKey.FileTransfer)
            logger.info(f"{in_path} 转移完成")
            return True, ""
        else:
            # 没有输入TMDBID时，按文件识别
            state, errmsg = self.__do_transfer(storage=storage,
                                               path=in_path,
                                               drive_id=drive_id,
                                               fileid=fileid,
                                               filetype=filetype,
                                               target=target,
                                               transfer_type=transfer_type,
                                               season=season,
                                               epformat=epformat,
                                               min_filesize=min_filesize,
                                               scrape=scrape,
                                               force=force)
            return state, errmsg

    def send_transfer_message(self, meta: MetaBase, mediainfo: MediaInfo,
                              transferinfo: TransferInfo, season_episode: str = None):
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
            title=msg_title, text=msg_str, image=mediainfo.get_message_image(),
            link=settings.MP_DOMAIN('#/history')))

    def delete_files(self, path: Path) -> Tuple[bool, str]:
        """
        删除转移后的文件以及空目录
        :param path: 文件路径
        :return: 成功标识，错误信息
        """
        logger.info(f"开始删除文件以及空目录：{path} ...")
        if not path.exists():
            return True, f"文件或目录不存在：{path}"
        if path.is_file():
            # 删除文件、nfo、jpg等同名文件
            pattern = path.stem.replace('[', '?').replace(']', '?')
            files = path.parent.glob(f"{pattern}.*")
            for file in files:
                Path(file).unlink()
            logger.warn(f"文件 {path} 已删除")
            # 删除thumb图片
            thumb_file = path.parent / (path.stem + "-thumb.jpg")
            if thumb_file.exists():
                thumb_file.unlink()
                logger.info(f"文件 {thumb_file} 已删除")
            # 需要删除父目录
        elif str(path.parent) == str(path.root):
            # 根目录，不删除
            logger.warn(f"根目录 {path} 不能删除！")
            return False, f"根目录 {path} 不能删除！"
        else:
            # 非根目录，才删除目录
            shutil.rmtree(path)
            # 删除目录
            logger.warn(f"目录 {path} 已删除")
            # 需要删除父目录

        # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
        if not SystemUtils.exits_files(path.parent, settings.RMT_MEDIAEXT):
            # 所有媒体库根目录的名称
            library_roots = self.directoryhelper.get_library_dirs()
            library_root_names = [Path(library_root.path).name for library_root in library_roots if library_root.path]
            # 所有二级分类的名称
            category_names = []
            category_conf = self.media_category()
            if category_conf:
                category_names += list(category_conf.keys())
                for cats in category_conf.values():
                    category_names += cats
            # 判断父目录是否为空, 为空则删除
            for parent_path in path.parents:
                # 遍历父目录到媒体库二级分类根路径
                if parent_path.name in library_root_names:
                    break
                if parent_path.name in category_names:
                    continue
                if str(parent_path.parent) != str(path.root):
                    # 父目录非根目录，才删除父目录
                    if not SystemUtils.exits_files(parent_path, settings.RMT_MEDIAEXT):
                        # 当前路径下没有媒体文件则删除
                        try:
                            shutil.rmtree(parent_path)
                            logger.warn(f"目录 {parent_path} 已删除")
                        except Exception as e:
                            logger.error(f"删除目录 {parent_path} 失败：{str(e)}")
                            return False, f"删除目录 {parent_path} 失败：{str(e)}"
        return True, ""
