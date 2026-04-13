import asyncio
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional, Dict, Union, List
from urllib.parse import unquote

import base64

from app.agent import agent_manager
from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings, global_vars
from app.core.context import MediaInfo, Context
from app.core.meta import MetaBase
from app.db.user_oper import UserOper
from app.helper.torrent import TorrentHelper
from app.helper.voice import VoiceHelper
from app.log import logger
from app.schemas import Notification, NotExistMediaInfo, CommingMessage
from app.schemas.message import ChannelCapabilityManager
from app.schemas.types import EventType, MessageChannel, MediaType
from app.utils.string import StringUtils
from app.utils.http import RequestUtils

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
    # 用户会话信息 {userid: (session_id, last_time)}
    _user_sessions: Dict[Union[str, int], tuple] = {}
    # 会话超时时间（分钟）
    _session_timeout_minutes: int = 24 * 60

    @staticmethod
    def __get_noexits_info(
        _meta: MetaBase, _mediainfo: MediaInfo
    ) -> Dict[Union[int, str], Dict[int, NotExistMediaInfo]]:
        """
        获取缺失的媒体信息
        """
        if _mediainfo.type == MediaType.TV:
            if not _mediainfo.seasons:
                # 补充媒体信息
                _mediainfo = MediaChain().recognize_media(
                    mtype=_mediainfo.type,
                    tmdbid=_mediainfo.tmdb_id,
                    doubanid=_mediainfo.douban_id,
                    cache=False,
                )
                if not _mediainfo:
                    logger.warn(
                        f"{_mediainfo.tmdb_id or _mediainfo.douban_id} 媒体信息识别失败！"
                    )
                    return {}
                if not _mediainfo.seasons:
                    logger.warn(
                        f"媒体信息中没有季集信息，"
                        f"标题：{_mediainfo.title}，"
                        f"tmdbid：{_mediainfo.tmdb_id}，doubanid：{_mediainfo.douban_id}"
                    )
                    return {}
            # KEY
            _mediakey = _mediainfo.tmdb_id or _mediainfo.douban_id
            _no_exists = {_mediakey: {}}
            if _meta.begin_season:
                # 指定季
                episodes = _mediainfo.seasons.get(_meta.begin_season)
                if not episodes:
                    return {}
                _no_exists[_mediakey][_meta.begin_season] = NotExistMediaInfo(
                    season=_meta.begin_season,
                    episodes=[],
                    total_episode=len(episodes),
                    start_episode=episodes[0],
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
                        start_episode=eps[0],
                    )
        else:
            _no_exists = {}

        return _no_exists

    def process(self, body: Any, form: Any, args: Any) -> None:
        """
        调用模块识别消息内容
        """
        # 消息来源
        source = args.get("source")
        # 获取消息内容
        info = self.message_parser(source=source, body=body, form=form, args=args)
        if not info:
            logger.info("消息链路未识别到有效消息: source=%s", source)
            return
        # 更新消息来源
        source = info.source
        # 渠道
        channel = info.channel
        # 用户ID
        userid = info.userid
        # 用户名（当渠道未提供公开用户名时，回退为 userid 的字符串，避免后续类型校验异常）
        username = (
            str(info.username) if info.username not in (None, "") else str(userid)
        )
        if userid is None or userid == "":
            logger.debug(f"未识别到用户ID：{body}{form}{args}")
            return

        # 消息内容
        text = str(info.text).strip() if info.text else ""
        images = info.images
        audio_refs = info.audio_refs
        if not text and not images and not audio_refs:
            logger.debug(f"未识别到消息内容：：{body}{form}{args}")
            return

        # 获取原消息ID信息
        original_message_id = info.message_id
        original_chat_id = info.chat_id

        # 处理消息
        self.handle_message(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            text=text,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            images=images,
            audio_refs=audio_refs,
        )

    def handle_message(
        self,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
        text: str,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
        images: Optional[List[str]] = None,
        audio_refs: Optional[List[str]] = None,
    ) -> None:
        """
        识别消息内容，执行操作
        """
        # 申明全局变量
        global _current_page, _current_meta, _current_media

        # 加载缓存
        user_cache: Dict[str, dict] = self.load_cache(self._cache_file) or {}

        try:
            # 识别语音为文本
            reply_with_voice = bool(audio_refs)
            if audio_refs:
                transcript = self._transcribe_audio_refs(audio_refs, channel, source)
                merged_parts = []
                seen_parts = set()
                for item in [text.strip() if text else "", transcript or ""]:
                    normalized = item.strip()
                    if not normalized or normalized in seen_parts:
                        continue
                    seen_parts.add(normalized)
                    merged_parts.append(normalized)
                text = "\n".join(merged_parts).strip()
                if not text:
                    self.post_message(
                        Notification(
                            channel=channel,
                            source=source,
                            userid=userid,
                            username=username,
                            title="语音识别失败，请稍后重试",
                        )
                    )
                    return

            # 保存消息
            if not text.startswith("CALLBACK:"):
                self.messagehelper.put(
                    CommingMessage(
                        userid=userid,
                        username=username,
                        channel=channel,
                        source=source,
                        text=text,
                    ),
                    role="user",
                )
                self.messageoper.add(
                    channel=channel,
                    source=source,
                    userid=username or userid,
                    text=text,
                    action=0,
                )
            # 处理消息
            if text.startswith("CALLBACK:"):
                # 处理按钮回调（适配支持回调的渠），优先级最高
                if ChannelCapabilityManager.supports_callbacks(channel):
                    self._handle_callback(
                        text=text,
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        original_message_id=original_message_id,
                        original_chat_id=original_chat_id,
                    )
                else:
                    logger.warning(
                        f"渠道 {channel.value} 不支持回调，但收到了回调消息：{text}"
                    )
            elif text.startswith("/") and not text.lower().startswith("/ai"):
                # 执行特定命令命令（但不是/ai）
                self.eventmanager.send_event(
                    EventType.CommandExcute,
                    {"cmd": text, "user": userid, "channel": channel, "source": source},
                )
            elif text.lower().startswith("/ai"):
                self._handle_ai_message(
                    text=text,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    images=images,
                    reply_with_voice=reply_with_voice,
                )
            elif settings.AI_AGENT_ENABLE and settings.AI_AGENT_GLOBAL:
                # 普通消息，全局智能体响应
                self._handle_ai_message(
                    text=text,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    images=images,
                    reply_with_voice=reply_with_voice,
                )
            else:
                # 非智能体普通消息响应
                if text.isdigit():
                    # 用户选择了具体的条目
                    # 缓存
                    cache_data: dict = user_cache.get(userid)
                    if not cache_data:
                        # 发送消息
                        self.post_message(
                            Notification(
                                channel=channel,
                                source=source,
                                title="输入有误！",
                                userid=userid,
                            )
                        )
                        return
                    cache_data = cache_data.copy()
                    # 选择项目
                    if not cache_data.get("items") or len(
                        cache_data.get("items")
                    ) < int(text):
                        # 发送消息
                        self.post_message(
                            Notification(
                                channel=channel,
                                source=source,
                                title="输入有误！",
                                userid=userid,
                            )
                        )
                        return
                    try:
                        # 选择的序号
                        _choice = int(text) + _current_page * self._page_size - 1
                        # 缓存类型
                        cache_type: str = cache_data.get("type")
                        # 缓存列表
                        cache_list: list = cache_data.get("items").copy()
                        # 选择
                        try:
                            if cache_type in ["Search", "ReSearch"]:
                                # 当前媒体信息
                                mediainfo: MediaInfo = cache_list[_choice]
                                _current_media = mediainfo
                                # 查询缺失的媒体信息
                                exist_flag, no_exists = (
                                    DownloadChain().get_no_exists_info(
                                        meta=_current_meta, mediainfo=_current_media
                                    )
                                )
                                if exist_flag and cache_type == "Search":
                                    # 媒体库中已存在
                                    self.post_message(
                                        Notification(
                                            channel=channel,
                                            source=source,
                                            title=f"【{_current_media.title_year}"
                                            f"{_current_meta.sea} 媒体库中已存在，如需重新下载请发送：搜索 名称 或 下载 名称】",
                                            userid=userid,
                                        )
                                    )
                                    return
                                elif exist_flag:
                                    # 没有缺失，但要全量重新搜索和下载
                                    no_exists = self.__get_noexits_info(
                                        _current_meta, _current_media
                                    )
                                # 发送缺失的媒体信息
                                messages = []
                                if no_exists and cache_type == "Search":
                                    # 发送缺失消息
                                    mediakey = mediainfo.tmdb_id or mediainfo.douban_id
                                    messages = [
                                        f"第 {sea} 季缺失 {StringUtils.str_series(no_exist.episodes) if no_exist.episodes else no_exist.total_episode} 集"
                                        for sea, no_exist in no_exists.get(
                                            mediakey
                                        ).items()
                                    ]
                                elif no_exists:
                                    # 发送总集数的消息
                                    mediakey = mediainfo.tmdb_id or mediainfo.douban_id
                                    messages = [
                                        f"第 {sea} 季总 {no_exist.total_episode} 集"
                                        for sea, no_exist in no_exists.get(
                                            mediakey
                                        ).items()
                                    ]
                                if messages:
                                    self.post_message(
                                        Notification(
                                            channel=channel,
                                            source=source,
                                            title=f"{mediainfo.title_year}：\n"
                                            + "\n".join(messages),
                                            userid=userid,
                                        )
                                    )
                                # 搜索种子，过滤掉不需要的剧集，以便选择
                                logger.info(f"开始搜索 {mediainfo.title_year} ...")
                                self.post_message(
                                    Notification(
                                        channel=channel,
                                        source=source,
                                        title=f"开始搜索 {mediainfo.type.value} {mediainfo.title_year} ...",
                                        userid=userid,
                                    )
                                )
                                # 开始搜索
                                contexts = SearchChain().process(
                                    mediainfo=mediainfo, no_exists=no_exists
                                )
                                if not contexts:
                                    # 没有数据
                                    self.post_message(
                                        Notification(
                                            channel=channel,
                                            source=source,
                                            title=f"{mediainfo.title}"
                                            f"{_current_meta.sea} 未搜索到需要的资源！",
                                            userid=userid,
                                        )
                                    )
                                    return
                                # 搜索结果排序
                                contexts = TorrentHelper().sort_torrents(contexts)
                                try:
                                    # 判断是否设置自动下载
                                    auto_download_user = settings.AUTO_DOWNLOAD_USER
                                    # 匹配到自动下载用户
                                    if auto_download_user and (
                                        auto_download_user == "all"
                                        or any(
                                            userid == user
                                            for user in auto_download_user.split(",")
                                        )
                                    ):
                                        logger.info(
                                            f"用户 {userid} 在自动下载用户中，开始自动择优下载 ..."
                                        )
                                        # 自动选择下载
                                        self.__auto_download(
                                            channel=channel,
                                            source=source,
                                            cache_list=contexts,
                                            userid=userid,
                                            username=username,
                                            no_exists=no_exists,
                                        )
                                    else:
                                        # 更新缓存
                                        user_cache[userid] = {
                                            "type": "Torrent",
                                            "items": contexts,
                                        }
                                        _current_page = 0
                                        # 保存缓存
                                        self.save_cache(user_cache, self._cache_file)
                                        # 删除原消息
                                        if (
                                            original_message_id
                                            and original_chat_id
                                            and ChannelCapabilityManager.supports_deletion(
                                                channel
                                            )
                                        ):
                                            self.delete_message(
                                                channel=channel,
                                                source=source,
                                                message_id=original_message_id,
                                                chat_id=original_chat_id,
                                            )
                                        # 发送种子数据
                                        logger.info(
                                            f"搜索到 {len(contexts)} 条数据，开始发送选择消息 ..."
                                        )
                                        self.__post_torrents_message(
                                            channel=channel,
                                            source=source,
                                            title=mediainfo.title,
                                            items=contexts[: self._page_size],
                                            userid=userid,
                                            total=len(contexts),
                                        )
                                finally:
                                    contexts.clear()
                                    del contexts
                            elif cache_type in ["Subscribe", "ReSubscribe"]:
                                # 订阅或洗版媒体
                                mediainfo: MediaInfo = cache_list[_choice]
                                # 洗版标识
                                best_version = False
                                # 查询缺失的媒体信息
                                if cache_type == "Subscribe":
                                    exist_flag, _ = DownloadChain().get_no_exists_info(
                                        meta=_current_meta, mediainfo=mediainfo
                                    )
                                    if exist_flag:
                                        self.post_message(
                                            Notification(
                                                channel=channel,
                                                source=source,
                                                title=f"【{mediainfo.title_year}"
                                                f"{_current_meta.sea} 媒体库中已存在，如需洗版请发送：洗版 XXX】",
                                                userid=userid,
                                            )
                                        )
                                        return
                                else:
                                    best_version = True
                                # 转换用户名
                                mp_name = (
                                    UserOper().get_name(
                                        **{f"{channel.name.lower()}_userid": userid}
                                    )
                                    if channel
                                    else None
                                )
                                # 添加订阅，状态为N
                                SubscribeChain().add(
                                    title=mediainfo.title,
                                    year=mediainfo.year,
                                    mtype=mediainfo.type,
                                    tmdbid=mediainfo.tmdb_id,
                                    season=_current_meta.begin_season,
                                    channel=channel,
                                    source=source,
                                    userid=userid,
                                    username=mp_name or username,
                                    best_version=best_version,
                                )
                            elif cache_type == "Torrent":
                                if int(text) == 0:
                                    # 自动选择下载，强制下载模式
                                    self.__auto_download(
                                        channel=channel,
                                        source=source,
                                        cache_list=cache_list,
                                        userid=userid,
                                        username=username,
                                    )
                                else:
                                    # 下载种子
                                    context: Context = cache_list[_choice]
                                    # 下载
                                    DownloadChain().download_single(
                                        context,
                                        channel=channel,
                                        source=source,
                                        userid=userid,
                                        username=username,
                                    )
                        finally:
                            cache_list.clear()
                            del cache_list
                    finally:
                        cache_data.clear()
                        del cache_data
                elif text.lower() == "p":
                    # 上一页
                    cache_data: dict = user_cache.get(userid)
                    if not cache_data:
                        # 没有缓存
                        self.post_message(
                            Notification(
                                channel=channel,
                                source=source,
                                title="输入有误！",
                                userid=userid,
                            )
                        )
                        return
                    cache_data = cache_data.copy()
                    try:
                        if _current_page == 0:
                            # 第一页
                            self.post_message(
                                Notification(
                                    channel=channel,
                                    source=source,
                                    title="已经是第一页了！",
                                    userid=userid,
                                )
                            )
                            return
                        # 减一页
                        _current_page -= 1
                        cache_type: str = cache_data.get("type")
                        # 产生副本，避免修改原值
                        cache_list: list = cache_data.get("items").copy()
                        try:
                            if _current_page == 0:
                                start = 0
                                end = self._page_size
                            else:
                                start = _current_page * self._page_size
                                end = start + self._page_size
                            if cache_type == "Torrent":
                                # 发送种子数据
                                self.__post_torrents_message(
                                    channel=channel,
                                    source=source,
                                    title=_current_media.title,
                                    items=cache_list[start:end],
                                    userid=userid,
                                    total=len(cache_list),
                                    original_message_id=original_message_id,
                                    original_chat_id=original_chat_id,
                                )
                            else:
                                # 发送媒体数据
                                self.__post_medias_message(
                                    channel=channel,
                                    source=source,
                                    title=_current_meta.name,
                                    items=cache_list[start:end],
                                    userid=userid,
                                    total=len(cache_list),
                                    original_message_id=original_message_id,
                                    original_chat_id=original_chat_id,
                                )
                        finally:
                            cache_list.clear()
                            del cache_list
                    finally:
                        cache_data.clear()
                        del cache_data
                elif text.lower() == "n":
                    # 下一页
                    cache_data: dict = user_cache.get(userid)
                    if not cache_data:
                        # 没有缓存
                        self.post_message(
                            Notification(
                                channel=channel,
                                source=source,
                                title="输入有误！",
                                userid=userid,
                            )
                        )
                        return
                    cache_data = cache_data.copy()
                    try:
                        cache_type: str = cache_data.get("type")
                        # 产生副本，避免修改原值
                        cache_list: list = cache_data.get("items").copy()
                        total = len(cache_list)
                        # 加一页
                        cache_list = cache_list[
                            (_current_page + 1) * self._page_size : (_current_page + 2)
                            * self._page_size
                        ]
                        if not cache_list:
                            # 没有数据
                            self.post_message(
                                Notification(
                                    channel=channel,
                                    source=source,
                                    title="已经是最后一页了！",
                                    userid=userid,
                                )
                            )
                            return
                        else:
                            try:
                                # 加一页
                                _current_page += 1
                                if cache_type == "Torrent":
                                    # 发送种子数据
                                    self.__post_torrents_message(
                                        channel=channel,
                                        source=source,
                                        title=_current_media.title,
                                        items=cache_list,
                                        userid=userid,
                                        total=total,
                                        original_message_id=original_message_id,
                                        original_chat_id=original_chat_id,
                                    )
                                else:
                                    # 发送媒体数据
                                    self.__post_medias_message(
                                        channel=channel,
                                        source=source,
                                        title=_current_meta.name,
                                        items=cache_list,
                                        userid=userid,
                                        total=total,
                                        original_message_id=original_message_id,
                                        original_chat_id=original_chat_id,
                                    )
                            finally:
                                cache_list.clear()
                                del cache_list
                    finally:
                        cache_data.clear()
                        del cache_data
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
                    elif StringUtils.is_link(text):
                        # 链接
                        content = text
                        action = "Link"
                    elif not StringUtils.is_media_title_like(text):
                        # 聊天
                        content = text
                        action = "Chat"
                    else:
                        # 搜索
                        content = text
                        action = "Search"

                    if action in ["Search", "ReSearch", "Subscribe", "ReSubscribe"]:
                        # 搜索
                        meta, medias = MediaChain().search(content)
                        # 识别
                        if not meta.name:
                            self.post_message(
                                Notification(
                                    channel=channel,
                                    source=source,
                                    title="无法识别输入内容！",
                                    userid=userid,
                                )
                            )
                            return
                        # 开始搜索
                        if not medias:
                            self.post_message(
                                Notification(
                                    channel=channel,
                                    source=source,
                                    title=f"{meta.name} 没有找到对应的媒体信息！",
                                    userid=userid,
                                )
                            )
                            return
                        logger.info(f"搜索到 {len(medias)} 条相关媒体信息")
                        try:
                            # 记录当前状态
                            _current_meta = meta
                            # 保存缓存
                            user_cache[userid] = {"type": action, "items": medias}
                            self.save_cache(user_cache, self._cache_file)
                            _current_page = 0
                            _current_media = None
                            # 发送媒体列表
                            self.__post_medias_message(
                                channel=channel,
                                source=source,
                                title=meta.name,
                                items=medias[: self._page_size],
                                userid=userid,
                                total=len(medias),
                            )
                        finally:
                            medias.clear()
                            del medias
                    else:
                        # 广播事件
                        self.eventmanager.send_event(
                            EventType.UserMessage,
                            {
                                "text": content,
                                "userid": userid,
                                "channel": channel,
                                "source": source,
                            },
                        )
        finally:
            user_cache.clear()
            del user_cache

    def _handle_callback(
        self,
        text: str,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ) -> None:
        """
        处理按钮回调
        """

        global _current_media

        # 提取回调数据
        callback_data = text[9:]  # 去掉 "CALLBACK:" 前缀
        logger.info(f"处理按钮回调：{callback_data}")

        # 插件消息的事件回调 [PLUGIN]插件ID|内容
        if callback_data.startswith("[PLUGIN]"):
            # 提取插件ID和内容
            plugin_id, content = callback_data.split("|", 1)
            # 广播给插件处理
            self.eventmanager.send_event(
                EventType.MessageAction,
                {
                    "plugin_id": plugin_id.replace("[PLUGIN]", ""),
                    "text": content,
                    "userid": userid,
                    "channel": channel,
                    "source": source,
                    "original_message_id": original_message_id,
                    "original_chat_id": original_chat_id,
                },
            )
            return

        # 解析系统回调数据
        try:
            page_text = callback_data.split("_", 1)[1]
            self.handle_message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                text=page_text,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
        except IndexError:
            logger.error(f"回调数据格式错误：{callback_data}")
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="回调数据格式错误，请检查！",
                )
            )

    def __auto_download(
        self,
        channel: MessageChannel,
        source: str,
        cache_list: list[Context],
        userid: Union[str, int],
        username: str,
        no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
    ):
        """
        自动择优下载
        """
        downloadchain = DownloadChain()
        if no_exists is None:
            # 查询缺失的媒体信息
            exist_flag, no_exists = downloadchain.get_no_exists_info(
                meta=_current_meta, mediainfo=_current_media
            )
            if exist_flag:
                # 媒体库中已存在，查询全量
                no_exists = self.__get_noexits_info(_current_meta, _current_media)

        # 批量下载
        downloads, lefts = downloadchain.batch_download(
            contexts=cache_list,
            no_exists=no_exists,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )
        if downloads and not lefts:
            # 全部下载完成
            logger.info(f"{_current_media.title_year} 下载完成")
        else:
            # 未完成下载
            logger.info(f"{_current_media.title_year} 未下载未完整，添加订阅 ...")
            if downloads and _current_media.type == MediaType.TV:
                # 获取已下载剧集
                downloaded = [
                    download.meta_info.begin_episode
                    for download in downloads
                    if download.meta_info.begin_episode
                ]
                note = downloaded
            else:
                note = None
            # 转换用户名
            mp_name = (
                UserOper().get_name(**{f"{channel.name.lower()}_userid": userid})
                if channel
                else None
            )
            # 添加订阅，状态为R
            SubscribeChain().add(
                title=_current_media.title,
                year=_current_media.year,
                mtype=_current_media.type,
                tmdbid=_current_media.tmdb_id,
                season=_current_meta.begin_season,
                channel=channel,
                source=source,
                userid=userid,
                username=mp_name or username,
                state="R",
                note=note,
            )

    def __post_medias_message(
        self,
        channel: MessageChannel,
        source: str,
        title: str,
        items: list,
        userid: str,
        total: int,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ):
        """
        发送媒体列表消息
        """
        # 检查渠道是否支持按钮
        supports_buttons = ChannelCapabilityManager.supports_buttons(channel)

        if supports_buttons:
            # 支持按钮的渠道
            if total > self._page_size:
                title = f"【{title}】共找到{total}条相关信息，请选择操作"
            else:
                title = f"【{title}】共找到{total}条相关信息，请选择操作"

            buttons = self._create_media_buttons(
                channel=channel, items=items, total=total
            )
        else:
            # 不支持按钮的渠道，使用文本提示
            if total > self._page_size:
                title = f"【{title}】共找到{total}条相关信息，请回复对应数字选择（p: 上一页 n: 下一页）"
            else:
                title = f"【{title}】共找到{total}条相关信息，请回复对应数字选择"
            buttons = None

        notification = Notification(
            channel=channel,
            source=source,
            title=title,
            userid=userid,
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

        self.post_medias_message(notification, medias=items)

    def _create_media_buttons(
        self, channel: MessageChannel, items: list, total: int
    ) -> List[List[Dict]]:
        """
        创建媒体选择按钮
        """
        global _current_page

        buttons = []
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        # 为每个媒体项创建选择按钮
        current_row = []
        for i in range(len(items)):
            media = items[i]

            if max_per_row == 1:
                # 每行一个按钮，使用完整文本
                button_text = f"{i + 1}. {media.title_year}"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."

                buttons.append(
                    [{"text": button_text, "callback_data": f"select_{i + 1}"}]
                )
            else:
                # 多按钮一行的情况，使用简化文本
                button_text = f"{i + 1}"

                current_row.append(
                    {"text": button_text, "callback_data": f"select_{i + 1}"}
                )

                # 如果当前行已满或者是最后一个按钮，添加到按钮列表
                if len(current_row) == max_per_row or i == len(items) - 1:
                    buttons.append(current_row)
                    current_row = []

        # 添加翻页按钮
        if total > self._page_size:
            page_buttons = []
            if _current_page > 0:
                page_buttons.append({"text": "⬅️ 上一页", "callback_data": "page_p"})
            if (_current_page + 1) * self._page_size < total:
                page_buttons.append({"text": "下一页 ➡️", "callback_data": "page_n"})
            if page_buttons:
                buttons.append(page_buttons)

        return buttons

    def __post_torrents_message(
        self,
        channel: MessageChannel,
        source: str,
        title: str,
        items: list,
        userid: str,
        total: int,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ):
        """
        发送种子列表消息
        """
        # 检查渠道是否支持按钮
        supports_buttons = ChannelCapabilityManager.supports_buttons(channel)

        if supports_buttons:
            # 支持按钮的渠道
            if total > self._page_size:
                title = f"【{title}】共找到{total}条相关资源，请选择下载"
            else:
                title = f"【{title}】共找到{total}条相关资源，请选择下载"

            buttons = self._create_torrent_buttons(
                channel=channel, items=items, total=total
            )
        else:
            # 不支持按钮的渠道，使用文本提示
            if total > self._page_size:
                title = f"【{title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择 p: 上一页 n: 下一页）"
            else:
                title = f"【{title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择）"
            buttons = None

        notification = Notification(
            channel=channel,
            source=source,
            title=title,
            userid=userid,
            link=settings.MP_DOMAIN("#/resource"),
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

        self.post_torrents_message(notification, torrents=items)

    def _create_torrent_buttons(
        self, channel: MessageChannel, items: list, total: int
    ) -> List[List[Dict]]:
        """
        创建种子下载按钮
        """

        global _current_page

        buttons = []
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        # 自动选择按钮
        buttons.append([{"text": "🤖 自动选择下载", "callback_data": "download_0"}])

        # 为每个种子项创建下载按钮
        current_row = []
        for i in range(len(items)):
            context = items[i]
            torrent = context.torrent_info

            if max_per_row == 1:
                # 每行一个按钮，使用完整文本
                button_text = f"{i + 1}. {torrent.site_name} - {torrent.seeders}↑"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."

                buttons.append(
                    [{"text": button_text, "callback_data": f"download_{i + 1}"}]
                )
            else:
                # 多按钮一行的情况，使用简化文本
                button_text = f"{i + 1}"

                current_row.append(
                    {"text": button_text, "callback_data": f"download_{i + 1}"}
                )

                # 如果当前行已满或者是最后一个按钮，添加到按钮列表
                if len(current_row) == max_per_row or i == len(items) - 1:
                    buttons.append(current_row)
                    current_row = []

        # 添加翻页按钮
        if total > self._page_size:
            page_buttons = []
            if _current_page > 0:
                page_buttons.append({"text": "⬅️ 上一页", "callback_data": "page_p"})
            if (_current_page + 1) * self._page_size < total:
                page_buttons.append({"text": "下一页 ➡️", "callback_data": "page_n"})
            if page_buttons:
                buttons.append(page_buttons)

        return buttons

    def _get_or_create_session_id(self, userid: Union[str, int]) -> str:
        """
        获取或创建会话ID
        如果用户上次会话在15分钟内，则复用相同的会话ID；否则创建新的会话ID
        """
        current_time = datetime.now()

        # 检查用户是否有已存在的会话
        if userid in self._user_sessions:
            session_id, last_time = self._user_sessions[userid]

            # 计算时间差
            time_diff = current_time - last_time

            # 如果时间差小于等于xx分钟，复用会话ID
            if time_diff <= timedelta(minutes=self._session_timeout_minutes):
                # 更新最后使用时间
                self._user_sessions[userid] = (session_id, current_time)
                logger.info(
                    f"复用会话ID: {session_id}, 用户: {userid}, 距离上次会话: {time_diff.total_seconds() / 60:.1f}分钟"
                )
                return session_id

        # 创建新的会话ID
        new_session_id = f"user_{userid}_{int(time.time())}"
        self._user_sessions[userid] = (new_session_id, current_time)
        logger.info(f"创建新会话ID: {new_session_id}, 用户: {userid}")
        return new_session_id

    def clear_user_session(self, userid: Union[str, int]) -> bool:
        """
        清除指定用户的会话信息
        返回是否成功清除
        """
        if userid in self._user_sessions:
            session_id, _ = self._user_sessions.pop(userid)
            logger.info(f"已清除用户 {userid} 的会话: {session_id}")
            return True
        return False

    def remote_clear_session(
        self,
        channel: MessageChannel,
        userid: Union[str, int],
        source: Optional[str] = None,
    ):
        """
        清除用户会话（远程命令接口）
        """
        # 获取并清除会话信息
        session_id = None
        if userid in self._user_sessions:
            session_id, _ = self._user_sessions.pop(userid)
            logger.info(f"已清除用户 {userid} 的会话: {session_id}")

        # 如果有会话ID，同时清除智能体的会话记忆
        if session_id:
            try:
                asyncio.run_coroutine_threadsafe(
                    agent_manager.clear_session(
                        session_id=session_id, user_id=str(userid)
                    ),
                    global_vars.loop,
                )
            except Exception as e:
                logger.warning(f"清除智能体会话记忆失败: {e}")

            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    title="智能体会话已清除，下次将创建新的会话",
                    userid=userid,
                )
            )
        else:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    title="您当前没有活跃的智能体会话",
                    userid=userid,
                )
            )

    def remote_stop_agent(
        self,
        channel: MessageChannel,
        userid: Union[str, int],
        source: Optional[str] = None,
    ):
        """
        应急停止当前正在执行的Agent推理（远程命令接口）。
        与 /clear_session 不同，此命令不会清除会话和记忆，
        停止后用户仍可继续对话。
        """
        # 查找用户的会话ID（不弹出，保留会话）
        session_info = self._user_sessions.get(userid)
        if session_info:
            session_id, _ = session_info
            try:
                future = asyncio.run_coroutine_threadsafe(
                    agent_manager.stop_current_task(session_id=session_id),
                    global_vars.loop,
                )
                stopped = future.result(timeout=10)
            except Exception as e:
                logger.warning(f"停止Agent推理失败: {e}")
                stopped = False

            if stopped:
                self.post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        title="智能体推理已应急停止，会话记忆已保留，您可以继续对话",
                        userid=userid,
                    )
                )
            else:
                self.post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        title="当前没有正在执行的智能体任务",
                        userid=userid,
                    )
                )
        else:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    title="您当前没有活跃的智能体会话",
                    userid=userid,
                )
            )

    def _handle_ai_message(
        self,
        text: str,
        channel: MessageChannel,
        source: str,
        userid: Union[str, int],
        username: str,
        images: Optional[List[str]] = None,
        reply_with_voice: bool = False,
    ) -> None:
        """
        处理AI智能体消息
        """
        try:
            # 检查AI智能体是否启用
            if not settings.AI_AGENT_ENABLE:
                self.post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="MoviePilot智能助手未启用，请在系统设置中启用",
                    )
                )
                return

            # 提取用户消息
            if text.lower().startswith("/ai"):
                user_message = text[3:].strip()  # 移除 "/ai" 前缀（大小写不敏感）
            else:
                user_message = text.strip()  # 按原消息处理

            if not user_message and not images:
                self.post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="请输入您的问题或需求",
                    )
                )
                return

            # 生成或复用会话ID
            session_id = self._get_or_create_session_id(userid)

            # 下载图片并转为base64
            original_images = images
            if images:
                images = self._download_images_to_base64(images, channel, source)
                if original_images and not images and not user_message:
                    self.post_message(
                        Notification(
                            channel=channel,
                            source=source,
                            userid=userid,
                            username=username,
                            title="图片读取失败，请稍后重试",
                        )
                    )
                    return

            # 在事件循环中处理
            asyncio.run_coroutine_threadsafe(
                agent_manager.process_message(
                    session_id=session_id,
                    user_id=str(userid),
                    message=user_message,
                    images=images,
                    channel=channel.value if channel else None,
                    source=source,
                    username=username,
                    reply_with_voice=reply_with_voice,
                ),
                global_vars.loop,
            )

        except Exception as e:
            logger.error(f"处理AI智能体消息失败: {e}")
            self.messagehelper.put(
                f"AI智能体处理失败: {str(e)}", role="system", title="MoviePilot助手"
            )

    def _transcribe_audio_refs(
        self, audio_refs: List[str], channel: MessageChannel, source: str
    ) -> Optional[str]:
        """
        下载并识别语音消息，仅处理当前已接入的渠道。
        """
        if not audio_refs:
            return None
        if not VoiceHelper.is_available("stt"):
            logger.warning("语音能力未配置，跳过语音识别")
            return None

        transcripts = []
        for audio_ref in audio_refs:
            try:
                if audio_ref.startswith("tg://voice_file_id/"):
                    file_id = audio_ref.replace("tg://voice_file_id/", "", 1)
                    content = self.run_module(
                        "download_telegram_file_bytes", file_id=file_id, source=source
                    )
                    filename = "input.ogg"
                elif audio_ref.startswith("tg://audio_file_id/"):
                    file_id = audio_ref.replace("tg://audio_file_id/", "", 1)
                    content = self.run_module(
                        "download_telegram_file_bytes", file_id=file_id, source=source
                    )
                    filename = "input.mp3"
                elif audio_ref.startswith("wxwork://voice_media_id/"):
                    content = self.run_module(
                        "download_wechat_media_bytes", media_ref=audio_ref, source=source
                    )
                    filename = "input.amr"
                elif audio_ref.startswith("slack://file/"):
                    content = self.run_module(
                        "download_slack_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(audio_ref, default="input.ogg")
                elif audio_ref.startswith("discord://file/"):
                    content = self.run_module(
                        "download_discord_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(audio_ref, default="input.ogg")
                elif audio_ref.startswith("qq://file/"):
                    content = self.run_module(
                        "download_qq_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(audio_ref, default="input.ogg")
                elif audio_ref.startswith("vocechat://file/"):
                    content = self.run_module(
                        "download_vocechat_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(audio_ref, default="input.ogg")
                elif audio_ref.startswith("synology://file/"):
                    content = self.run_module(
                        "download_synologychat_file_bytes",
                        file_ref=audio_ref,
                        source=source,
                    )
                    filename = self._guess_audio_filename(audio_ref, default="input.ogg")
                elif audio_ref.startswith("wxbot://voice"):
                    continue
                elif audio_ref.startswith("http"):
                    resp = RequestUtils(timeout=30).get_res(audio_ref)
                    content = resp.content if resp and resp.content else None
                    filename = self._guess_audio_filename(audio_ref, default="input.ogg")
                else:
                    logger.debug(
                        "暂不支持的语音引用: channel=%s, source=%s, ref=%s",
                        channel.value if channel else None,
                        source,
                        audio_ref,
                    )
                    continue

                if not content:
                    logger.warning(
                        "语音下载失败，跳过识别: channel=%s, source=%s, ref=%s",
                        channel.value if channel else None,
                        source,
                        audio_ref,
                    )
                    continue

                transcript = VoiceHelper.transcribe_bytes(content=content, filename=filename)
                if transcript:
                    transcripts.append(transcript)
                    logger.info(
                        "语音识别成功: channel=%s, source=%s, ref=%s, text_len=%s",
                        channel.value if channel else None,
                        source,
                        audio_ref,
                        len(transcript),
                    )
            except Exception as err:
                logger.error(f"语音识别失败: {err}")

        return "\n".join(transcripts).strip() if transcripts else None

    @staticmethod
    def _guess_audio_filename(audio_ref: str, default: str = "input.ogg") -> str:
        """
        根据引用中的扩展名推测音频文件名，便于 STT 服务识别格式。
        """
        if not audio_ref:
            return default
        raw_ref = unquote(audio_ref).split("?", 1)[0].split("#", 1)[0]
        match = re.search(
            r"([^/]+\.(mp3|m4a|wav|ogg|oga|opus|aac|amr|flac|mpga|mpeg|webm))$",
            raw_ref,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
        return default

    def _download_images_to_base64(
        self, images: List[str], channel: MessageChannel, source: str
    ) -> List[str]:
        """
        下载图片并转为base64
        """
        if not images:
            return None
        base64_images = []
        for img in images:
            try:
                if img.startswith("data:"):
                    base64_images.append(img)
                elif img.startswith("tg://file_id/"):
                    file_id = img.replace("tg://file_id/", "")
                    base64_data = self.run_module(
                        "download_telegram_file_to_base64", file_id=file_id, source=source
                    )
                    if base64_data:
                        base64_images.append(f"data:image/jpeg;base64,{base64_data}")
                        logger.info(
                            "图片下载成功: channel=%s, source=%s, input=%s, output=data:image/jpeg;base64...(omitted)",
                            channel.value if channel else None,
                            source,
                            img,
                        )
                elif img.startswith("wxwork://media_id/") or img.startswith(
                    "wxbot://image/"
                ):
                    data_url = self.run_module(
                        "download_wechat_image_to_data_url",
                        image_ref=img,
                        source=source,
                    )
                    if data_url:
                        base64_images.append(data_url)
                elif channel == MessageChannel.Slack:
                    data_url = self.run_module(
                        "download_slack_file_to_data_url", file_url=img, source=source
                    )
                    if data_url:
                        base64_images.append(data_url)
                elif img.startswith("vocechat://file/"):
                    data_url = self.run_module(
                        "download_vocechat_image_to_data_url",
                        image_ref=img,
                        source=source,
                    )
                    if data_url:
                        base64_images.append(data_url)
                elif img.startswith("http"):
                    resp = RequestUtils(timeout=30).get_res(img)
                    if resp and resp.content:
                        base64_data = base64.b64encode(resp.content).decode()
                        mime_type = resp.headers.get("Content-Type", "image/jpeg")
                        base64_images.append(f"data:{mime_type};base64,{base64_data}")
            except Exception as e:
                logger.error(f"下载图片失败: {img}, error: {e}")
        return base64_images if base64_images else None
