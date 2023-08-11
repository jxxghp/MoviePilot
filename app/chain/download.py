import re
from pathlib import Path
from typing import List, Optional, Tuple, Set, Dict, Union

from app.chain import ChainBase
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.meta import MetaBase
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.mediaserver_oper import MediaServerOper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import ExistMediaInfo, NotExistMediaInfo, DownloadingTorrent, Notification
from app.schemas.types import MediaType, TorrentStatus, EventType, MessageChannel, NotificationType
from app.utils.string import StringUtils


class DownloadChain(ChainBase):
    """
    下载处理链
    """

    def __init__(self):
        super().__init__()
        self.torrent = TorrentHelper()
        self.downloadhis = DownloadHistoryOper()
        self.mediaserver = MediaServerOper()

    def post_download_message(self, meta: MetaBase, mediainfo: MediaInfo, torrent: TorrentInfo,
                              channel: MessageChannel = None,
                              userid: str = None):
        """
        发送添加下载的消息
        """
        msg_text = ""
        if torrent.site_name:
            msg_text = f"站点：{torrent.site_name}"
        if meta.resource_term:
            msg_text = f"{msg_text}\n质量：{meta.resource_term}"
        if torrent.size:
            if str(torrent.size).replace(".", "").isdigit():
                size = StringUtils.str_filesize(torrent.size)
            else:
                size = torrent.size
            msg_text = f"{msg_text}\n大小：{size}"
        if torrent.title:
            msg_text = f"{msg_text}\n种子：{torrent.title}"
        if torrent.seeders:
            msg_text = f"{msg_text}\n做种数：{torrent.seeders}"
        msg_text = f"{msg_text}\n促销：{torrent.volume_factor}"
        if torrent.hit_and_run:
            msg_text = f"{msg_text}\nHit&Run：是"
        if torrent.description:
            html_re = re.compile(r'<[^>]+>', re.S)
            description = html_re.sub('', torrent.description)
            torrent.description = re.sub(r'<[^>]+>', '', description)
            msg_text = f"{msg_text}\n描述：{torrent.description}"

        self.post_message(Notification(
            channel=channel,
            mtype=NotificationType.Download,
            title=f"{mediainfo.title_year}"
                  f"{meta.season_episode} 开始下载",
            text=msg_text,
            image=mediainfo.get_message_image(),
            userid=userid))

    def download_torrent(self, torrent: TorrentInfo,
                         channel: MessageChannel = None,
                         userid: Union[str, int] = None) -> Tuple[Optional[Path], str, list]:
        """
        下载种子文件
        :return: 种子路径，种子目录名，种子文件清单
        """
        torrent_file, _, download_folder, files, error_msg = self.torrent.download_torrent(
            url=torrent.enclosure,
            cookie=torrent.site_cookie,
            ua=torrent.site_ua,
            proxy=torrent.site_proxy)
        if not torrent_file:
            logger.error(f"下载种子文件失败：{torrent.title} - {torrent.enclosure}")
            self.post_message(Notification(
                channel=channel,
                mtype=NotificationType.Download,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{error_msg}\n种子链接：{torrent.enclosure}",
                userid=userid))
            return None, "", []
        return torrent_file, download_folder, files

    def download_single(self, context: Context, torrent_file: Path = None,
                        episodes: Set[int] = None,
                        channel: MessageChannel = None,
                        save_path: str = None,
                        userid: Union[str, int] = None) -> Optional[str]:
        """
        下载及发送通知
        """
        _torrent = context.torrent_info
        _media = context.media_info
        _meta = context.meta_info
        _folder_name = ""
        if not torrent_file:
            # 下载种子文件
            torrent_file, _folder_name, _ = self.download_torrent(_torrent, userid=userid)
            if not torrent_file:
                return
        # 下载目录
        if not save_path:
            if settings.DOWNLOAD_CATEGORY and _media and _media.category:
                if _media.type == MediaType.MOVIE:
                    download_dir = Path(settings.DOWNLOAD_MOVIE_PATH or settings.DOWNLOAD_PATH) / _media.category
                else:
                    download_dir = Path(settings.DOWNLOAD_TV_PATH or settings.DOWNLOAD_PATH) / _media.category
            elif _media:
                if _media.type == MediaType.MOVIE:
                    download_dir = Path(settings.DOWNLOAD_MOVIE_PATH or settings.DOWNLOAD_PATH)
                else:
                    download_dir = Path(settings.DOWNLOAD_TV_PATH or settings.DOWNLOAD_PATH)
            else:
                download_dir = Path(settings.DOWNLOAD_PATH)
        else:
            download_dir = Path(save_path)
        # 添加下载
        result: Optional[tuple] = self.download(torrent_path=torrent_file,
                                                cookie=_torrent.site_cookie,
                                                episodes=episodes,
                                                download_dir=download_dir)
        if result:
            _hash, error_msg = result
        else:
            _hash, error_msg = None, "未知错误"

        if _hash:
            # 登记下载记录
            self.downloadhis.add(
                path=_folder_name or _torrent.title,
                type=_media.type.value,
                title=_media.title,
                year=_media.year,
                tmdbid=_media.tmdb_id,
                imdbid=_media.imdb_id,
                tvdbid=_media.tvdb_id,
                doubanid=_media.douban_id,
                seasons=_meta.season,
                episodes=_meta.episode,
                image=_media.get_backdrop_image(),
                download_hash=_hash,
                torrent_name=_torrent.title,
                torrent_description=_torrent.description,
                torrent_site=_torrent.site_name
            )
            # 发送消息
            self.post_download_message(meta=_meta, mediainfo=_media, torrent=_torrent, channel=channel)
            # 下载成功后处理
            self.download_added(context=context, torrent_path=torrent_file, download_dir=download_dir)
            # 广播事件
            self.eventmanager.send_event(EventType.DownloadAdded, {
                "hash": _hash,
                "torrent_file": torrent_file,
                "context": context
            })
        else:
            # 下载失败
            logger.error(f"{_media.title_year} 添加下载任务失败："
                         f"{_torrent.title} - {_torrent.enclosure}，{error_msg}")
            self.post_message(Notification(
                channel=channel,
                mtype=NotificationType.Download,
                title="添加下载任务失败：%s %s"
                      % (_media.title_year, _meta.season_episode),
                text=f"站点：{_torrent.site_name}\n"
                     f"种子名称：{_meta.org_string}\n"
                     f"种子链接：{_torrent.enclosure}\n"
                     f"错误信息：{error_msg}",
                image=_media.get_message_image(),
                userid=userid))
        return _hash

    def batch_download(self,
                       contexts: List[Context],
                       no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                       save_path: str = None,
                       userid: str = None) -> Tuple[List[Context], Dict[int, Dict[int, NotExistMediaInfo]]]:
        """
        根据缺失数据，自动种子列表中组合择优下载
        :param contexts:  资源上下文列表
        :param no_exists:  缺失的剧集信息
        :param save_path:  保存路径
        :param userid:  用户ID
        :return: 已经下载的资源列表、剩余未下载到的剧集 no_exists[tmdb_id] = {season: NotExistMediaInfo}
        """
        # 已下载的项目
        downloaded_list: List[Context] = []

        def __update_seasons(_tmdbid: int, _need: list, _current: list) -> list:
            """
            更新need_tvs季数，返回剩余季数
            :param _tmdbid: TMDBID
            :param _need: 需要下载的季数
            :param _current: 已经下载的季数
            """
            # 剩余季数
            need = list(set(_need).difference(set(_current)))
            # 清除已下载的季信息
            for _sea in list(no_exists.get(_tmdbid)):
                if _sea not in need:
                    no_exists[_tmdbid].pop(_sea)
                if not no_exists.get(_tmdbid) and no_exists.get(_tmdbid) is not None:
                    no_exists.pop(_tmdbid)
            return need

        def __update_episodes(_tmdbid: int, _sea: int, _need: list, _current: set) -> list:
            """
            更新need_tvs集数，返回剩余集数
            :param _tmdbid: TMDBID
            :param _sea: 季数
            :param _need: 需要下载的集数
            :param _current: 已经下载的集数
            """
            # 剩余集数
            need = list(set(_need).difference(set(_current)))
            if need:
                not_exist = no_exists[_tmdbid][_sea]
                no_exists[_tmdbid][_sea] = NotExistMediaInfo(
                    season=not_exist.season,
                    episodes=need,
                    total_episode=not_exist.total_episode,
                    start_episode=not_exist.start_episode
                )
            else:
                no_exists[_tmdbid].pop(_sea)
                if not no_exists.get(_tmdbid) and no_exists.get(_tmdbid) is not None:
                    no_exists.pop(_tmdbid)
            return need

        def __get_season_episodes(tmdbid: int, season: int) -> int:
            """
            获取需要的季的集数
            """
            if not no_exists.get(tmdbid):
                return 0
            no_exist = no_exists.get(tmdbid)
            if not no_exist.get(season):
                return 0
            return no_exist[season].total_episode

        # 分组排序
        contexts = TorrentHelper().sort_group_torrents(contexts)

        # 如果是电影，直接下载
        for context in contexts:
            if context.media_info.type == MediaType.MOVIE:
                if self.download_single(context, save_path=save_path, userid=userid):
                    # 下载成功
                    downloaded_list.append(context)

        # 电视剧整季匹配
        if no_exists:
            # 先把整季缺失的拿出来，看是否刚好有所有季都满足的种子 {tmdbid: [seasons]}
            need_seasons: Dict[int, list] = {}
            for need_tmdbid, need_tv in no_exists.items():
                for tv in need_tv.values():
                    if not tv:
                        continue
                    # 季列表为空的，代表全季缺失
                    if not tv.episodes:
                        if not need_seasons.get(need_tmdbid):
                            need_seasons[need_tmdbid] = []
                        need_seasons[need_tmdbid].append(tv.season or 1)
            # 查找整季包含的种子，只处理整季没集的种子或者是集数超过季的种子
            for need_tmdbid, need_season in need_seasons.items():
                # 循环种子
                for context in contexts:
                    # 媒体信息
                    media = context.media_info
                    # 识别元数据
                    meta = context.meta_info
                    # 种子信息
                    torrent = context.torrent_info
                    # 排除电视剧
                    if media.type != MediaType.TV:
                        continue
                    # 种子的季清单
                    torrent_season = meta.season_list
                    # 种子有集的不要
                    if meta.episode_list:
                        continue
                    # 匹配TMDBID
                    if need_tmdbid == media.tmdb_id:
                        # 种子季是需要季或者子集
                        if set(torrent_season).issubset(set(need_season)):
                            if len(torrent_season) == 1:
                                # 只有一季的可能是命名错误，需要打开种子鉴别，只有实际集数大于等于总集数才下载
                                torrent_path, _, torrent_files = self.download_torrent(torrent)
                                if not torrent_path:
                                    continue
                                torrent_episodes = self.torrent.get_torrent_episodes(torrent_files)
                                if not torrent_episodes \
                                        or len(torrent_episodes) >= __get_season_episodes(need_tmdbid,
                                                                                          torrent_season[0]):
                                    # 下载
                                    download_id = self.download_single(context=context,
                                                                       torrent_file=torrent_path,
                                                                       save_path=save_path,
                                                                       userid=userid)
                                else:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数为 {len(torrent_episodes)}，未含所需集数")
                                    continue
                            else:
                                # 下载
                                download_id = self.download_single(context, save_path=save_path, userid=userid)

                            if download_id:
                                # 下载成功
                                downloaded_list.append(context)
                                # 更新仍需季集
                                need_season = __update_seasons(_tmdbid=need_tmdbid,
                                                               _need=need_season,
                                                               _current=torrent_season)
        # 电视剧季内的集匹配
        if no_exists:
            # TMDBID列表
            need_tv_list = list(no_exists)
            for need_tmdbid in need_tv_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_tmdbid)
                if not need_tv:
                    continue
                # 循环每一季
                for sea, tv in need_tv.items():
                    # 当前需要季
                    need_season = sea
                    # 当前需要集
                    need_episodes = tv.episodes
                    # TMDB总集数
                    total_episode = tv.total_episode
                    # 需要开始集
                    start_episode = tv.start_episode or 1
                    # 缺失整季的转化为缺失集进行比较
                    if not need_episodes:
                        need_episodes = list(range(start_episode, total_episode))
                    # 循环种子
                    for context in contexts:
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 非剧集不处理
                        if media.type != MediaType.TV:
                            continue
                        # 匹配TMDB
                        if media.tmdb_id == need_tmdbid:
                            # 不重复添加
                            if context in downloaded_list:
                                continue
                            # 种子季
                            torrent_season = meta.season_list
                            # 只处理单季含集的种子
                            if len(torrent_season) != 1 or torrent_season[0] != need_season:
                                continue
                            # 种子集列表
                            torrent_episodes = set(meta.episode_list)
                            # 整季的不处理
                            if not torrent_episodes:
                                continue
                            # 为需要集的子集则下载
                            if torrent_episodes.issubset(set(need_episodes)):
                                # 下载
                                download_id = self.download_single(context, save_path=save_path, userid=userid)
                                if download_id:
                                    # 下载成功
                                    downloaded_list.append(context)
                                    # 更新仍需集数
                                    need_episodes = __update_episodes(_tmdbid=need_tmdbid,
                                                                      _need=need_episodes,
                                                                      _sea=need_season,
                                                                      _current=torrent_episodes)

        # 仍然缺失的剧集，从整季中选择需要的集数文件下载，仅支持QB和TR
        if no_exists:
            # TMDBID列表
            no_exists_list = list(no_exists)
            for need_tmdbid in no_exists_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_tmdbid)
                if not need_tv:
                    continue
                # 需要季列表
                need_tv_list = list(need_tv)
                # 循环需要季
                for sea in need_tv_list:
                    # NotExistMediaInfo
                    tv = need_tv.get(sea)
                    # 当前需要季
                    need_season = sea
                    # 当前需要集
                    need_episodes = tv.episodes
                    # 没有集的不处理
                    if not need_episodes:
                        continue
                    # 循环种子
                    for context in contexts:
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 种子信息
                        torrent = context.torrent_info
                        # 非剧集不处理
                        if media.type != MediaType.TV:
                            continue
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        # 没有需要集后退出
                        if not need_episodes:
                            break
                        # 选中一个单季整季的或单季包括需要的所有集的
                        if media.tmdb_id == need_tmdbid \
                                and (not meta.episode_list
                                     or set(meta.episode_list).intersection(set(need_episodes))) \
                                and len(meta.season_list) == 1 \
                                and meta.season_list[0] == need_season:
                            # 检查种子看是否有需要的集
                            torrent_path, _, torrent_files = self.download_torrent(torrent, userid=userid)
                            if not torrent_path:
                                continue
                            # 种子全部集
                            torrent_episodes = self.torrent.get_torrent_episodes(torrent_files)
                            # 选中的集
                            selected_episodes = set(torrent_episodes).intersection(set(need_episodes))
                            if not selected_episodes:
                                logger.info(f"{torrent.site_name} - {torrent.title} 没有需要的集，跳过...")
                                continue
                            # 添加下载
                            download_id = self.download_single(context=context,
                                                               torrent_file=torrent_path,
                                                               episodes=selected_episodes,
                                                               save_path=save_path,
                                                               userid=userid)
                            if not download_id:
                                continue
                            # 把识别的集更新到上下文
                            context.meta_info.begin_episode = min(selected_episodes)
                            context.meta_info.end_episode = max(selected_episodes)
                            # 下载成功
                            downloaded_list.append(context)
                            # 更新仍需集数
                            need_episodes = __update_episodes(_tmdbid=need_tmdbid,
                                                              _need=need_episodes,
                                                              _sea=need_season,
                                                              _current=selected_episodes)

        # 返回下载的资源，剩下没下完的
        return downloaded_list, no_exists

    def get_no_exists_info(self, meta: MetaBase,
                           mediainfo: MediaInfo,
                           no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None
                           ) -> Tuple[bool, Dict[int, Dict[int, NotExistMediaInfo]]]:
        """
        检查媒体库，查询是否存在，对于剧集同时返回不存在的季集信息
        :param meta: 元数据
        :param mediainfo: 已识别的媒体信息
        :param no_exists: 在调用该方法前已经存储的不存在的季集信息，有传入时该函数搜索的内容将会叠加后输出
        :return: 当前媒体是否缺失，各标题总的季集和缺失的季集
        """

        def __append_no_exists(_season: int, _episodes: list, _total: int, _start: int):
            """
            添加不存在的季集信息
            {tmdbid: [
                "season": int,
                "episodes": list,
                "total_episode": int,
                "start_episode": int
            ]}
            """
            if not no_exists.get(mediainfo.tmdb_id):
                no_exists[mediainfo.tmdb_id] = {
                    _season: NotExistMediaInfo(
                        season=_season,
                        episodes=_episodes,
                        total_episode=_total,
                        start_episode=_start
                    )
                }
            else:
                no_exists[mediainfo.tmdb_id][_season] = NotExistMediaInfo(
                    season=_season,
                    episodes=_episodes,
                    total_episode=_total,
                    start_episode=_start
                )

        if not no_exists:
            no_exists = {}
        if mediainfo.type == MediaType.MOVIE:
            # 电影
            itemid = self.mediaserver.get_item_id(mtype=mediainfo.type.value,
                                                  tmdbid=mediainfo.tmdb_id)
            exists_movies: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if exists_movies:
                logger.info(f"媒体库中已存在电影：{mediainfo.title_year}")
                return True, {}
            return False, {}
        else:
            if not mediainfo.seasons:
                # 补充媒体信息
                mediainfo: MediaInfo = self.recognize_media(mtype=mediainfo.type,
                                                            tmdbid=mediainfo.tmdb_id)
                if not mediainfo:
                    logger.error(f"媒体信息识别失败！")
                    return False, {}
                if not mediainfo.seasons:
                    logger.error(f"媒体信息中没有季集信息：{mediainfo.title_year}")
                    return False, {}
            # 电视剧
            itemid = self.mediaserver.get_item_id(mtype=mediainfo.type.value,
                                                  tmdbid=mediainfo.tmdb_id,
                                                  season=mediainfo.season)
            exists_tvs: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if not exists_tvs:
                # 所有剧集均缺失
                for season, episodes in mediainfo.seasons.items():
                    if not episodes:
                        continue
                    # 全季不存在
                    if meta.begin_season \
                            and season not in meta.season_list:
                        continue
                    __append_no_exists(_season=season, _episodes=[], _total=len(episodes), _start=min(episodes))
                return False, no_exists
            else:
                # 存在一些，检查缺失的季集
                for season, episodes in mediainfo.seasons.items():
                    if meta.begin_season \
                            and season not in meta.season_list:
                        continue
                    if not episodes:
                        continue
                    exist_seasons = exists_tvs.seasons
                    if exist_seasons.get(season):
                        # 取差集
                        lack_episodes = list(set(episodes).difference(set(exist_seasons[season])))
                        if not lack_episodes:
                            # 全部集存在
                            continue
                        # 添加不存在的季集信息
                        __append_no_exists(_season=season, _episodes=lack_episodes,
                                           _total=len(episodes), _start=min(episodes))
                    else:
                        # 全季不存在
                        __append_no_exists(_season=season, _episodes=[],
                                           _total=len(episodes), _start=min(episodes))
            # 存在不完整的剧集
            if no_exists:
                logger.debug(f"媒体库中已存在部分剧集，缺失：{no_exists}")
                return False, no_exists
            # 全部存在
            return True, no_exists

    def remote_downloading(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        查询正在下载的任务，并发送消息
        """
        torrents = self.list_torrents(status=TorrentStatus.DOWNLOADING)
        if not torrents:
            self.post_message(Notification(
                channel=channel,
                mtype=NotificationType.Download,
                title="没有正在下载的任务！"))
            return
        # 发送消息
        title = f"共 {len(torrents)} 个任务正在下载："
        messages = []
        index = 1
        for torrent in torrents:
            messages.append(f"{index}. {torrent.title} "
                            f"{StringUtils.str_filesize(torrent.size)} "
                            f"{round(torrent.progress * 100, 1)}%")
            index += 1
        self.post_message(Notification(
            channel=channel, mtype=NotificationType.Download,
            title=title, text="\n".join(messages), userid=userid))

    def downloading(self) -> List[DownloadingTorrent]:
        """
        查询正在下载的任务
        """
        torrents = self.list_torrents(status=TorrentStatus.DOWNLOADING)
        if not torrents:
            return []
        ret_torrents = []
        for torrent in torrents:
            history = self.downloadhis.get_by_hash(torrent.hash)
            if history:
                torrent.media = {
                    "tmdbid": history.tmdbid,
                    "type": history.type,
                    "title": history.title,
                    "season": history.seasons,
                    "episode": history.episodes,
                    "image": history.image,
                }
            ret_torrents.append(torrent)
        return ret_torrents

    def set_downloading(self, hash_str, oper: str) -> bool:
        """
        控制下载任务 start/stop
        """
        if oper == "start":
            return self.start_torrents(hashs=[hash_str])
        elif oper == "stop":
            return self.stop_torrents(hashs=[hash_str])
        return False

    def remove_downloading(self, hash_str: str) -> bool:
        """
        删除下载任务
        """
        return self.remove_torrents(hashs=[hash_str])
