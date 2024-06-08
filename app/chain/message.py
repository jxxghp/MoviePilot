import copy
import json
import re
from typing import Any, Optional, Dict, Union

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.event import EventManager
from app.core.meta import MetaBase
from app.db.message_oper import MessageOper
from app.helper.message import MessageHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import Notification, NotExistMediaInfo, CommingMessage
from app.schemas.types import EventType, MessageChannel, MediaType
from app.utils.string import StringUtils

# 当前页面
_current_page: int = 0
# 当前元数据
_current_meta: Optional[MetaBase] = None
# 当前媒体信息
_current_media: Optional[MediaInfo] = None


class MessageChain(ChainBase):
    """
    外来消息处理链
    """
    # 缓存的用户数据 {userid: {type: str, items: list}}
    _cache_file = "__user_messages__"
    # 每页数据量
    _page_size: int = 8

    def __init__(self):
        super().__init__()
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.searchchain = SearchChain()
        self.mediachain = MediaChain()
        self.eventmanager = EventManager()
        self.torrenthelper = TorrentHelper()
        self.messagehelper = MessageHelper()
        self.messageoper = MessageOper()

    def __get_noexits_info(
            self,
            _meta: MetaBase,
            _mediainfo: MediaInfo) -> Dict[Union[int, str], Dict[int, NotExistMediaInfo]]:
        """
        获取缺失的媒体信息
        """
        if _mediainfo.type == MediaType.TV:
            if not _mediainfo.seasons:
                # 补充媒体信息
                _mediainfo = self.mediachain.recognize_media(mtype=_mediainfo.type,
                                                             tmdbid=_mediainfo.tmdb_id,
                                                             doubanid=_mediainfo.douban_id,
                                                             cache=False)
                if not _mediainfo:
                    logger.warn(f"{_mediainfo.tmdb_id or _mediainfo.douban_id} 媒体信息识别失败！")
                    return {}
                if not _mediainfo.seasons:
                    logger.warn(f"媒体信息中没有季集信息，"
                                f"标题：{_mediainfo.title}，"
                                f"tmdbid：{_mediainfo.tmdb_id}，doubanid：{_mediainfo.douban_id}")
                    return {}
            # KEY
            _mediakey = _mediainfo.tmdb_id or _mediainfo.douban_id
            _no_exists = {
                _mediakey: {}
            }
            if _meta.begin_season:
                # 指定季
                episodes = _mediainfo.seasons.get(_meta.begin_season)
                if not episodes:
                    return {}
                _no_exists[_mediakey][_meta.begin_season] = NotExistMediaInfo(
                    season=_meta.begin_season,
                    episodes=[],
                    total_episode=len(episodes),
                    start_episode=episodes[0]
                )
            else:
                # 所有季
                for sea, eps in _mediainfo.seasons.items():
                    if not eps:
                        continue
                    _no_exists[_mediakey][sea] = NotExistMediaInfo(
                        season=sea,
                        episodes=[],
                        total_episode=len(eps),
                        start_episode=eps[0]
                    )
        else:
            _no_exists = {}

        return _no_exists

    def process(self, body: Any, form: Any, args: Any) -> None:
        """
        调用模块识别消息内容
        """
        # 获取消息内容
        info = self.message_parser(body=body, form=form, args=args)
        if not info:
            return
        # 渠道
        channel = info.channel
        # 用户ID
        userid = info.userid
        # 用户名
        username = info.username or userid
        if not userid:
            logger.debug(f'未识别到用户ID：{body}{form}{args}')
            return
        # 消息内容
        text = str(info.text).strip() if info.text else None
        if not text:
            logger.debug(f'未识别到消息内容：：{body}{form}{args}')
            return
        # 处理消息
        self.handle_message(channel=channel, userid=userid, username=username, text=text)

    def handle_message(self, channel: MessageChannel, userid: Union[str, int], username: str, text: str) -> None:
        """
        识别消息内容，执行操作
        """
        # 申明全局变量
        global _current_page, _current_meta, _current_media
        # 加载缓存
        user_cache: Dict[str, dict] = self.load_cache(self._cache_file) or {}
        # 处理消息
        logger.info(f'收到用户消息内容，用户：{userid}，内容：{text}')
        # 保存消息
        self.messagehelper.put(
            CommingMessage(
                userid=userid,
                username=username,
                channel=channel,
                text=text
            ), role="user")
        self.messageoper.add(
            channel=channel,
            userid=username or userid,
            text=text,
            action=0
        )
        # 处理消息
        if text.startswith('/'):
            # 执行命令
            self.eventmanager.send_event(
                EventType.CommandExcute,
                {
                    "cmd": text,
                    "user": userid,
                    "channel": channel
                }
            )

        elif text.isdigit():
            # 用户选择了具体的条目
            # 缓存
            cache_data: dict = user_cache.get(userid)
            # 选择项目
            if not cache_data \
                    or not cache_data.get('items') \
                    or len(cache_data.get('items')) < int(text):
                # 发送消息
                self.post_message(Notification(channel=channel, title="输入有误！", userid=userid))
                return
            # 选择的序号
            _choice = int(text) + _current_page * self._page_size - 1
            # 缓存类型
            cache_type: str = cache_data.get('type')
            # 缓存列表
            cache_list: list = copy.deepcopy(cache_data.get('items'))
            # 选择
            if cache_type in ["Search", "ReSearch"]:
                # 当前媒体信息
                mediainfo: MediaInfo = cache_list[_choice]
                _current_media = mediainfo
                # 查询缺失的媒体信息
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=_current_meta,
                                                                              mediainfo=_current_media)
                if exist_flag and cache_type == "Search":
                    # 媒体库中已存在
                    self.post_message(
                        Notification(channel=channel,
                                     title=f"【{_current_media.title_year}"
                                           f"{_current_meta.sea} 媒体库中已存在，如需重新下载请发送：搜索 名称 或 下载 名称】",
                                     userid=userid))
                    return
                elif exist_flag:
                    # 没有缺失，但要全量重新搜索和下载
                    no_exists = self.__get_noexits_info(_current_meta, _current_media)
                # 发送缺失的媒体信息
                messages = []
                if no_exists and cache_type == "Search":
                    # 发送缺失消息
                    mediakey = mediainfo.tmdb_id or mediainfo.douban_id
                    messages = [
                        f"第 {sea} 季缺失 {StringUtils.str_series(no_exist.episodes) if no_exist.episodes else no_exist.total_episode} 集"
                        for sea, no_exist in no_exists.get(mediakey).items()]
                elif no_exists:
                    # 发送总集数的消息
                    mediakey = mediainfo.tmdb_id or mediainfo.douban_id
                    messages = [
                        f"第 {sea} 季总 {no_exist.total_episode} 集"
                        for sea, no_exist in no_exists.get(mediakey).items()]
                if messages:
                    self.post_message(Notification(channel=channel,
                                                   title=f"{mediainfo.title_year}：\n" + "\n".join(messages),
                                                   userid=userid))
                # 搜索种子，过滤掉不需要的剧集，以便选择
                logger.info(f"开始搜索 {mediainfo.title_year} ...")
                self.post_message(
                    Notification(channel=channel,
                                 title=f"开始搜索 {mediainfo.type.value} {mediainfo.title_year} ...",
                                 userid=userid))
                # 开始搜索
                contexts = self.searchchain.process(mediainfo=mediainfo,
                                                    no_exists=no_exists)
                if not contexts:
                    # 没有数据
                    self.post_message(Notification(
                        channel=channel, title=f"{mediainfo.title}"
                                               f"{_current_meta.sea} 未搜索到需要的资源！",
                        userid=userid))
                    return
                # 搜索结果排序
                contexts = self.torrenthelper.sort_torrents(contexts)
                # 判断是否设置自动下载
                auto_download_user = settings.AUTO_DOWNLOAD_USER
                # 匹配到自动下载用户
                if auto_download_user \
                        and (auto_download_user == "all"
                             or any(userid == user for user in auto_download_user.split(","))):
                    logger.info(f"用户 {userid} 在自动下载用户中，开始自动择优下载 ...")
                    # 自动选择下载
                    self.__auto_download(channel=channel,
                                         cache_list=contexts,
                                         userid=userid,
                                         username=username,
                                         no_exists=no_exists)
                else:
                    # 更新缓存
                    user_cache[userid] = {
                        "type": "Torrent",
                        "items": contexts
                    }
                    # 发送种子数据
                    logger.info(f"搜索到 {len(contexts)} 条数据，开始发送选择消息 ...")
                    self.__post_torrents_message(channel=channel,
                                                 title=mediainfo.title,
                                                 items=contexts[:self._page_size],
                                                 userid=userid,
                                                 total=len(contexts))

            elif cache_type in ["Subscribe", "ReSubscribe"]:
                # 订阅或洗版媒体
                mediainfo: MediaInfo = cache_list[_choice]
                # 洗版标识
                best_version = False
                # 查询缺失的媒体信息
                if cache_type == "Subscribe":
                    exist_flag, _ = self.downloadchain.get_no_exists_info(meta=_current_meta,
                                                                          mediainfo=mediainfo)
                    if exist_flag:
                        self.post_message(Notification(
                            channel=channel,
                            title=f"【{mediainfo.title_year}"
                                  f"{_current_meta.sea} 媒体库中已存在，如需洗版请发送：洗版 XXX】",
                            userid=userid))
                        return
                else:
                    best_version = True
                # 添加订阅，状态为N
                self.subscribechain.add(title=mediainfo.title,
                                        year=mediainfo.year,
                                        mtype=mediainfo.type,
                                        tmdbid=mediainfo.tmdb_id,
                                        season=_current_meta.begin_season,
                                        channel=channel,
                                        userid=userid,
                                        username=username,
                                        best_version=best_version)
            elif cache_type == "Torrent":
                if int(text) == 0:
                    # 自动选择下载，强制下载模式
                    self.__auto_download(channel=channel,
                                         cache_list=cache_list,
                                         userid=userid,
                                         username=username)
                else:
                    # 下载种子
                    context: Context = cache_list[_choice]
                    # 下载
                    self.downloadchain.download_single(context, channel=channel,
                                                       userid=userid, username=username)

        elif text.lower() == "p":
            # 上一页
            cache_data: dict = user_cache.get(userid)
            if not cache_data:
                # 没有缓存
                self.post_message(Notification(
                    channel=channel, title="输入有误！", userid=userid))
                return

            if _current_page == 0:
                # 第一页
                self.post_message(Notification(
                    channel=channel, title="已经是第一页了！", userid=userid))
                return
            # 减一页
            _current_page -= 1
            cache_type: str = cache_data.get('type')
            # 产生副本，避免修改原值
            cache_list: list = copy.deepcopy(cache_data.get('items'))
            if _current_page == 0:
                start = 0
                end = self._page_size
            else:
                start = _current_page * self._page_size
                end = start + self._page_size
            if cache_type == "Torrent":
                # 发送种子数据
                self.__post_torrents_message(channel=channel,
                                             title=_current_media.title,
                                             items=cache_list[start:end],
                                             userid=userid,
                                             total=len(cache_list))
            else:
                # 发送媒体数据
                self.__post_medias_message(channel=channel,
                                           title=_current_meta.name,
                                           items=cache_list[start:end],
                                           userid=userid,
                                           total=len(cache_list))

        elif text.lower() == "n":
            # 下一页
            cache_data: dict = user_cache.get(userid)
            if not cache_data:
                # 没有缓存
                self.post_message(Notification(
                    channel=channel, title="输入有误！", userid=userid))
                return
            cache_type: str = cache_data.get('type')
            # 产生副本，避免修改原值
            cache_list: list = copy.deepcopy(cache_data.get('items'))
            total = len(cache_list)
            # 加一页
            cache_list = cache_list[
                         (_current_page + 1) * self._page_size:(_current_page + 2) * self._page_size]
            if not cache_list:
                # 没有数据
                self.post_message(Notification(
                    channel=channel, title="已经是最后一页了！", userid=userid))
                return
            else:
                # 加一页
                _current_page += 1
                if cache_type == "Torrent":
                    # 发送种子数据
                    self.__post_torrents_message(channel=channel,
                                                 title=_current_media.title,
                                                 items=cache_list, userid=userid, total=total)
                else:
                    # 发送媒体数据
                    self.__post_medias_message(channel=channel,
                                               title=_current_meta.name,
                                               items=cache_list, userid=userid, total=total)

        else:
            # 搜索或订阅
            if text.startswith("订阅"):
                # 订阅
                content = re.sub(r"订阅[:：\s]*", "", text)
                action = "Subscribe"
            elif text.startswith("洗版"):
                # 洗版
                content = re.sub(r"洗版[:：\s]*", "", text)
                action = "ReSubscribe"
            elif text.startswith("搜索") or text.startswith("下载"):
                # 重新搜索/下载
                content = re.sub(r"(搜索|下载)[:：\s]*", "", text)
                action = "ReSearch"
            elif text.startswith("#") \
                    or re.search(r"^请[问帮你]", text) \
                    or re.search(r"[?？]$", text) \
                    or StringUtils.count_words(text) > 10 \
                    or text.find("继续") != -1:
                # 聊天
                content = text
                action = "chat"
            else:
                # 搜索
                content = text
                action = "Search"

            if action != "chat":
                # 搜索
                meta, medias = self.mediachain.search(content)
                # 识别
                if not meta.name:
                    self.post_message(Notification(
                        channel=channel, title="无法识别输入内容！", userid=userid))
                    return
                # 开始搜索
                if not medias:
                    self.post_message(Notification(
                        channel=channel, title=f"{meta.name} 没有找到对应的媒体信息！", userid=userid))
                    return
                logger.info(f"搜索到 {len(medias)} 条相关媒体信息")
                # 记录当前状态
                _current_meta = meta
                user_cache[userid] = {
                    'type': action,
                    'items': medias
                }
                _current_page = 0
                _current_media = None
                # 发送媒体列表
                self.__post_medias_message(channel=channel,
                                           title=meta.name,
                                           items=medias[:self._page_size],
                                           userid=userid, total=len(medias))
            else:
                # 广播事件
                self.eventmanager.send_event(
                    EventType.UserMessage,
                    {
                        "text": content,
                        "userid": userid,
                        "channel": channel
                    }
                )

        # 保存缓存
        self.save_cache(user_cache, self._cache_file)

    def __auto_download(self, channel: MessageChannel, cache_list: list[Context],
                        userid: Union[str, int], username: str,
                        no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None):
        """
        自动择优下载
        """
        if no_exists is None:
            # 查询缺失的媒体信息
            exist_flag, no_exists = self.downloadchain.get_no_exists_info(
                meta=_current_meta,
                mediainfo=_current_media
            )
            if exist_flag:
                # 媒体库中已存在，查询全量
                no_exists = self.__get_noexits_info(_current_meta, _current_media)

        # 批量下载
        downloads, lefts = self.downloadchain.batch_download(contexts=cache_list,
                                                             no_exists=no_exists,
                                                             channel=channel,
                                                             userid=userid,
                                                             username=username)
        if downloads and not lefts:
            # 全部下载完成
            logger.info(f'{_current_media.title_year} 下载完成')
        else:
            # 未完成下载
            logger.info(f'{_current_media.title_year} 未下载未完整，添加订阅 ...')
            if downloads and _current_media.type == MediaType.TV:
                # 获取已下载剧集
                downloaded = [download.meta_info.begin_episode for download in downloads
                              if download.meta_info.begin_episode]
                note = json.dumps(downloaded)
            else:
                note = None
            # 添加订阅，状态为R
            self.subscribechain.add(title=_current_media.title,
                                    year=_current_media.year,
                                    mtype=_current_media.type,
                                    tmdbid=_current_media.tmdb_id,
                                    season=_current_meta.begin_season,
                                    channel=channel,
                                    userid=userid,
                                    username=username,
                                    state="R",
                                    note=note)

    def __post_medias_message(self, channel: MessageChannel,
                              title: str, items: list, userid: str, total: int):
        """
        发送媒体列表消息
        """
        if total > self._page_size:
            title = f"【{title}】共找到{total}条相关信息，请回复对应数字选择（p: 上一页 n: 下一页）"
        else:
            title = f"【{title}】共找到{total}条相关信息，请回复对应数字选择"
        self.post_medias_message(Notification(
            channel=channel,
            title=title,
            userid=userid
        ), medias=items)

    def __post_torrents_message(self, channel: MessageChannel, title: str, items: list,
                                userid: str, total: int):
        """
        发送种子列表消息
        """
        if total > self._page_size:
            title = f"【{title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择 p: 上一页 n: 下一页）"
        else:
            title = f"【{title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择）"
        self.post_torrents_message(Notification(
            channel=channel,
            title=title,
            userid=userid,
            link=settings.MP_DOMAIN('#/resource')
        ), torrents=items)
