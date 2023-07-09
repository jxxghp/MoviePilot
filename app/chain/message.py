from typing import Any

from app.chain.download import *
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.context import MediaInfo
from app.core.event import EventManager
from app.log import logger
from app.schemas.types import EventType


class MessageChain(ChainBase):
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
    _current_meta: Optional[MetaBase] = None
    # 当前媒体信息
    _current_media: Optional[MediaInfo] = None

    def __init__(self):
        super().__init__()
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.searchchain = SearchChain()
        self.medtachain = MediaChain()
        self.torrent = TorrentHelper()
        self.eventmanager = EventManager()
        self.torrenthelper = TorrentHelper()

    def process(self, body: Any, form: Any, args: Any) -> None:
        """
        识别消息内容，执行操作
        """
        # 获取消息内容
        info: dict = self.message_parser(body=body, form=form, args=args)
        if not info:
            return
        # 用户ID
        userid = info.get('userid')
        username = info.get('username')
        if not userid:
            logger.debug(f'未识别到用户ID：{body}{form}{args}')
            return
        # 消息内容
        text = str(info.get('text')).strip() if info.get('text') else None
        if not text:
            logger.debug(f'未识别到消息内容：：{body}{form}{args}')
            return
        logger.info(f'收到用户消息内容，用户：{userid}，内容：{text}')
        if text.startswith('/'):
            # 执行命令
            self.eventmanager.send_event(
                EventType.CommandExcute,
                {
                    "cmd": text,
                    "user": userid
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
                self.post_message(title="输入有误！", userid=userid)
                return
            # 缓存类型
            cache_type: str = cache_data.get('type')
            # 缓存列表
            cache_list: list = cache_data.get('items')
            # 选择
            if cache_type == "Search":
                mediainfo: MediaInfo = cache_list[int(text) + self._current_page * self._page_size - 1]
                self._current_media = mediainfo
                # 查询缺失的媒体信息
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=self._current_meta,
                                                                              mediainfo=self._current_media)
                if exist_flag:
                    self.post_message(title=f"{self._current_media.title_year}"
                                            f"{self._current_meta.sea} 媒体库中已存在",
                                      userid=userid)
                    return
                # 发送缺失的媒体信息
                if no_exists:
                    # 发送消息
                    messages = [
                        f"第 {sea} 季缺失 {StringUtils.str_series(no_exist.episodes) if no_exist.episodes else no_exist.total_episodes} 集"
                        for sea, no_exist in no_exists.get(mediainfo.tmdb_id).items()]
                    self.post_message(title=f"{mediainfo.title_year}：\n" + "\n".join(messages))
                # 搜索种子，过滤掉不需要的剧集，以便选择
                logger.info(f"{mediainfo.title_year} 媒体库中不存在，开始搜索 ...")
                self.post_message(
                    title=f"开始搜索 {mediainfo.type.value} {mediainfo.title_year} ...", userid=userid)
                # 开始搜索
                contexts = self.searchchain.process(mediainfo=mediainfo,
                                                    no_exists=no_exists)
                if not contexts:
                    # 没有数据
                    self.post_message(title=f"{mediainfo.title}"
                                            f"{self._current_meta.sea} 未搜索到需要的资源！",
                                      userid=userid)
                    return
                # 搜索结果排序
                contexts = self.torrenthelper.sort_torrents(contexts)
                # 更新缓存
                self._user_cache[userid] = {
                    "type": "Torrent",
                    "items": contexts
                }
                self._current_page = 0
                # 发送种子数据
                logger.info(f"搜索到 {len(contexts)} 条数据，开始发送选择消息 ...")
                self.__post_torrents_message(title=mediainfo.title,
                                             items=contexts[:self._page_size],
                                             mediainfo=mediainfo,
                                             userid=userid,
                                             total=len(contexts))

            elif cache_type == "Subscribe":
                # 订阅媒体
                mediainfo: MediaInfo = cache_list[int(text) - 1]
                # 查询缺失的媒体信息
                exist_flag, _ = self.downloadchain.get_no_exists_info(meta=self._current_meta,
                                                                      mediainfo=mediainfo)
                if exist_flag:
                    self.post_message(title=f"{mediainfo.title_year}"
                                            f"{self._current_meta.sea} 媒体库中已存在",
                                      userid=userid)
                    return
                self.subscribechain.add(title=mediainfo.title,
                                        year=mediainfo.year,
                                        mtype=mediainfo.type,
                                        tmdbid=mediainfo.tmdb_id,
                                        season=self._current_meta.begin_season,
                                        userid=userid,
                                        username=username)
            elif cache_type == "Torrent":
                if int(text) == 0:
                    # 自动选择下载
                    # 查询缺失的媒体信息
                    exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=self._current_meta,
                                                                                  mediainfo=self._current_media)
                    if exist_flag:
                        self.post_message(title=f"{self._current_media.title_year}"
                                                f"{self._current_meta.sea} 媒体库中已存在",
                                          userid=userid)
                        return
                    # 批量下载
                    downloads, lefts = self.downloadchain.batch_download(contexts=cache_list,
                                                                         no_exists=no_exists,
                                                                         userid=userid)
                    if downloads and not lefts:
                        # 全部下载完成
                        logger.info(f'{self._current_media.title_year} 下载完成')
                    else:
                        # 未完成下载
                        logger.info(f'{self._current_media.title_year} 未下载未完整，添加订阅 ...')
                        # 添加订阅
                        self.subscribechain.add(title=self._current_media.title,
                                                year=self._current_media.year,
                                                mtype=self._current_media.type,
                                                tmdbid=self._current_media.tmdb_id,
                                                season=self._current_meta.begin_season,
                                                userid=userid,
                                                username=username)
                else:
                    # 下载种子
                    context: Context = cache_list[int(text) - 1]
                    # 下载
                    self.downloadchain.download_single(context, userid=userid)

        elif text.lower() == "p":
            # 上一页
            cache_data: dict = self._user_cache.get(userid)
            if not cache_data:
                # 没有缓存
                self.post_message(title="输入有误！", userid=userid)
                return

            if self._current_page == 0:
                # 第一页
                self.post_message(title="已经是第一页了！", userid=userid)
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
                self.__post_torrents_message(title=self._current_media.title,
                                             items=cache_list[start:end],
                                             mediainfo=self._current_media,
                                             userid=userid,
                                             total=len(cache_list))
            else:
                # 发送媒体数据
                self.__post_medias_message(title=self._current_media.title,
                                           items=cache_list[start:end],
                                           userid=userid,
                                           total=len(cache_list))

        elif text.lower() == "n":
            # 下一页
            cache_data: dict = self._user_cache.get(userid)
            if not cache_data:
                # 没有缓存
                self.post_message(title="输入有误！", userid=userid)
                return
            cache_type: str = cache_data.get('type')
            cache_list: list = cache_data.get('items')
            total = len(cache_list)
            # 加一页
            self._current_page += 1
            cache_list = cache_list[self._current_page * self._page_size:(self._current_page + 1) * self._page_size]
            if not cache_list:
                # 没有数据
                self.post_message(title="已经是最后一页了！", userid=userid)
                return
            else:
                if cache_type == "Torrent":
                    # 发送种子数据
                    self.__post_torrents_message(title=self._current_media.title,
                                                 mediainfo=self._current_media,
                                                 items=cache_list, userid=userid, total=total)
                else:
                    # 发送媒体数据
                    self.__post_medias_message(title=self._current_media.title,
                                               items=cache_list, userid=userid, total=total)

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
            # 搜索
            meta, medias = self.medtachain.search(content)
            # 识别
            if not meta.name:
                self.post_message(title="无法识别输入内容！", userid=userid)
                return
            # 开始搜索
            if not medias:
                self.post_message(title=f"{meta.name} 没有找到对应的媒体信息！", userid=userid)
                return
            logger.info(f"搜索到 {len(medias)} 条相关媒体信息")
            # 记录当前状态
            self._current_meta = meta
            self._user_cache[userid] = {
                'type': action,
                'items': medias
            }
            self._current_page = 0
            self._current_media = None
            # 发送媒体列表
            self.__post_medias_message(title=meta.name,
                                       items=medias[:self._page_size],
                                       userid=userid, total=len(medias))

    def __post_medias_message(self, title: str, items: list, userid: str, total: int):
        """
        发送媒体列表消息
        """
        self.post_medias_message(
            title=f"【{title}】共找到{total}条相关信息，请回复对应数字选择（p: 上一页 n: 下一页）",
            items=items,
            userid=userid
        )

    def __post_torrents_message(self, title: str, items: list,
                                mediainfo: MediaInfo, userid: str, total: int):
        """
        发送种子列表消息
        """
        self.post_torrents_message(
            title=f"【{title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择 p: 上一页 n: 下一页）",
            items=items,
            mediainfo=mediainfo,
            userid=userid
        )
