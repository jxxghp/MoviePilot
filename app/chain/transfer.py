import re
import threading
from pathlib import Path
from typing import List, Optional, Tuple, Union, Dict

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings, global_vars
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath, MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.format import FormatParser
from app.helper.progress import ProgressHelper
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent, Notification, EpisodeFormat, FileItem, TransferDirectoryConf
from app.schemas.types import TorrentStatus, EventType, MediaType, ProgressKey, NotificationType, MessageChannel, \
    SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class TransferChain(ChainBase):
    """
    文件整理处理链
    """

    def __init__(self):
        super().__init__()
        self.downloadhis = DownloadHistoryOper()
        self.transferhis = TransferHistoryOper()
        self.progress = ProgressHelper()
        self.mediachain = MediaChain()
        self.tmdbchain = TmdbChain()
        self.storagechain = StorageChain()
        self.systemconfig = SystemConfigOper()
        self.directoryhelper = DirectoryHelper()
        self.all_exts = settings.RMT_MEDIAEXT

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
        获取下载器中的种子列表，并执行整理
        """

        # 全局锁，避免重复处理
        with lock:
            # 获取下载器监控目录
            download_dirs = self.directoryhelper.get_download_dirs()
            # 如果没有下载器监控的目录则不处理
            if not any(dir_info.monitor_type == "downloader" and dir_info.storage == "local"
                       for dir_info in download_dirs):
                return True
            logger.info("开始整理下载器中已经完成下载的文件 ...")
            # 从下载器获取种子列表
            torrents: Optional[List[TransferTorrent]] = self.list_torrents(status=TorrentStatus.TRANSFER)
            if not torrents:
                logger.info("没有已完成下载但未整理的任务")
                return False

            logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")

            for torrent in torrents:
                if global_vars.is_system_stopped:
                    break
                # 文件路径
                file_path = torrent.path
                if not file_path.exists():
                    logger.warn(f"文件不存在：{file_path}")
                    continue
                # 检查是否为下载器监控目录中的文件
                is_downloader_monitor = False
                for dir_info in download_dirs:
                    if dir_info.monitor_type != "downloader":
                        continue
                    if not dir_info.download_path:
                        continue
                    if file_path.is_relative_to(Path(dir_info.download_path)):
                        is_downloader_monitor = True
                        break
                if not is_downloader_monitor:
                    logger.debug(f"文件 {file_path} 不在下载器监控目录中，不通过下载器进行整理")
                    continue
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
                        # 更新自定义媒体类别
                        if downloadhis.media_category:
                            mediainfo.category = downloadhis.media_category
                else:
                    # 非MoviePilot下载的任务，按文件识别
                    mediainfo = None

                # 执行整理，匹配源目录
                state, errmsg = self.__do_transfer(
                    fileitem=FileItem(
                        storage="local",
                        path=str(file_path),
                        type="dir" if not file_path.is_file() else "file",
                        name=file_path.name,
                        size=file_path.stat().st_size,
                        extension=file_path.suffix.lstrip('.'),
                    ),
                    mediainfo=mediainfo,
                    downloader=torrent.downloader,
                    download_hash=torrent.hash,
                    src_match=True
                )

                # 设置下载任务状态
                if state:
                    self.transfer_completed(hashs=torrent.hash)

            # 结束
            logger.info("所有下载器中下载完成的文件已整理完成")
            return True

    def __do_transfer(self, fileitem: FileItem,
                      meta: MetaBase = None, mediainfo: MediaInfo = None,
                      target_directory: TransferDirectoryConf = None,
                      target_storage: str = None, target_path: Path = None,
                      transfer_type: str = None, scrape: bool = None,
                      library_type_folder: bool = False, library_category_folder: bool = False,
                      season: int = None, epformat: EpisodeFormat = None, min_filesize: int = 0,
                      downloader: str = None, download_hash: str = None,
                      force: bool = False, src_match: bool = False) -> Tuple[bool, str]:
        """
        执行一个复杂目录的整理操作
        :param fileitem: 文件项
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param target_directory:  目标目录配置
        :param target_storage: 目标存储器
        :param target_path: 目标路径
        :param transfer_type: 整理类型
        :param scrape: 是否刮削元数据
        :param library_type_folder: 媒体库类型子目录
        :param library_category_folder: 媒体库类别子目录
        :param season: 季
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param downloader: 下载器
        :param download_hash: 下载记录hash
        :param force: 是否强制整理
        :param src_match: 是否源目录匹配
        返回：成功标识，错误信息
        """

        # 自定义格式
        formaterHandler = FormatParser(eformat=epformat.format,
                                       details=epformat.detail,
                                       part=epformat.part,
                                       offset=epformat.offset) if epformat else None

        # 整理屏蔽词
        transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)

        # 开始进度
        self.progress.start(ProgressKey.FileTransfer)

        # 汇总季集清单
        season_episodes: Dict[Tuple, List[int]] = {}
        # 汇总媒体信息
        medias: Dict[Tuple, MediaInfo] = {}
        # 汇总整理信息
        transfers: Dict[Tuple, TransferInfo] = {}

        # 待整理文件列表
        file_items: List[FileItem] = []
        # 蓝光目录列表
        bluray: List[FileItem] = []
        # 汇总错误信息
        err_msgs: List[str] = []
        # 已处理数量
        processed_num = 0
        # 失败数量
        fail_num = 0
        # 跳过数量
        skip_num = 0
        # 本次整理方式
        current_transfer_type = transfer_type
        # 是否全部成功
        all_success = True

        # 获取待整理路径清单
        trans_items = self.__get_trans_fileitems(fileitem)
        # 总文件数
        total_num = len(trans_items)
        self.progress.update(value=0,
                             text=f"开始 {fileitem.path}，共 {total_num} 个文件或子目录 ...",
                             key=ProgressKey.FileTransfer)

        if not trans_items:
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        # 处理所有待整理目录或文件，默认一个整理路径或文件只有一个媒体信息
        for trans_item in trans_items:
            item_path = Path(trans_item.path)
            # 是否蓝光路径
            bluray_dir = trans_item.storage == "local" and SystemUtils.is_bluray_dir(item_path)
            # 如果是目录且不是⼀蓝光原盘，获取所有文件并整理
            if trans_item.type == "dir" and not bluray_dir:
                # 遍历获取下载目录所有文件（递归）
                if files := self.storagechain.list_files(trans_item, recursion=True):
                    file_items.extend(files)
            # 如果是蓝光目录,计算⼤⼩
            elif bluray_dir:
                bluray.append(trans_item)
            # 单个文件
            else:
                file_items.append(trans_item)

        if formaterHandler:
            # 有集自定义格式，过滤文件
            file_items = [f for f in file_items if formaterHandler.match(f.name)]

        # 过滤后缀和大小
        file_items = [f for f in file_items
                      if f.extension and (f".{f.extension.lower()}" in self.all_exts
                                          and (not min_filesize or f.size > min_filesize * 1024 * 1024))]
        # BDMV 跳过过滤
        file_items.extend(bluray)
        if not file_items:
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        # 更新总文件数
        total_num = len(file_items)
        logger.info(f"正在整理 {total_num} 个文件...")

        # 整理所有文件
        for file_item in file_items:
            if global_vars.is_system_stopped:
                break
            file_path = Path(file_item.path)
            # 回收站及隐藏的文件不处理
            if file_item.path.find('/@Recycle/') != -1 \
                    or file_item.path.find('/#recycle/') != -1 \
                    or file_item.path.find('/.') != -1 \
                    or file_item.path.find('/@eaDir') != -1:
                logger.debug(f"{file_item.path} 是回收站或隐藏的文件")
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
                    if keyword and re.search(r"%s" % keyword, file_item.path, re.IGNORECASE):
                        logger.info(f"{file_item.path} 命中整理屏蔽词 {keyword}，不处理")
                        is_blocked = True
                        break
            if is_blocked:
                err_msgs.append(f"{file_item.name} 命中整理屏蔽词")
                # 计数
                processed_num += 1
                skip_num += 1
                all_success = False
                continue

            # 整理成功的不再处理
            if not force:
                transferd = self.transferhis.get_by_src(file_item.path, storage=file_item.storage)
                if transferd and transferd.status:
                    logger.info(f"{file_item.path} 已成功整理过，如需重新处理，请删除历史记录。")
                    # 计数
                    processed_num += 1
                    skip_num += 1
                    all_success = False
                    continue

            # 更新进度
            self.progress.update(value=processed_num / total_num * 100,
                                 text=f"正在整理 （{processed_num + 1}/{total_num}）{file_item.name} ...",
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
                all_success = False
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
                # 新增整理失败历史记录
                his = self.transferhis.add_fail(
                    fileitem=file_item,
                    mode=transfer_type,
                    meta=file_meta,
                    download_hash=download_hash
                )
                self.post_message(Notification(
                    mtype=NotificationType.Manual,
                    title=f"{file_path.name} 未识别到媒体信息，无法入库！",
                    text=f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别整理。",
                    link=settings.MP_DOMAIN('#/history')
                ))
                # 计数
                processed_num += 1
                fail_num += 1
                all_success = False
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
                download_file = self.downloadhis.get_file_by_fullpath(file_item.path)
                if download_file:
                    download_hash = download_file.download_hash

            # 查询整理目标目录
            if not target_directory and not target_path:
                if src_match:
                    # 按源目录匹配，以便找到更合适的目录配置
                    target_directory = self.directoryhelper.get_dir(file_mediainfo,
                                                                    storage=file_item.storage,
                                                                    src_path=file_path,
                                                                    target_storage=target_storage)
                else:
                    # 未指定目标路径，根据媒体信息获取目标目录
                    target_directory = self.directoryhelper.get_dir(file_mediainfo,
                                                                    storage=target_storage,
                                                                    target_storage=target_storage)

            # 执行整理
            transferinfo: TransferInfo = self.transfer(fileitem=file_item,
                                                       meta=file_meta,
                                                       mediainfo=file_mediainfo,
                                                       target_directory=target_directory,
                                                       target_storage=target_storage,
                                                       target_path=target_path,
                                                       transfer_type=transfer_type,
                                                       episodes_info=episodes_info,
                                                       scrape=scrape,
                                                       library_type_folder=library_type_folder,
                                                       library_category_folder=library_category_folder)
            if not transferinfo:
                logger.error("文件整理模块运行失败")
                return False, "文件整理模块运行失败"
            if not transferinfo.success:
                # 整理失败
                logger.warn(f"{file_path.name} 入库失败：{transferinfo.message}")
                err_msgs.append(f"{file_path.name} {transferinfo.message}")
                # 新增整理失败历史记录
                self.transferhis.add_fail(
                    fileitem=file_item,
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
                all_success = False
                continue

            # 汇总信息
            current_transfer_type = transferinfo.transfer_type
            mkey = (file_mediainfo.tmdb_id, file_meta.begin_season)
            if mkey not in medias:
                # 新增信息
                medias[mkey] = file_mediainfo
                season_episodes[mkey] = file_meta.episode_list
                transfers[mkey] = transferinfo
            else:
                # 合并季集清单
                season_episodes[mkey] = list(set(season_episodes[mkey] + file_meta.episode_list))
                # 合并整理数据
                transfers[mkey].file_count += transferinfo.file_count
                transfers[mkey].total_size += transferinfo.total_size
                transfers[mkey].file_list.extend(transferinfo.file_list)
                transfers[mkey].file_list_new.extend(transferinfo.file_list_new)
                transfers[mkey].fail_list.extend(transferinfo.fail_list)

            # 新增整理成功历史记录
            self.transferhis.add_success(
                fileitem=file_item,
                mode=transfer_type or transferinfo.transfer_type,
                download_hash=download_hash,
                meta=file_meta,
                mediainfo=file_mediainfo,
                transferinfo=transferinfo
            )

            # 整理完成事件
            self.eventmanager.send_event(EventType.TransferComplete, {
                'meta': file_meta,
                'mediainfo': file_mediainfo,
                'transferinfo': transferinfo,
                'downloader': downloader,
                'download_hash': download_hash,
            })

            # 更新进度
            processed_num += 1
            self.progress.update(value=processed_num / total_num * 100,
                                 text=f"{file_path.name} 整理完成",
                                 key=ProgressKey.FileTransfer)

        # 目录或文件整理完成
        self.progress.update(text=f"{fileitem.path} 整理完成，正在执行后续处理 ...",
                             key=ProgressKey.FileTransfer)

        # 执行后续处理
        for mkey, media in medias.items():
            transfer_info = transfers[mkey]
            transfer_meta = MetaInfo(transfer_info.target_diritem.name)
            transfer_meta.begin_season = mkey[1]
            # 发送通知
            if transfer_info.need_notify:
                se_str = None
                if media.type == MediaType.TV:
                    se_str = f"{transfer_meta.season} {StringUtils.format_ep(season_episodes[mkey])}"
                self.send_transfer_message(meta=transfer_meta,
                                           mediainfo=media,
                                           transferinfo=transfer_info,
                                           season_episode=se_str)
            # 刮削事件
            if scrape or transfer_info.need_scrape:
                self.eventmanager.send_event(EventType.MetadataScrape, {
                    'meta': transfer_meta,
                    'mediainfo': media,
                    'fileitem': transfer_info.target_diritem
                })

        # 移动模式处理
        if all_success and current_transfer_type in ["move"]:
            # 下载器hash
            if download_hash:
                if self.remove_torrents(download_hash, downloader=downloader):
                    logger.info(f"移动模式删除种子成功：{download_hash} ")
            # 删除残留目录
            if fileitem:
                self.storagechain.delete_media_file(fileitem, delete_self=False)

        # 结束进度
        logger.info(f"{fileitem.path} 整理完成，共 {total_num} 个文件，"
                    f"失败 {fail_num} 个，跳过 {skip_num} 个")

        self.progress.update(value=100,
                             text=f"{fileitem.path} 整理完成，共 {total_num} 个文件，"
                                  f"失败 {fail_num} 个，跳过 {skip_num} 个",
                             key=ProgressKey.FileTransfer)
        # 结速进度
        self.progress.end(ProgressKey.FileTransfer)

        return True, "\n".join(err_msgs)

    def __get_trans_fileitems(self, fileitem: FileItem):
        """
        获取整理目录或文件列表
        """

        file_path = Path(fileitem.path)

        if fileitem.storage == "local" and not file_path.exists():
            logger.warn(f"目录不存在：{fileitem.path}")
            return []

        # 单文件
        if fileitem.type == "file":
            return [fileitem]

        # 蓝光原盘
        if fileitem.storage == "local" and SystemUtils.is_bluray_dir(file_path):
            return [fileitem]

        # 需要整理的文件项列表
        trans_items = []

        # 先检查当前目录的下级目录，以支持合集的情况
        for sub_dir in self.storagechain.list_files(fileitem):
            subfile_path = Path(sub_dir.path)
            # 添加蓝光原盘
            if sub_dir.storage == "local" \
                    and sub_dir.type == "dir" \
                    and SystemUtils.is_bluray_dir(subfile_path):
                trans_items.append(sub_dir)
            # 添加目录
            elif sub_dir.type == "dir":
                trans_items.append(sub_dir)

        if not trans_items:
            # 没有有效子目录，直接整理当前目录
            trans_items.append(fileitem)
        else:
            # 有子目录时，把当前目录的文件添加到整理任务中
            sub_items = self.storagechain.list_files(fileitem)
            if sub_items:
                sub_files = [f for f in sub_items if f.type == "file"]
                if sub_files:
                    trans_items.extend(sub_files)

        return trans_items

    def remote_transfer(self, arg_str: str, channel: MessageChannel,
                        userid: Union[str, int] = None, source: str = None):
        """
        远程重新整理，参数 历史记录ID TMDBID|类型
        """

        def args_error():
            self.post_message(Notification(channel=channel, source=source,
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
            self.post_message(Notification(channel=channel, title="手动整理失败", source=source,
                                           text=errmsg, userid=userid, link=settings.MP_DOMAIN('#/history')))
            return

    def __re_transfer(self, logid: int, mtype: MediaType = None,
                      mediaid: str = None) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别整理，只支持简单条件
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param mediaid: TMDB ID/豆瓣ID
        """
        # 查询历史记录
        history: TransferHistory = self.transferhis.get(logid)
        if not history:
            logger.error(f"历史记录不存在，ID：{logid}")
            return False, "历史记录不存在"
        # 按源目录路径重新整理
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
        # 重新执行整理
        logger.info(f"{src_path.name} 识别为：{mediainfo.title_year}")

        # 删除旧的已整理文件
        if history.dest_fileitem:
            # 解析目标文件对象
            dest_fileitem = FileItem(**history.dest_fileitem)
            self.storagechain.delete_file(dest_fileitem)

        # 强制整理
        if history.src_fileitem:
            state, errmsg = self.__do_transfer(fileitem=FileItem(**history.src_fileitem),
                                               mediainfo=mediainfo,
                                               download_hash=history.download_hash,
                                               force=True)
            if not state:
                return False, errmsg

        return True, ""

    def manual_transfer(self,
                        fileitem: FileItem,
                        target_storage: str = None,
                        target_path: Path = None,
                        tmdbid: int = None,
                        doubanid: str = None,
                        mtype: MediaType = None,
                        season: int = None,
                        transfer_type: str = None,
                        epformat: EpisodeFormat = None,
                        min_filesize: int = 0,
                        scrape: bool = None,
                        library_type_folder: bool = False,
                        library_category_folder: bool = False,
                        force: bool = False) -> Tuple[bool, Union[str, list]]:
        """
        手动整理，支持复杂条件，带进度显示
        :param fileitem: 文件项
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :param season: 季度
        :param transfer_type: 整理类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型建立目录
        :param library_category_folder: 是否按类别建立目录
        :param force: 是否强制整理
        """
        logger.info(f"手动整理：{fileitem.path} ...")
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
                                 text=f"开始整理 {fileitem.path} ...",
                                 key=ProgressKey.FileTransfer)
            # 开始整理
            state, errmsg = self.__do_transfer(
                fileitem=fileitem,
                target_storage=target_storage,
                target_path=target_path,
                mediainfo=mediainfo,
                transfer_type=transfer_type,
                season=season,
                epformat=epformat,
                min_filesize=min_filesize,
                scrape=scrape,
                library_type_folder=library_type_folder,
                library_category_folder=library_category_folder,
                force=force,
            )
            if not state:
                return False, errmsg

            self.progress.end(ProgressKey.FileTransfer)
            logger.info(f"{fileitem.path} 整理完成")
            return True, ""
        else:
            # 没有输入TMDBID时，按文件识别
            state, errmsg = self.__do_transfer(fileitem=fileitem,
                                               target_storage=target_storage,
                                               target_path=target_path,
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
