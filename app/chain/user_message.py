from typing import Dict

from fastapi import Request

from app.chain import _ChainBase
from app.chain.common import *
from app.chain.search import SearchChain
from app.core import MediaInfo, TorrentInfo, MetaInfo
from app.db.subscribes import Subscribes
from app.log import logger
from app.utils.types import EventType


class UserMessageChain(_ChainBase):
    """
    外来消息处理链
    """
    # 缓存的用户数据 {userid: {type: str, items: list}}
    _user_cache: Dict[str, dict] = {}
    # 每页数据量
    _page_size: int = 8
    # 当前页面
    _current_page: int = 0
    # 当前元数据
    _current_meta: Optional[MetaInfo] = None
    # 当前媒体信息
    _current_media: Optional[MediaInfo] = None

    def __init__(self):
        super().__init__()
        self.common = CommonChain()
        self.subscribes = Subscribes()
        self.searchchain = SearchChain()
        self.torrent = TorrentHelper()

    def process(self, request: Request, *args, **kwargs) -> None:
        """
        识别消息内容，执行操作
        """
        # 获取消息内容
        info: dict = self.run_module('message_parser', request=request)
        if not info:
            return
        # 用户ID
        userid = info.get('userid')
        if not userid:
            logger.debug(f'未识别到用户ID：{request}')
            return
        # 消息内容
        text = str(info.get('text')).strip() if info.get('text') else None
        if not text:
            logger.debug(f'未识别到消息内容：{request}')
            return
        logger.info(f'收到用户消息内容，用户：{userid}，内容：{text}')
        if text.startswith('/'):
            # 执行命令
            self.eventmanager.send_event(
                EventType.CommandExcute,
                {
                    "cmd": text
                }
            )
        elif text.isdigit():
            # 缓存
            cache_data: dict = self._user_cache.get(userid)
            # 选择项目
            if not cache_data \
                    or not cache_data.get('items') \
                    or len(cache_data.get('items')) < int(text):
                # 发送消息
                self.common.post_message(title="输入有误！", userid=userid)
                return
            # 缓存类型
            cache_type: str = cache_data.get('type')
            # 缓存列表
            cache_list: list = cache_data.get('items')
            # 选择
            if cache_type == "Search":
                mediainfo: MediaInfo = cache_list[int(text) - 1]
                self._current_media = mediainfo
                # 检查是否已存在
                exists: list = self.run_module('media_exists', mediainfo=mediainfo)
                if exists:
                    # 已存在
                    self.common.post_message(
                        title=f"{mediainfo.type.value} {mediainfo.get_title_string()} 媒体库中已存在", userid=userid)
                    return
                # 搜索种子
                contexts = self.searchchain.process(meta=self._current_meta, mediainfo=mediainfo)
                if not contexts:
                    # 没有数据
                    self.common.post_message(title=f"{mediainfo.title} 未搜索到资源！", userid=userid)
                    return
                # 更新缓存
                self._user_cache[userid] = {
                    "type": "Torrent",
                    "items": contexts
                }
                self._current_page = 0
                # 发送种子数据
                self.__post_torrents_message(items=contexts[:self._page_size], userid=userid)

            elif cache_type == "Subscribe":
                # 订阅媒体
                mediainfo: MediaInfo = cache_list[int(text) - 1]
                self._current_media = mediainfo
                state, msg = self.subscribes.add(mediainfo,
                                                 season=self._current_meta.begin_season,
                                                 episode=self._current_meta.begin_episode)
                if state:
                    # 订阅成功
                    self.common.post_message(
                        title=f"{mediainfo.get_title_string()} 已添加订阅",
                        image=mediainfo.get_message_image(),
                        userid=userid)
                else:
                    # 订阅失败
                    self.common.post_message(title=f"{mediainfo.title} 添加订阅失败：{msg}", userid=userid)
            elif cache_type == "Torrent":
                if int(text) == 0:
                    # 自动选择下载
                    # 查询缺失的媒体信息
                    exist_flag, no_exists = self.common.get_no_exists_info(mediainfo=self._current_media)
                    if exist_flag:
                        self.common.post_message(title=f"{self._current_media.get_title_string()} 媒体库中已存在",
                                                 userid=userid)
                        return
                    # 批量下载
                    self.common.batch_download(contexts=cache_list, need_tvs=no_exists, userid=userid)
                else:
                    # 下载种子
                    torrent: TorrentInfo = cache_list[int(text) - 1]
                    meta: MetaBase = MetaInfo(torrent.title)
                    torrent_file, _, _, _, error_msg = self.torrent.download_torrent(
                        url=torrent.enclosure,
                        cookie=torrent.site_cookie,
                        ua=torrent.site_ua,
                        proxy=torrent.site_proxy)
                    if not torrent_file:
                        logger.error(f"下载种子文件失败：{torrent.title} - {torrent.enclosure}")
                        self.run_module('post_message',
                                        title=f"{torrent.title} 种子下载失败！",
                                        text=f"错误信息：{error_msg}\n种子链接：{torrent.enclosure}",
                                        userid=userid)
                        return
                    # 添加下载
                    result: Optional[tuple] = self.run_module("download",
                                                              torrent_path=torrent_file,
                                                              cookie=torrent.site_cookie)
                    if result:
                        state, msg = result
                    else:
                        state, msg = False, "未知错误"
                    # 发送消息
                    if not state:
                        # 下载失败
                        self.common.post_message(title=f"{torrent.title} 添加下载失败！",
                                                 text=f"错误信息：{msg}",
                                                 userid=userid)
                        return
                    # 下载成功，发送通知
                    self.common.post_download_message(meta=meta, mediainfo=self._current_media, torrent=torrent)

        elif text.lower() == "p":
            # 上一页
            cache_data: dict = self._user_cache.get(userid)
            if not cache_data:
                # 没有缓存
                self.common.post_message(title="输入有误！", userid=userid)
                return

            if self._current_page == 0:
                # 第一页
                self.common.post_message(title="已经是第一页了！", userid=userid)
                return
            cache_type: str = cache_data.get('type')
            cache_list: list = cache_data.get('items')
            # 减一页
            self._current_page -= 1
            if self._current_page == 0:
                start = 0
                end = self._page_size
            else:
                start = self._current_page * self._page_size
                end = start + self._page_size
            if cache_type == "Torrent":
                # 发送种子数据
                self.__post_torrents_message(items=cache_list[start:end], userid=userid)
            else:
                # 发送媒体数据
                self.__post_medias_message(items=cache_list[start:end], userid=userid)

        elif text.lower() == "n":
            # 下一页
            cache_data: dict = self._user_cache.get(userid)
            if not cache_data:
                # 没有缓存
                self.common.post_message(title="输入有误！", userid=userid)
                return
            cache_type: str = cache_data.get('type')
            cache_list: list = cache_data.get('items')
            # 加一页
            self._current_page += 1
            cache_list = cache_list[self._current_page * self._page_size:]
            if not cache_list:
                # 没有数据
                self.common.post_message(title="已经是最后一页了！", userid=userid)
                return
            else:
                if cache_type == "Torrent":
                    # 发送种子数据
                    self.__post_torrents_message(items=cache_list, userid=userid)
                else:
                    # 发送媒体数据
                    self.__post_medias_message(items=cache_list, userid=userid)

        else:
            # 搜索或订阅
            if text.startswith("订阅"):
                # 订阅
                content = re.sub(r"订阅[:：\s]*", "", text)
                action = "Subscribe"
            else:
                # 搜索
                content = re.sub(r"(搜索|下载)[:：\s]*", "", text)
                action = "Search"
            # 提取要素
            mtype, key_word, season_num, episode_num, year, title = StringUtils.get_keyword(content)
            # 识别
            meta = MetaInfo(title)
            if not meta.get_name():
                self.common.post_message(title="无法识别输入内容！", userid=userid)
                return
            # 合并信息
            if mtype:
                meta.type = mtype
            if season_num:
                meta.begin_season = season_num
            if episode_num:
                meta.begin_episode = episode_num
            if year:
                meta.year = year
            self._current_meta = meta
            # 开始搜索
            medias: Optional[List[MediaInfo]] = self.run_module('search_medias', meta=meta)
            if not medias:
                self.common.post_message(title=f"{meta.get_name()} 没有找到对应的媒体信息！", userid=userid)
                return
            self._user_cache[userid] = {
                'type': action,
                'items': medias
            }
            self._current_page = 0
            self._current_media = None
            # 发送媒体列表
            self.__post_medias_message(items=medias[:self._page_size], userid=userid)

    def __post_medias_message(self, items: list, userid: str):
        """
        发送媒体列表消息
        """
        self.run_module('post_medias_message',
                        title="请回复数字选择对应媒体（p：上一页, n：下一页）",
                        items=items,
                        userid=userid)

    def __post_torrents_message(self, items: list, userid: str):
        """
        发送种子列表消息
        """
        self.run_module('post_torrents_message',
                        title="请回复数字下载对应资源（0：自动选择, p：上一页, n：下一页）",
                        items=items,
                        userid=userid)
