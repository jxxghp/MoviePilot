import base64
import copy
import json
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple, Set, Dict, Union

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.mediaserver_oper import MediaServerOper
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import ExistMediaInfo, NotExistMediaInfo, DownloadingTorrent, Notification
from app.schemas.types import MediaType, TorrentStatus, EventType, MessageChannel, NotificationType
from app.utils.http import RequestUtils
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
        self.directoryhelper = DirectoryHelper()
        self.messagehelper = MessageHelper()

    def post_download_message(self, meta: MetaBase, mediainfo: MediaInfo, torrent: TorrentInfo,
                              channel: MessageChannel = None, userid: str = None, username: str = None,
                              download_episodes: str = None):
        """
        发送添加下载的消息
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param torrent: 种子信息
        :param channel: 通知渠道
        :param userid: 用户ID，指定时精确发送对应用户
        :param username: 通知显示的下载用户信息
        :param download_episodes: 下载的集数
        """
        msg_text = ""
        if username:
            msg_text = f"用户：{username}"
        if torrent.site_name:
            msg_text = f"{msg_text}\n站点：{torrent.site_name}"
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
        if torrent.pubdate:
            msg_text = f"{msg_text}\n发布时间：{torrent.pubdate}"
        if torrent.freedate:
            msg_text = f"{msg_text}\n免费时间：{StringUtils.diff_time_str(torrent.freedate)}"
        if torrent.seeders:
            msg_text = f"{msg_text}\n做种数：{torrent.seeders}"
        if torrent.uploadvolumefactor and torrent.downloadvolumefactor:
            msg_text = f"{msg_text}\n促销：{torrent.volume_factor}"
        if torrent.hit_and_run:
            msg_text = f"{msg_text}\nHit&Run：是"
        if torrent.labels:
            msg_text = f"{msg_text}\n标签：{' '.join(torrent.labels)}"
        if torrent.description:
            html_re = re.compile(r'<[^>]+>', re.S)
            description = html_re.sub('', torrent.description)
            torrent.description = re.sub(r'<[^>]+>', '', description)
            msg_text = f"{msg_text}\n描述：{torrent.description}"

        self.post_message(Notification(
            channel=channel,
            mtype=NotificationType.Download,
            userid=userid,
            title=f"{mediainfo.title_year} "
                  f"{'%s %s' % (meta.season, download_episodes) if download_episodes else meta.season_episode} 开始下载",
            text=msg_text,
            image=mediainfo.get_message_image(),
            link=settings.MP_DOMAIN('/#/downloading')))

    def download_torrent(self, torrent: TorrentInfo,
                         channel: MessageChannel = None,
                         userid: Union[str, int] = None
                         ) -> Tuple[Optional[Union[Path, str]], str, list]:
        """
        下载种子文件，如果是磁力链，会返回磁力链接本身
        :return: 种子路径，种子目录名，种子文件清单
        """

        def __get_redict_url(url: str, ua: str = None, cookie: str = None) -> Optional[str]:
            """
            获取下载链接， url格式：[base64]url
            """
            # 获取[]中的内容
            m = re.search(r"\[(.*)](.*)", url)
            if m:
                # 参数
                base64_str = m.group(1)
                # URL
                url = m.group(2)
                if not base64_str:
                    return url
                # 解码参数
                req_str = base64.b64decode(base64_str.encode('utf-8')).decode('utf-8')
                req_params: Dict[str, dict] = json.loads(req_str)
                # 是否使用cookie
                if not req_params.get('cookie'):
                    cookie = None
                # 请求头
                if req_params.get('header'):
                    headers = req_params.get('header')
                else:
                    headers = None
                if req_params.get('method') == 'get':
                    # GET请求
                    res = RequestUtils(
                        ua=ua,
                        cookies=cookie,
                        headers=headers
                    ).get_res(url, params=req_params.get('params'))
                else:
                    # POST请求
                    res = RequestUtils(
                        ua=ua,
                        cookies=cookie,
                        headers=headers
                    ).post_res(url, params=req_params.get('params'))
                if not res:
                    return None
                if not req_params.get('result'):
                    return res.text
                else:
                    data = res.json()
                    for key in str(req_params.get('result')).split("."):
                        data = data.get(key)
                        if not data:
                            return None
                    logger.info(f"获取到下载地址：{data}")
                    return data
            return None

        # 获取下载链接
        if not torrent.enclosure:
            return None, "", []
        if torrent.enclosure.startswith("magnet:"):
            return torrent.enclosure, "", []
        # Cookie
        site_cookie = torrent.site_cookie
        if torrent.enclosure.startswith("["):
            # 需要解码获取下载地址
            torrent_url = __get_redict_url(url=torrent.enclosure,
                                           ua=torrent.site_ua,
                                           cookie=site_cookie)
            # 涉及解析地址的不使用Cookie下载种子，否则MT会出错
            site_cookie = None
        else:
            torrent_url = torrent.enclosure
        if not torrent_url:
            logger.error(f"{torrent.title} 无法获取下载地址：{torrent.enclosure}！")
            return None, "", []
        # 下载种子文件
        torrent_file, content, download_folder, files, error_msg = self.torrent.download_torrent(
            url=torrent_url,
            cookie=site_cookie,
            ua=torrent.site_ua,
            proxy=torrent.site_proxy)

        if isinstance(content, str):
            # 磁力链
            return content, "", []

        if not torrent_file:
            logger.error(f"下载种子文件失败：{torrent.title} - {torrent_url}")
            self.post_message(Notification(
                channel=channel,
                mtype=NotificationType.Manual,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{error_msg}\n站点：{torrent.site_name}",
                userid=userid))
            return None, "", []

        # 返回 种子文件路径，种子目录名，种子文件清单
        return torrent_file, download_folder, files

    def download_single(self, context: Context, torrent_file: Path = None,
                        episodes: Set[int] = None,
                        channel: MessageChannel = None,
                        save_path: str = None,
                        userid: Union[str, int] = None,
                        username: str = None) -> Optional[str]:
        """
        下载及发送通知
        :param context: 资源上下文
        :param torrent_file: 种子文件路径
        :param episodes: 需要下载的集数
        :param channel: 通知渠道
        :param save_path: 保存路径
        :param userid: 用户ID
        :param username: 调用下载的用户名/插件名
        """
        _torrent = context.torrent_info
        _media = context.media_info
        _meta = context.meta_info

        # 补充完整的media数据
        if not _media.genre_ids:
            new_media = self.recognize_media(mtype=_media.type, tmdbid=_media.tmdb_id,
                                             doubanid=_media.douban_id, bangumiid=_media.bangumi_id)
            if new_media:
                _media = new_media

        # 实际下载的集数
        download_episodes = StringUtils.format_ep(list(episodes)) if episodes else None
        _folder_name = ""
        if not torrent_file:
            # 下载种子文件，得到的可能是文件也可能是磁力链
            content, _folder_name, _file_list = self.download_torrent(_torrent,
                                                                      channel=channel,
                                                                      userid=userid)
            if not content:
                return None
        else:
            content = torrent_file
            # 获取种子文件的文件夹名和文件清单
            _folder_name, _file_list = self.torrent.get_torrent_info(torrent_file)

        # 下载目录
        if save_path:
            # 有自定义下载目录时，尝试匹配目录配置
            dir_info = self.directoryhelper.get_download_dir(_media, to_path=Path(save_path))
        else:
            # 根据媒体信息查询下载目录配置
            dir_info = self.directoryhelper.get_download_dir(_media)
        # 拼装子目录
        if dir_info:
            # 一级目录
            if not dir_info.media_type and dir_info.auto_category:
                # 一级自动分类
                download_dir = Path(dir_info.path) / _media.type.value
            else:
                # 一级不分类
                download_dir = Path(dir_info.path)

            # 二级目录
            if not dir_info.category and dir_info.auto_category and _media and _media.category:
                # 二级自动分类
                download_dir = download_dir / _media.category
        elif save_path:
            # 自定义下载目录
            download_dir = Path(save_path)
        else:
            # 未找到下载目录，且没有自定义下载目录
            logger.error(f"未找到下载目录：{_media.type.value} {_media.title_year}")
            self.messagehelper.put(f"{_media.type.value} {_media.title_year} 未找到下载目录！",
                                   title="下载失败", role="system")
            return None

        # 添加下载
        result: Optional[tuple] = self.download(content=content,
                                                cookie=_torrent.site_cookie,
                                                episodes=episodes,
                                                download_dir=download_dir,
                                                category=_media.category)
        if result:
            _hash, error_msg = result
        else:
            _hash, error_msg = None, "未知错误"

        if _hash:
            # 下载文件路径
            if _folder_name:
                download_path = download_dir / _folder_name
            else:
                download_path = download_dir / _file_list[0] if _file_list else download_dir

            # 登记下载记录
            self.downloadhis.add(
                path=str(download_path),
                type=_media.type.value,
                title=_media.title,
                year=_media.year,
                tmdbid=_media.tmdb_id,
                imdbid=_media.imdb_id,
                tvdbid=_media.tvdb_id,
                doubanid=_media.douban_id,
                seasons=_meta.season,
                episodes=download_episodes or _meta.episode,
                image=_media.get_backdrop_image(),
                download_hash=_hash,
                torrent_name=_torrent.title,
                torrent_description=_torrent.description,
                torrent_site=_torrent.site_name,
                userid=userid,
                username=username,
                channel=channel.value if channel else None,
                date=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            )

            # 登记下载文件
            files_to_add = []
            for file in _file_list:
                if episodes:
                    # 识别文件集
                    file_meta = MetaInfo(Path(file).stem)
                    if not file_meta.begin_episode \
                            or file_meta.begin_episode not in episodes:
                        continue
                # 只处理视频格式
                if not Path(file).suffix \
                        or Path(file).suffix not in settings.RMT_MEDIAEXT:
                    continue
                files_to_add.append({
                    "download_hash": _hash,
                    "downloader": settings.DEFAULT_DOWNLOADER,
                    "fullpath": str(download_dir / _folder_name / file),
                    "savepath": str(download_dir / _folder_name),
                    "filepath": file,
                    "torrentname": _meta.org_string,
                })
            if files_to_add:
                self.downloadhis.add_files(files_to_add)

            # 发送消息（群发，不带channel和userid）
            self.post_download_message(meta=_meta, mediainfo=_media, torrent=_torrent,
                                       username=username, download_episodes=download_episodes)
            # 下载成功后处理
            self.download_added(context=context, download_dir=download_dir, torrent_path=torrent_file)
            # 广播事件
            self.eventmanager.send_event(EventType.DownloadAdded, {
                "hash": _hash,
                "context": context,
                "username": username
            })
        else:
            # 下载失败
            logger.error(f"{_media.title_year} 添加下载任务失败："
                         f"{_torrent.title} - {_torrent.enclosure}，{error_msg}")
            # 只发送给对应渠道和用户
            self.post_message(Notification(
                channel=channel,
                mtype=NotificationType.Manual,
                title="添加下载任务失败：%s %s"
                      % (_media.title_year, _meta.season_episode),
                text=f"站点：{_torrent.site_name}\n"
                     f"种子名称：{_meta.org_string}\n"
                     f"错误信息：{error_msg}",
                image=_media.get_message_image(),
                userid=userid))
        return _hash

    def batch_download(self,
                       contexts: List[Context],
                       no_exists: Dict[Union[int, str], Dict[int, NotExistMediaInfo]] = None,
                       save_path: str = None,
                       channel: MessageChannel = None,
                       userid: str = None,
                       username: str = None
                       ) -> Tuple[List[Context], Dict[Union[int, str], Dict[int, NotExistMediaInfo]]]:
        """
        根据缺失数据，自动种子列表中组合择优下载
        :param contexts:  资源上下文列表
        :param no_exists:  缺失的剧集信息
        :param save_path:  保存路径
        :param channel:  通知渠道
        :param userid:  用户ID
        :param username: 调用下载的用户名/插件名
        :return: 已经下载的资源列表、剩余未下载到的剧集 no_exists[tmdb_id/douban_id] = {season: NotExistMediaInfo}
        """
        # 已下载的项目
        downloaded_list: List[Context] = []

        def __update_seasons(_mid: Union[int, str], _need: list, _current: list) -> list:
            """
            更新need_tvs季数，返回剩余季数
            :param _mid: TMDBID
            :param _need: 需要下载的季数
            :param _current: 已经下载的季数
            """
            # 剩余季数
            need = list(set(_need).difference(set(_current)))
            # 清除已下载的季信息
            seas = copy.deepcopy(no_exists.get(_mid))
            if seas:
                for _sea in list(seas):
                    if _sea not in need:
                        no_exists[_mid].pop(_sea)
                    if not no_exists.get(_mid) and no_exists.get(_mid) is not None:
                        no_exists.pop(_mid)
                        break
            return need

        def __update_episodes(_mid: Union[int, str], _sea: int, _need: list, _current: set) -> list:
            """
            更新need_tvs集数，返回剩余集数
            :param _mid: TMDBID
            :param _sea: 季数
            :param _need: 需要下载的集数
            :param _current: 已经下载的集数
            """
            # 剩余集数
            need = list(set(_need).difference(set(_current)))
            if need:
                not_exist = no_exists[_mid][_sea]
                no_exists[_mid][_sea] = NotExistMediaInfo(
                    season=not_exist.season,
                    episodes=need,
                    total_episode=not_exist.total_episode,
                    start_episode=not_exist.start_episode
                )
            else:
                no_exists[_mid].pop(_sea)
                if not no_exists.get(_mid) and no_exists.get(_mid) is not None:
                    no_exists.pop(_mid)
            return need

        def __get_season_episodes(_mid: Union[int, str], season: int) -> int:
            """
            获取需要的季的集数
            """
            if not no_exists.get(_mid):
                return 9999
            no_exist = no_exists.get(_mid)
            if not no_exist.get(season):
                return 9999
            return no_exist[season].total_episode

        # 分组排序
        contexts = TorrentHelper().sort_group_torrents(contexts)

        # 如果是电影，直接下载
        for context in contexts:
            if context.media_info.type == MediaType.MOVIE:
                logger.info(f"开始下载电影 {context.torrent_info.title} ...")
                if self.download_single(context, save_path=save_path, channel=channel,
                                        userid=userid, username=username):
                    # 下载成功
                    logger.info(f"{context.torrent_info.title} 添加下载成功")
                    downloaded_list.append(context)

        # 电视剧整季匹配
        logger.info(f"开始匹配电视剧整季：{no_exists}")
        if no_exists:
            # 先把整季缺失的拿出来，看是否刚好有所有季都满足的种子 {tmdbid: [seasons]}
            need_seasons: Dict[int, list] = {}
            for need_mid, need_tv in no_exists.items():
                for tv in need_tv.values():
                    if not tv:
                        continue
                    # 季列表为空的，代表全季缺失
                    if not tv.episodes:
                        if not need_seasons.get(need_mid):
                            need_seasons[need_mid] = []
                        need_seasons[need_mid].append(tv.season or 1)
            logger.info(f"缺失整季：{need_seasons}")
            # 查找整季包含的种子，只处理整季没集的种子或者是集数超过季的种子
            for need_mid, need_season in need_seasons.items():
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
                    # 没有季的默认为第1季
                    if not torrent_season:
                        torrent_season = [1]
                    # 种子有集的不要
                    if meta.episode_list:
                        continue
                    # 匹配TMDBID
                    if need_mid == media.tmdb_id or need_mid == media.douban_id:
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        # 种子季是需要季或者子集
                        if set(torrent_season).issubset(set(need_season)):
                            if len(torrent_season) == 1:
                                # 只有一季的可能是命名错误，需要打开种子鉴别，只有实际集数大于等于总集数才下载
                                logger.info(f"开始下载种子 {torrent.title} ...")
                                content, _, torrent_files = self.download_torrent(torrent)
                                if not content:
                                    logger.warn(f"{torrent.title} 种子下载失败！")
                                    continue
                                if isinstance(content, str):
                                    logger.warn(f"{meta.org_string} 下载地址是磁力链，无法确定种子文件集数")
                                    continue
                                torrent_episodes = self.torrent.get_torrent_episodes(torrent_files)
                                logger.info(f"{meta.org_string} 解析种子文件集数为 {torrent_episodes}")
                                if not torrent_episodes:
                                    continue
                                # 更新集数范围
                                begin_ep = min(torrent_episodes)
                                end_ep = max(torrent_episodes)
                                meta.set_episodes(begin=begin_ep, end=end_ep)
                                # 需要总集数
                                need_total = __get_season_episodes(need_mid, torrent_season[0])
                                if len(torrent_episodes) < need_total:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数发现不是完整合集，先放弃这个种子")
                                    continue
                                else:
                                    # 下载
                                    logger.info(f"开始下载 {torrent.title} ...")
                                    download_id = self.download_single(
                                        context=context,
                                        torrent_file=content if isinstance(content, Path) else None,
                                        save_path=save_path,
                                        channel=channel,
                                        userid=userid,
                                        username=username
                                    )
                            else:
                                # 下载
                                logger.info(f"开始下载 {torrent.title} ...")
                                download_id = self.download_single(context,
                                                                   save_path=save_path, channel=channel,
                                                                   userid=userid, username=username)

                            if download_id:
                                # 下载成功
                                logger.info(f"{torrent.title} 添加下载成功")
                                downloaded_list.append(context)
                                # 更新仍需季集
                                need_season = __update_seasons(_mid=need_mid,
                                                               _need=need_season,
                                                               _current=torrent_season)
                                logger.info(f"{need_mid} 剩余需要季：{need_season}")
                                if not need_season:
                                    # 全部下载完成
                                    break
        # 电视剧季内的集匹配
        logger.info(f"开始电视剧完整集匹配：{no_exists}")
        if no_exists:
            # TMDBID列表
            need_tv_list = list(no_exists)
            for need_mid in need_tv_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_mid)
                if not need_tv:
                    continue
                need_tv_copy = copy.deepcopy(no_exists.get(need_mid))
                # 循环每一季
                for sea, tv in need_tv_copy.items():
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
                        need_episodes = list(range(start_episode, total_episode + 1))
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
                        if media.tmdb_id == need_mid or media.douban_id == need_mid:
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
                                logger.info(f"开始下载 {meta.title} ...")
                                download_id = self.download_single(context,
                                                                   save_path=save_path, channel=channel,
                                                                   userid=userid, username=username)
                                if download_id:
                                    # 下载成功
                                    logger.info(f"{meta.title} 添加下载成功")
                                    downloaded_list.append(context)
                                    # 更新仍需集数
                                    need_episodes = __update_episodes(_mid=need_mid,
                                                                      _need=need_episodes,
                                                                      _sea=need_season,
                                                                      _current=torrent_episodes)
                                    logger.info(f"季 {need_season} 剩余需要集：{need_episodes}")

        # 仍然缺失的剧集，从整季中选择需要的集数文件下载，仅支持QB和TR
        logger.info(f"开始电视剧多集拆包匹配：{no_exists}")
        if no_exists:
            # TMDBID列表
            no_exists_list = list(no_exists)
            for need_mid in no_exists_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_mid)
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
                        if (media.tmdb_id == need_mid or media.douban_id == need_mid) \
                                and (not meta.episode_list
                                     or set(meta.episode_list).intersection(set(need_episodes))) \
                                and len(meta.season_list) == 1 \
                                and meta.season_list[0] == need_season:
                            # 检查种子看是否有需要的集
                            logger.info(f"开始下载种子 {torrent.title} ...")
                            content, _, torrent_files = self.download_torrent(torrent)
                            if not content:
                                logger.info(f"{torrent.title} 种子下载失败！")
                                continue
                            if isinstance(content, str):
                                logger.warn(f"{meta.org_string} 下载地址是磁力链，无法解析种子文件集数")
                                continue
                            # 种子全部集
                            torrent_episodes = self.torrent.get_torrent_episodes(torrent_files)
                            logger.info(f"{torrent.site_name} - {meta.org_string} 解析种子文件集数：{torrent_episodes}")
                            # 选中的集
                            selected_episodes = set(torrent_episodes).intersection(set(need_episodes))
                            if not selected_episodes:
                                logger.info(f"{torrent.site_name} - {torrent.title} 没有需要的集，跳过...")
                                continue
                            logger.info(f"{torrent.site_name} - {torrent.title} 选中集数：{selected_episodes}")
                            # 添加下载
                            logger.info(f"开始下载 {torrent.title} ...")
                            download_id = self.download_single(
                                context=context,
                                torrent_file=content if isinstance(content, Path) else None,
                                episodes=selected_episodes,
                                save_path=save_path,
                                channel=channel,
                                userid=userid,
                                username=username
                            )
                            if not download_id:
                                continue
                            # 下载成功
                            logger.info(f"{torrent.title} 添加下载成功")
                            downloaded_list.append(context)
                            # 更新种子集数范围
                            begin_ep = min(torrent_episodes)
                            end_ep = max(torrent_episodes)
                            meta.set_episodes(begin=begin_ep, end=end_ep)
                            # 更新仍需集数
                            need_episodes = __update_episodes(_mid=need_mid,
                                                              _need=need_episodes,
                                                              _sea=need_season,
                                                              _current=selected_episodes)
                            logger.info(f"季 {need_season} 剩余需要集：{need_episodes}")

        # 返回下载的资源，剩下没下完的
        logger.info(f"成功下载种子数：{len(downloaded_list)}，剩余未下载的剧集：{no_exists}")
        return downloaded_list, no_exists

    def get_no_exists_info(self, meta: MetaBase,
                           mediainfo: MediaInfo,
                           no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                           totals: Dict[int, int] = None
                           ) -> Tuple[bool, Dict[Union[int, str], Dict[int, NotExistMediaInfo]]]:
        """
        检查媒体库，查询是否存在，对于剧集同时返回不存在的季集信息
        :param meta: 元数据
        :param mediainfo: 已识别的媒体信息
        :param no_exists: 在调用该方法前已经存储的不存在的季集信息，有传入时该函数搜索的内容将会叠加后输出
        :param totals: 电视剧每季的总集数
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
            mediakey = mediainfo.tmdb_id or mediainfo.douban_id
            if not no_exists.get(mediakey):
                no_exists[mediakey] = {
                    _season: NotExistMediaInfo(
                        season=_season,
                        episodes=_episodes,
                        total_episode=_total,
                        start_episode=_start
                    )
                }
            else:
                no_exists[mediakey][_season] = NotExistMediaInfo(
                    season=_season,
                    episodes=_episodes,
                    total_episode=_total,
                    start_episode=_start
                )

        if not no_exists:
            no_exists = {}

        if not totals:
            totals = {}

        if mediainfo.type == MediaType.MOVIE:
            # 电影
            itemid = self.mediaserver.get_item_id(mtype=mediainfo.type.value,
                                                  title=mediainfo.title,
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
                                                            tmdbid=mediainfo.tmdb_id,
                                                            doubanid=mediainfo.douban_id)
                if not mediainfo:
                    logger.error(f"媒体信息识别失败！")
                    return False, {}
                if not mediainfo.seasons:
                    logger.error(f"媒体信息中没有季集信息：{mediainfo.title_year}")
                    return False, {}
            # 电视剧
            itemid = self.mediaserver.get_item_id(mtype=mediainfo.type.value,
                                                  title=mediainfo.title,
                                                  tmdbid=mediainfo.tmdb_id,
                                                  season=mediainfo.season)
            # 媒体库已存在的剧集
            exists_tvs: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if not exists_tvs:
                # 所有季集均缺失
                for season, episodes in mediainfo.seasons.items():
                    if not episodes:
                        continue
                    # 全季不存在
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    # 总集数
                    total_ep = totals.get(season) or len(episodes)
                    __append_no_exists(_season=season, _episodes=[],
                                       _total=total_ep, _start=min(episodes))
                return False, no_exists
            else:
                # 存在一些，检查每季缺失的季集
                for season, episodes in mediainfo.seasons.items():
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    if not episodes:
                        continue
                    # 该季总集数
                    season_total = totals.get(season) or len(episodes)
                    # 该季已存在的集
                    exist_episodes = exists_tvs.seasons.get(season)
                    if exist_episodes:
                        # 已存在取差集
                        if totals.get(season):
                            # 按总集数计算缺失集（开始集为TMDB中的最小集）
                            lack_episodes = list(set(range(min(episodes),
                                                           season_total + min(episodes))
                                                     ).difference(set(exist_episodes)))
                        else:
                            # 按TMDB集数计算缺失集
                            lack_episodes = list(set(episodes).difference(set(exist_episodes)))
                        if not lack_episodes:
                            # 全部集存在
                            continue
                        # 添加不存在的季集信息
                        __append_no_exists(_season=season, _episodes=lack_episodes,
                                           _total=season_total, _start=min(lack_episodes))
                    else:
                        # 全季不存在
                        __append_no_exists(_season=season, _episodes=[],
                                           _total=season_total, _start=min(episodes))
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
                title="没有正在下载的任务！",
                userid=userid,
                link=settings.MP_DOMAIN('#/downloading')
            ))
            return
        # 发送消息
        title = f"共 {len(torrents)} 个任务正在下载："
        messages = []
        index = 1
        for torrent in torrents:
            messages.append(f"{index}. {torrent.title} "
                            f"{StringUtils.str_filesize(torrent.size)} "
                            f"{round(torrent.progress, 1)}%")
            index += 1
        self.post_message(Notification(
            channel=channel,
            mtype=NotificationType.Download,
            title=title,
            text="\n".join(messages),
            userid=userid,
            link=settings.MP_DOMAIN('#/downloading')
        ))

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
                # 媒体信息
                torrent.media = {
                    "tmdbid": history.tmdbid,
                    "type": history.type,
                    "title": history.title,
                    "season": history.seasons,
                    "episode": history.episodes,
                    "image": history.image,
                }
                # 下载用户
                torrent.userid = history.userid
                torrent.username = history.username
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

    @eventmanager.register(EventType.DownloadFileDeleted)
    def download_file_deleted(self, event: Event):
        """
        下载文件删除时，同步删除下载任务
        """
        if not event:
            return
        hash_str = event.event_data.get("hash")
        if not hash_str:
            return
        logger.warn(f"检测到下载源文件被删除，删除下载任务（不含文件）：{hash_str}")
        # 先查询种子
        torrents: List[schemas.TransferTorrent] = self.list_torrents(hashs=[hash_str])
        if torrents:
            self.remove_torrents(hashs=[hash_str], delete_file=False)
            # 发出下载任务删除事件，如需处理辅种，可监听该事件
            self.eventmanager.send_event(EventType.DownloadDeleted, {
                "hash": hash_str,
                "torrents": [torrent.dict() for torrent in torrents]
            })
        else:
            logger.info(f"没有在下载器中查询到 {hash_str} 对应的下载任务")
