"""
消息渠道的媒体搜索、资源选择、下载和订阅交互编排。
"""

import math
import re
import uuid
from collections.abc import Callable, Mapping
from copy import copy, deepcopy
from typing import Any, Dict, List, Optional, Tuple, TypeVar, Union

from app.application.directory import DirectoryHelper
from app.application.download.admission import DownloadAuthorizationError, scoped_download_authorization
from app.application.history import scoped_download_owner
from app.application.messaging.media import (
    PendingMediaInteraction,
    media_interaction_manager,
)
from app.application.messaging.message import scoped_message_reply
from app.application.security.authorization import channel_search_auto_download, get_channel_authorization
from app.application.torrent.download import TorrentHelper
from app.chain.base import ChainBase
from app.chain.download import DownloadChain
from app.chain.download.submission import (
    available_tmdb_seasons,
    convert_download_identity_to_tmdb,
    explicit_meta_seasons,
    is_tmdb_recognize_source,
)
from app.chain.media import MediaChain
from app.chain.music_interaction.music import (
    format_music_candidate,
    media_exists_retry_prompt,
    post_music_aware_torrents,
    post_music_candidates,
    resolve_media_action,
    search_music_candidates,
    selected_music_type,
)
from app.chain.music_interaction.subscribe import submit_media_subscription, subscribe_media
from app.chain.search.facade import SearchChain
from app.domain import episode as episode_rules
from app.domain.context import Context, MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.download import DownloadDirectory
from app.schemas.file import FileURI
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.message import Message
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaSource, MediaType, NotificationChannel

_DownloadResult = TypeVar("_DownloadResult")


class MediaInteractionChain(ChainBase):
    """
    处理影视与音乐搜索、订阅、资源选择和翻页等消息交互流程。
    """

    _button_page_size = 8
    _text_page_size = 8
    _auto_download_dir_name = "自动匹配目录"
    _subscribe_actions = frozenset({"Subscribe", "ReSubscribe", "MusicSubscribe", "MusicReSubscribe"})
    _scope_labels = {"search": "搜索", "subscribe": "订阅", "download": "下载"}

    @classmethod
    def _action_required_scope(cls, action: str) -> str:
        """
        返回媒体交互动作所需的渠道能力键。

        :param action: 媒体交互动作名称
        :return: 订阅类动作返回 subscribe，其余搜索类动作返回 search
        """
        return "subscribe" if action in cls._subscribe_actions else "search"

    @staticmethod
    def _has_scope(allowed_scopes: Optional[Mapping[str, Any]], scope: str) -> bool:
        """
        判断当前上下文是否具备指定能力；未传权限映射时按管理员/非受限模式放行。

        :param allowed_scopes: 受控普通成员的实时权限映射，None 表示不限制
        :param scope: 待检查的能力键
        :return: 允许执行时返回 True
        """
        return allowed_scopes is None or allowed_scopes.get(scope) is True

    def _require_scope(
            self,
            allowed_scopes: Optional[Mapping[str, Any]],
            scope: str,
            *,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> bool:
        """
        在受控成员执行传统媒体交互前校验能力授权，未授权时直接回复提示。

        :param allowed_scopes: 受控普通成员的实时权限映射，None 表示不限制
        :param scope: 本步骤所需能力键（search、subscribe 或 download）
        :param channel: 发起交互的通知渠道
        :param source: 渠道实例标识
        :param userid: 渠道平台用户标识
        :param username: 渠道展示名称
        :return: 已获授权时返回 True；拒绝时回复提示并返回 False
        """
        if self._has_scope(allowed_scopes, scope):
            return True
        label = self._scope_labels.get(scope, scope)
        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"当前渠道成员未获授权使用{label}能力，请联系管理员调整权限",
                save_history=False,
            )
        )
        return False

    @staticmethod
    def has_pending_interaction(user_id: Union[str, int]) -> bool:
        """
        判断用户当前是否存在未结束的媒体交互。
        """
        return media_interaction_manager.get_by_user(user_id) is not None

    @staticmethod
    def _get_noexits_info(
            meta: MetaBase, mediainfo: MediaInfo | MusicInfo
    ) -> Dict[Union[int, str], Dict[int, NotExistMediaInfo]]:
        """
        构造媒体缺失集信息，用于全量重搜或自动下载补全集数。
        """
        if mediainfo.type == MediaType.TV:
            if not mediainfo.seasons:
                recognized_media = MediaChain().recognize_media(
                    mtype=mediainfo.type,
                    media_source=resolve_media_identity(media=mediainfo)[0],
                    media_id=resolve_media_identity(media=mediainfo)[1],
                    cache=False,
                )
                if not recognized_media:
                    logger.warn("媒体信息识别失败，无法补充季集信息")
                    return {}
                if not recognized_media.seasons:
                    logger.warn(
                        "媒体信息中没有季集信息，标题：%s，tmdbid：%s，doubanid：%s",
                        recognized_media.title,
                        recognized_media.tmdb_id,
                        recognized_media.douban_id,
                    )
                    return {}
                mediainfo = recognized_media

            media_source, media_id = resolve_media_identity(media=mediainfo)
            mediakey = build_media_key(media_source, media_id)
            no_exists: Dict[Union[int, str], Dict[int, NotExistMediaInfo]] = {
                mediakey: {}
            }
            if meta.begin_season is not None:
                episodes = mediainfo.seasons.get(meta.begin_season)
                if not episodes:
                    return {}
                no_exists[mediakey][meta.begin_season] = NotExistMediaInfo(
                    season=meta.begin_season,
                    episodes=[],
                    total_episode=len(episodes),
                    start_episode=episodes[0],
                )
            else:
                for sea, eps in mediainfo.seasons.items():
                    if not eps:
                        continue
                    no_exists[mediakey][sea] = NotExistMediaInfo(
                        season=sea,
                        episodes=[],
                        total_episode=len(eps),
                        start_episode=eps[0],
                    )
            return no_exists
        return {}

    @staticmethod
    def parse_callback(
            callback_data: str,
    ) -> Optional[Tuple[Optional[str], str, Optional[int]]]:
        """
        解析新旧两种媒体交互按钮格式。
        """
        if callback_data.startswith("media:"):
            parts = callback_data.split(":")
            if len(parts) < 3:
                return None
            request_id = parts[1]
            action = parts[2]
            index = None
            if len(parts) >= 4 and parts[3].isdigit():
                index = int(parts[3])
            return request_id, action, index

        match = re.match(r"^(select|download)_(\d+)$", callback_data)
        if match:
            return None, match.group(1), int(match.group(2))
        if callback_data == "page_p":
            return None, "page-prev", None
        if callback_data == "page_n":
            return None, "page-next", None
        return None

    def _operation_username(
        self, operation: str, channel: NotificationChannel, source: str,
        userid: Union[str, int], username: str, *, explain: bool = True,
    ) -> Optional[str]:
        """
        在业务调用前复核当前权限，使用服务端账号名记录业务归属。

        :param operation: 与现有 API 共用的业务操作标识
        :param channel: 平台来源
        :param source: 消息所属实例名称
        :param userid: 仅用于平台回复和授权查找的发言人 ID
        :param username: 旧网页及兼容入口已校验的显示名称
        :param explain: 拒绝时是否发送原因
        :return: 可执行操作的账号名称；拒绝时为空
        """
        actor = self._operation_actor(operation, channel, source, userid, username, explain=explain)
        return actor[0] if actor else None

    def _operation_actor(
        self, operation: str, channel: NotificationChannel, source: str,
        userid: Union[str, int], username: str, *, explain: bool = True,
    ) -> tuple[str, str | None] | None:
        """
        取得当前已授权的用户名及不可变账号身份。
        """
        service = get_channel_authorization()
        if service is None or channel in {NotificationChannel.Web, NotificationChannel.WebAgent}:
            return username, None
        try:
            actor = service.resolve_operation(channel.value, source, str(userid), operation)
            return actor.name, actor.notification_identity
        except PermissionError as error:
            if explain:
                self._post_invalid_input(channel, source, userid, username, title=str(error))
            return None

    def _submit_channel_download(
        self, submit: Callable[[], _DownloadResult], channel: NotificationChannel, source: str,
        userid: Union[str, int], username: str, identity: str | None, chat_id: str | None,
    ) -> _DownloadResult | None:
        """
        在下载链复核原发言人和归属，权限失效时结束当前提交并回复原因。

        :param submit: 当前交互选择的单资源或自动择优操作
        :param channel: 当前交互平台
        :param source: 原连接名称
        :param userid: 实际发言人
        :param username: 初次校验取得的业务账号名
        :param identity: 初次校验取得的不可变业务归属
        :param chat_id: 本次交互的回复地址
        :return: 下载链的结果，权限失效时返回 None
        """
        def authorize() -> None:
            """
            核对提交时的成员状态及归属与本次操作一致。
            """
            service = get_channel_authorization()
            if service is None:
                raise PermissionError("当前渠道授权不可用，请重新发起下载")
            actor = service.resolve_operation(channel.value, source, str(userid), "download.add")
            if actor.notification_identity != identity:
                raise PermissionError("渠道账号归属已改变，请重新发起下载")

        try:
            with scoped_download_owner(identity), scoped_download_authorization(authorize if identity is not None else None):
                return submit()
        except DownloadAuthorizationError as error:
            self._post_invalid_input(channel, source, userid, username, title=str(error), original_chat_id=chat_id)
            return None

    def _claim_media_choice(
            self, request: PendingMediaInteraction, action: str, index: Optional[int], callback_data: str,
    ) -> bool:
        """
        绑定写按钮到同一份候选列表，原子消费已验证的序号。

        :param request: 所属会话
        :param action: 当前回调动作
        :param index: 列表内序号，自动下载为零
        :param callback_data: 原始回调，包括可选列表版本
        :return: 导航或有效且尚未执行的选择返回 True
        """
        phase = {"select": "media", "download": "torrent", "download-dir": "download-dir"}.get(action)
        if phase is None:
            return True
        version = request.callback_versions.get(phase, "")
        suffix = callback_data.split(":")[4:]
        if (version and suffix != [version]) or (not version and suffix):
            return False
        if request.phase != phase or index is None:
            return False
        if index < (0 if action == "download" else 1) or index > self._page_size(request.channel):
            return False
        return media_interaction_manager.claim_callback(request, action, index, version)

    @scoped_message_reply
    def handle_callback_interaction(
            self,
            callback_data: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
    ) -> bool:
        """
        处理按钮回调，并将当前视图刷新到原消息上。

        :param callback_data: 渠道传回的按钮回调载荷
        :param channel: 通知渠道
        :param source: 渠道实例标识
        :param userid: 渠道平台用户标识
        :param username: 渠道展示名称
        :param original_message_id: 需要原地刷新的消息标识，None 表示发送新消息
        :param original_chat_id: 会话或群聊标识，None 表示使用渠道默认定位
        :param allowed_scopes: 受控普通成员的实时权限映射，None 表示不限制
        :param owner_username: 统一渠道授权确定的业务归属用户名，None 时保留既有解析规则
        :return: 已匹配并处理回调时返回 True
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False

        request_id, action, index = parsed
        if request_id:
            request = media_interaction_manager.get_by_id(request_id, userid, channel, source, original_chat_id)
        else:
            request = media_interaction_manager.get_by_user(userid, channel, source, original_chat_id)

        if not request:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="交互已失效，请重新搜索或订阅",
                    save_history=False,
                    original_chat_id=original_chat_id,
                )
            )
            return True

        # 先检查当前权限，再消费按钮。业务操作提交前仍会复核授权。
        operation = self._media_choice_operation(request, action)
        if operation and self._operation_actor(operation, channel, source, userid, username) is None:
            return True
        if not self._claim_media_choice(request, action, index, callback_data):
            return True

        request.username = username
        request.original_chat_id = original_chat_id

        if action == "page-prev":
            if request.page <= 0:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是第一页了！",
                    original_chat_id=original_chat_id,
                )
                return True
            request.page -= 1
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "page-next":
            if not self._has_next_page(request):
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是最后一页了！",
                    original_chat_id=original_chat_id,
                )
                return True
            request.page += 1
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "select":
            self._handle_media_selection(
                request=request,
                page_index=index,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return True

        if action == "download":
            self._handle_torrent_selection(
                request=request,
                page_index=index,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return True

        if action == "download-dir":
            self._handle_download_dir_selection(
                request=request,
                page_index=index,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return True

        return False

    @staticmethod
    def _media_choice_operation(request: PendingMediaInteraction, action: str) -> str | None:
        """
        获取写按钮消费前需要复核的操作权限。

        :param request: 当前媒体交互请求
        :param action: 已解析的按钮动作
        :return: 下载、订阅或搜索的授权操作；翻页没有消费动作
        """
        if action in {"download", "download-dir"}:
            return "download.add"
        if action == "select":
            if request.action in {"Subscribe", "ReSubscribe", "MusicSubscribe", "MusicReSubscribe"}:
                return "subscription.add"
            return "search.torrents"
        return None

    @scoped_message_reply
    def handle_text_interaction(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        处理文本式交互。

        有会话时优先处理数字选择和翻页；无会话时负责识别搜索/订阅类入口。

        :param channel: 通知渠道
        :param source: 渠道实例标识
        :param userid: 渠道平台用户标识
        :param username: 渠道展示名称
        :param text: 用户输入的文本内容
        :param allowed_scopes: 受控普通成员的实时权限映射，None 表示不限制
        :param owner_username: 统一渠道授权确定的业务归属用户名，None 时保留既有解析规则
        :return: 已消费该文本时返回 True
        """
        request = media_interaction_manager.get_by_user(userid, channel, source, original_chat_id)
        if request is not None:
            request.original_chat_id = original_chat_id
        normalized = (text or "").strip()
        lowered = normalized.lower()

        if request and lowered in {"退出", "关闭", "q", "quit", "exit"}:
            media_interaction_manager.remove(request.request_id)
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="媒体交互已结束",
                    save_history=False,
                    original_chat_id=original_chat_id,
                )
            )
            return True

        if normalized.isdigit():
            if not request:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    original_chat_id=original_chat_id,
                )
                return True
            request.channel = channel
            request.source = source
            request.username = username
            index = int(normalized)
            if request.phase == "download-dir":
                self._handle_download_dir_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    allowed_scopes=allowed_scopes,
                    owner_username=owner_username,
                )
            elif request.phase == "torrent":
                self._handle_torrent_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    allowed_scopes=allowed_scopes,
                    owner_username=owner_username,
                )
            else:
                self._handle_media_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    allowed_scopes=allowed_scopes,
                    owner_username=owner_username,
                )
            return True

        if lowered in {"p", "prev", "上一页"}:
            if not request:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    original_chat_id=original_chat_id,
                )
                return True
            if request.page <= 0:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是第一页了！",
                    original_chat_id=original_chat_id,
                )
                return True
            request.page -= 1
            request.channel = channel
            request.source = source
            request.username = username
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
            )
            return True

        if lowered in {"n", "next", "下一页"}:
            if not request:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    original_chat_id=original_chat_id,
                )
                return True
            if not self._has_next_page(request):
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是最后一页了！",
                    original_chat_id=original_chat_id,
                )
                return True
            request.page += 1
            request.channel = channel
            request.source = source
            request.username = username
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
            )
            return True

        action, content = self._resolve_action(normalized)
        if not action:
            return False

        self._start_media_interaction(
            action=action,
            content=content,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            allowed_scopes=allowed_scopes,
            original_chat_id=original_chat_id,
        )
        return True

    @staticmethod
    def _resolve_action(text: str) -> Tuple[Optional[str], str]:
        """
        将消息文本归类为影视或音乐搜索、订阅及普通聊天。
        """
        return resolve_media_action(text)

    def _start_media_interaction(
            self,
            action: str,
            content: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        根据用户输入搜索影视或音乐，并进入对应候选选择阶段。
        """
        if not self._require_scope(
            allowed_scopes,
            self._action_required_scope(action),
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        ):
            return
        owner = self._operation_username("subscription.add" if "Subscribe" in action else "search.torrents", channel, source, userid, username)
        if owner is None:
            return
        username = owner
        meta: Optional[MetaBase]
        medias: List[MediaInfo | MusicInfo] = []
        if action.startswith("Music"):
            music_meta, title, music_candidates = search_music_candidates(content)
            meta = music_meta
            medias.extend(music_candidates)
        else:
            meta, media_candidates = MediaChain().search(content)
            medias.extend(media_candidates)
            title = meta.name if meta else ""
        if not meta or not title:
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="无法识别输入内容！",
                original_chat_id=original_chat_id,
            )
            return
        if not medias:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{title} 没有找到对应的媒体信息！",
                    save_history=False,
                    original_chat_id=original_chat_id,
                )
            )
            return

        logger.info("搜索到 %s 条相关媒体信息", len(medias))
        request = media_interaction_manager.create_or_replace(
            user_id=userid,
            channel=channel,
            source=source,
            username=username,
            action=action,
            keyword=content,
            title=title,
            meta=meta,
            items=medias,
            original_chat_id=original_chat_id,
        )
        self._render_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
        )

    def _handle_media_selection(
            self,
            request: PendingMediaInteraction,
            page_index: Optional[int],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
    ) -> None:
        """
        处理媒体选择阶段的序号输入。
        """
        original_chat_id = original_chat_id or request.original_chat_id
        if request.phase != "media":
            self._post_invalid_input(channel=channel, source=source, userid=userid, username=username,
                                     title="媒体选择已结束，请使用当前资源列表", original_chat_id=original_chat_id)
            return
        page_items, page, _ = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        request.page = page
        if not page_index or page_index < 1 or page_index > len(page_items):
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_chat_id=original_chat_id,
            )
            return

        mediainfo: MediaInfo | MusicInfo = page_items[page_index - 1]
        request.current_media = mediainfo

        if request.action in {"Search", "ReSearch", "MusicSearch", "MusicReSearch"}:
            if not self._require_scope(
                allowed_scopes,
                "search",
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            ):
                return
            self._search_media_resources(
                request=request,
                mediainfo=mediainfo,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return

        if request.action in {
            "Subscribe",
            "ReSubscribe",
            "MusicSubscribe",
            "MusicReSubscribe",
        }:
            if not self._require_scope(
                allowed_scopes,
                "subscribe",
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            ):
                return
            subscribe_media(
                host=self,
                request=request,
                mediainfo=mediainfo,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                owner_username=owner_username,
            )

    def _search_media_resources(
            self,
            request: PendingMediaInteraction,
            mediainfo: MediaInfo | MusicInfo,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
    ) -> None:
        """
        根据已选媒体搜索资源，并切换到资源选择阶段。
        """
        owner = self._operation_username("search.torrents", channel, source, userid, username)
        if owner is None:
            return
        username = owner
        original_chat_id = original_chat_id or request.original_chat_id
        meta = request.meta
        exist_flag, no_exists = DownloadChain().get_no_exists_info(
            meta=meta,
            mediainfo=mediainfo,
        )
        if exist_flag and request.action in {"Search", "MusicSearch"}:
            media_label, retry_hint = media_exists_retry_prompt(
                mediainfo=mediainfo,
                meta=meta,
                keyword=request.keyword,
                subscription=False,
            )
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"【{media_label} 媒体库中已存在，如需重新下载请发送：{retry_hint}】",
                    save_history=False,
                    original_chat_id=original_chat_id,
                )
            )
            return
        if exist_flag and meta:
            no_exists = self._get_noexits_info(meta, mediainfo)

        messages = self._build_no_exists_messages(
            mediainfo=mediainfo,
            no_exists=no_exists,
            show_missing_only=request.action == "Search",
        )
        if messages:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{mediainfo.title_year}：\n" + "\n".join(messages),
                    save_history=False,
                    original_chat_id=original_chat_id,
                )
            )

        logger.info("开始搜索 %s ...", mediainfo.title_year)
        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                    title=f"开始搜索 {(mediainfo.type or MediaType.UNKNOWN).value} {mediainfo.title_year} ...",
                save_history=False,
                original_chat_id=original_chat_id,
            )
        )

        contexts = SearchChain().process(mediainfo=mediainfo, no_exists=no_exists)
        if not contexts:
            season = meta.sea if meta else ""
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{mediainfo.title}{season} 未搜索到需要的资源！",
                    save_history=False,
                    original_chat_id=original_chat_id,
                )
            )
            return

        contexts = TorrentHelper().sort_torrents(contexts)
        if (self._should_auto_download(userid, source, channel)
                and self._has_scope(allowed_scopes, "download")) and self._operation_username(
            "download.add", channel, source, userid, username, explain=False,
        ) is not None:
            logger.info("用户 %s 在自动下载用户中，开始自动择优下载 ...", userid)
            request.phase = "torrent"
            request.page = 0
            request.title = mediainfo.title or ""
            request.items = list(contexts)
            if self._prompt_download_dir_selection(
                    request=request,
                    download_mode="auto",
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    no_exists=no_exists,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
            ):
                return
            self._auto_download(
                request=request,
                cache_list=contexts,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                no_exists=no_exists,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return

        request.phase = "torrent"
        request.page = 0
        request.title = mediainfo.title or ""
        request.items = list(contexts)
        self._render_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _handle_torrent_selection(
            self,
            request: PendingMediaInteraction,
            page_index: Optional[int],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
    ) -> None:
        """
        处理资源选择阶段的下载操作。
        """
        owner = self._operation_actor("download.add", channel, source, userid, username)
        if owner is None:
            return
        username, identity = owner
        if get_channel_authorization() is None:
            username = owner_username or username
        if request.phase != "torrent":
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_chat_id=request.original_chat_id,
            )
            return
        if not self._require_scope(
            allowed_scopes,
            "download",
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        ):
            return

        if page_index == 0:
            if self._prompt_download_dir_selection(
                    request=request,
                    download_mode="auto",
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
            ):
                return
            self._auto_download(
                request=request,
                cache_list=request.items,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return

        page_items, page, _ = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        request.page = page
        if not page_index or page_index < 1 or page_index > len(page_items):
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_chat_id=request.original_chat_id,
            )
            return

        context: Context = self._prepare_manual_download_context(page_items[page_index - 1])
        if self._prompt_download_dir_selection(
                request=request,
                download_mode="single",
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                context=context,
        ):
            return
        self._submit_channel_download(
            lambda: DownloadChain().download_single(
                context,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                user_specified=True,
            ), channel, source, userid, username, identity, request.original_chat_id,
        )

    @staticmethod
    def _prepare_manual_download_context(context: Context) -> Context:
        """
        在全局识别源为 TMDB 时，把交互选中的非 TMDB 影视下载上下文转换为 TMDB 主身份。

        未触发转换或转换不可靠时直接返回原上下文对象，不修改搜索缓存列表中的原项。

        :param context: 用户在消息交互中选中的单个资源上下文
        :return: 转换后的浅拷贝上下文，或无需转换时的原上下文
        """
        media, meta = context.media_info, context.meta_info
        if (
            not isinstance(media, MediaInfo)
            or meta is None
            or media.type not in (MediaType.MOVIE, MediaType.TV)
            or not is_tmdb_recognize_source()
        ):
            return context
        source, media_id = resolve_media_identity(media=media)
        if not source or not media_id or source == MediaSource.TMDB:
            return context
        converted = convert_download_identity_to_tmdb(
            MediaChain(),
            meta,
            source,
            media_id,
            source_media=media,
        )
        if converted is None:
            return context
        converted_meta, converted_media, origin_media = converted
        updated = copy(context)
        updated.meta_info = converted_meta
        updated.media_info = converted_media
        updated.origin_media = origin_media
        return updated

    def _prompt_download_dir_selection(
            self,
            request: PendingMediaInteraction,
            download_mode: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            context: Optional[Context] = None,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        在下载前进入目录选择阶段；没有配置下载目录时保持原下载流程。
        """
        original_chat_id = original_chat_id or request.original_chat_id
        media_info = context.media_info if context else request.current_media
        download_dirs = self._get_download_dirs(media_info)
        if not download_dirs:
            return False
        if len(download_dirs) == 1 and not self._is_auto_download_dir(download_dirs[0]):
            return False

        request.pending_torrent_page = request.page
        request.phase = "download-dir"
        request.page = 0
        request.download_dirs = download_dirs
        request.pending_download_mode = download_mode
        request.pending_download_context = context
        request.pending_no_exists = no_exists
        self._post_download_dirs_message(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        return True

    def _handle_download_dir_selection(
            self,
            request: PendingMediaInteraction,
            page_index: Optional[int],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
    ) -> None:
        """
        处理下载目录阶段的序号输入，并继续执行挂起的下载动作。
        """
        if request.phase != "download-dir":
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_chat_id=request.original_chat_id,
            )
            return
        if not self._require_scope(
            allowed_scopes,
            "download",
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        ):
            return

        page_items, page, _ = self._page_items(
            items=request.download_dirs,
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        request.page = page
        if not page_index or page_index < 1 or page_index > len(page_items):
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_chat_id=request.original_chat_id,
            )
            return

        download_dir = page_items[page_index - 1]
        if self._is_auto_download_dir(download_dir):
            self._execute_pending_download(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                save_path=None,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return

        save_path = download_dir.save_path or download_dir.download_path
        if not save_path:
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="下载目录配置无效！",
                original_chat_id=request.original_chat_id,
            )
            return
        self._execute_pending_download(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            save_path=save_path,
            allowed_scopes=allowed_scopes,
            owner_username=owner_username,
        )

    def _execute_pending_download(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            save_path: Optional[str],
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
    ) -> None:
        """
        使用用户确认的下载目录执行单资源下载或自动择优下载。
        """
        owner = self._operation_actor("download.add", channel, source, userid, username)
        if owner is None:
            return
        username, identity = owner
        if get_channel_authorization() is None:
            username = owner_username or username
        download_mode = request.pending_download_mode
        if download_mode == "single" and request.pending_download_context:
            context = request.pending_download_context
            self._restore_torrent_phase(request)
            self._submit_channel_download(
                lambda: DownloadChain().download_single(
                    context,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    user_specified=True,
                    save_path=save_path,
                ), channel, source, userid, username, identity, request.original_chat_id,
            )
            return

        if download_mode == "auto":
            cache_list = list(request.items or [])
            no_exists = request.pending_no_exists
            self._restore_torrent_phase(request)
            self._auto_download(
                request=request,
                cache_list=cache_list,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                no_exists=no_exists,
                save_path=save_path,
                allowed_scopes=allowed_scopes,
                owner_username=owner_username,
            )
            return

        self._restore_torrent_phase(request)
        self._post_invalid_input(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title="下载操作已失效，请重新选择资源",
            original_chat_id=request.original_chat_id,
        )

    @staticmethod
    def _restore_torrent_phase(request: PendingMediaInteraction) -> None:
        """
        下载动作完成或失效后恢复到资源列表阶段，便于用户继续选择其它资源。
        """
        request.phase = "torrent"
        request.page = request.pending_torrent_page
        request.download_dirs = []
        request.pending_download_mode = None
        request.pending_download_context = None
        request.pending_no_exists = None
        request.pending_torrent_page = 0

    @staticmethod
    def _batch_seasons_supported(
        converted_media: MediaInfo,
        matching_contexts: List[Context],
        no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]],
        old_key: str,
    ) -> bool:
        """
        核验批量自动下载涉及的缺集季号和候选显式季号是否均存在于转换后的 TMDB 剧集中。

        :param converted_media: 转换得到的 TMDB 媒体对象
        :param matching_contexts: 绑定了原非 TMDB 身份的候选上下文
        :param no_exists: 调用方已计算的缺集映射，可为空
        :param old_key: 原非 TMDB 身份对应的媒体键
        :return: 所有目标季均受支持或无需校验时返回 True，存在缺失季时返回 False
        """
        if converted_media.type != MediaType.TV:
            return True
        available = available_tmdb_seasons(converted_media)
        if not available:
            return True
        if no_exists and old_key in no_exists:
            if any(season not in available for season in no_exists[old_key]):
                return False
        for item in matching_contexts:
            item_meta = item.meta_info
            if item_meta is None:
                continue
            if any(season not in available for season in explicit_meta_seasons(item_meta)):
                return False
        return True

    @classmethod
    def _prepare_auto_download_batch(
        cls,
        current_media: MediaInfo | MusicInfo,
        meta: MetaBase,
        cache_list: List[Context],
        no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]],
    ) -> Tuple[
        MediaInfo | MusicInfo,
        MetaBase,
        List[Context],
        Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]],
    ]:
        """
        在自动择优下载前同步转换目标媒体、候选列表和缺集映射的 TMDB 主身份。

        批内目标键、待确认防重键和补建订阅共享同一身份；任一季号核验失败时整批回退原身份。

        :param current_media: 当前交互选中的媒体目标
        :param meta: 本次搜索请求元数据
        :param cache_list: 待提交的候选资源上下文列表
        :param no_exists: 已计算的缺集映射，可为 None
        :return: 转换或回退后的 ``(目标媒体, 元数据, 候选列表, 缺集映射)``
        """
        if (
            not isinstance(current_media, MediaInfo)
            or current_media.type not in (MediaType.MOVIE, MediaType.TV)
            or not is_tmdb_recognize_source()
        ):
            return current_media, meta, cache_list, no_exists
        source, media_id = resolve_media_identity(media=current_media)
        if not source or not media_id or source == MediaSource.TMDB:
            return current_media, meta, cache_list, no_exists
        converted = convert_download_identity_to_tmdb(
            MediaChain(),
            meta,
            source,
            media_id,
            source_media=current_media,
        )
        if converted is None:
            return current_media, meta, cache_list, no_exists
        converted_meta, converted_media, origin_media = converted
        old_key = build_media_key(source, media_id)
        new_key = build_media_key(MediaSource.TMDB, converted_media.media_id)
        matching_contexts = [
            item for item in cache_list
            if resolve_media_identity(media=item.media_info) == (source, media_id)
        ]
        if not new_key or not cls._batch_seasons_supported(converted_media, matching_contexts, no_exists, old_key):
            logger.warning(
                f"交互自动下载转换后的 TMDB {converted_media.media_id} 季信息与候选或缺集不兼容，"
                f"保留原身份 {source.value}:{media_id}"
            )
            return current_media, meta, cache_list, no_exists
        converted_seasons = explicit_meta_seasons(converted_meta)
        fill_season = converted_seasons[0] if len(converted_seasons) == 1 else None
        remapped_contexts: List[Context] = []
        for item in cache_list:
            if resolve_media_identity(media=item.media_info) != (source, media_id):
                remapped_contexts.append(item)
                continue
            updated_item = copy(item)
            updated_item.media_info = converted_media
            updated_item.origin_media = origin_media
            if (
                converted_media.type == MediaType.TV
                and updated_item.meta_info is not None
                and fill_season is not None
                and not explicit_meta_seasons(updated_item.meta_info)
            ):
                updated_meta = deepcopy(updated_item.meta_info)
                updated_meta.begin_season = fill_season
                if getattr(updated_meta, "total_season", 0) == 0:
                    updated_meta.total_season = 1
                updated_item.meta_info = updated_meta
            remapped_contexts.append(updated_item)
        remapped_no_exists = no_exists
        if no_exists is not None and old_key in no_exists:
            remapped_no_exists = {
                (new_key if key == old_key else key): value
                for key, value in no_exists.items()
            }
        return converted_media, converted_meta, remapped_contexts, remapped_no_exists

    def _auto_download(
            self,
            request: PendingMediaInteraction,
            cache_list: List[Context],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
            save_path: Optional[str] = None,
            allowed_scopes: Optional[Mapping[str, Any]] = None,
            owner_username: Optional[str] = None,
    ) -> None:
        """
        自动择优下载当前资源列表，并在未完成时补建订阅。
        """
        owner = self._operation_actor("download.add", channel, source, userid, username)
        if owner is None:
            return
        username, identity = owner
        if get_channel_authorization() is None:
            username = owner_username or username
        current_media = request.current_media
        meta = request.meta
        if not current_media or not meta:
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="下载交互信息已失效，请重新搜索媒体",
                original_chat_id=request.original_chat_id,
            )
            return
        current_media, meta, cache_list, no_exists = self._prepare_auto_download_batch(
            current_media,
            meta,
            cache_list,
            no_exists,
        )
        downloadchain = DownloadChain()
        if no_exists is None:
            exist_flag, no_exists = downloadchain.get_no_exists_info(
                meta=meta,
                mediainfo=current_media,
            )
            if exist_flag:
                no_exists = self._get_noexits_info(meta, current_media)

        result = self._submit_channel_download(
            lambda: downloadchain.batch_download(
                contexts=cache_list,
                no_exists=no_exists,
                save_path=save_path,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            ), channel, source, userid, username, identity, request.original_chat_id,
        )
        if result is None:
            return
        downloads, lefts = result
        if downloads and not lefts:
            logger.info("%s 下载完成", current_media.title_year)
            return
        if not self._has_scope(allowed_scopes, "subscribe"):
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="当前渠道成员未获授权使用订阅能力，未自动添加缺失订阅",
                    save_history=False,
                )
            )
            return

        logger.info("%s 未下载未完整，添加订阅 ...", current_media.title_year)
        if downloads and current_media.type == MediaType.TV:
            note = [
                download.meta_info.begin_episode
                for download in downloads
                if download.meta_info.begin_episode
            ]
        else:
            note = None

        subscription_owner = self._operation_actor("subscription.add", channel, source, userid, username, explain=False)
        if subscription_owner is None:
            return
        mp_name, subscription_identity = subscription_owner
        if get_channel_authorization() is None:
            mp_name = owner_username or (
                self.user_repository.find_name_by_bindings({f"{channel.name.lower()}_userid": userid}) or username
            )
        submit_media_subscription(
            self, channel, source, userid, request.original_chat_id,
            title=current_media.title or "",
            year=str(current_media.year or ""),
            mtype=current_media.type,
            media_source=current_media.media_source,
            media_id=current_media.media_id,
            season=meta.begin_season,
            username=mp_name,
            owner_identity=subscription_identity,
            owner_scope=bool(subscription_identity),
            state="R",
            note=note,
            music_type=selected_music_type(current_media),
        )

    def _render_interaction(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        按当前阶段渲染媒体列表或资源列表。
        """
        original_chat_id = original_chat_id or request.original_chat_id
        if request.phase == "download-dir":
            self._post_download_dirs_message(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
        elif request.phase == "torrent":
            self._post_torrents_message(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
        else:
            self._post_medias_message(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )

    def _post_medias_message(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新媒体选择列表。
        """
        original_chat_id = original_chat_id or request.original_chat_id
        if request.action.startswith("Music"):
            post_music_candidates(
                host=self,
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return
        page_items, page, total_pages = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(channel),
        )
        request.page = page
        total = len(request.items)
        if self._supports_interactive_buttons(channel):
            title = f"【{request.title}】共找到{total}条相关信息，请选择操作"
            buttons = self._create_media_buttons(
                channel=channel,
                request=request,
                items=page_items,
                total=total,
                total_pages=total_pages,
            )
        else:
            if total > self._page_size(channel):
                title = f"【{request.title}】共找到{total}条相关信息，请回复对应数字选择（p: 上一页 n: 下一页）"
            else:
                title = f"【{request.title}】共找到{total}条相关信息，请回复对应数字选择"
            buttons = None

        self.post_medias_message(
            Message(
                channel=channel,
                source=source,
                title=title,
                userid=userid,
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                save_history=False,
            ),
            medias=page_items,
        )

    def _post_torrents_message(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新资源选择列表。
        """
        original_chat_id = original_chat_id or request.original_chat_id
        page_items, page, total_pages = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(channel),
        )
        request.page = page
        total = len(request.items)
        if self._supports_interactive_buttons(channel):
            title = f"【{request.title}】共找到{total}条相关资源，请选择下载"
            buttons = self._create_torrent_buttons(
                channel=channel,
                request=request,
                items=page_items,
                total=total,
                total_pages=total_pages,
            )
        else:
            page_hint = " p: 上一页 n: 下一页" if total > self._page_size(channel) else ""
            title = f"【{request.title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择{page_hint}）"
            buttons = None

        post_music_aware_torrents(
            self,
            Message(
                channel=channel,
                source=source,
                title=title,
                userid=userid,
                link=self.runtime_config.resource_url,
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                save_history=False,
            ),
            torrents=page_items,
        )

    def _post_download_dirs_message(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新下载目录选择列表。
        """
        original_chat_id = original_chat_id or request.original_chat_id
        page_items, page, total_pages = self._page_items(
            items=request.download_dirs,
            page=request.page,
            page_size=self._page_size(channel),
        )
        request.page = page
        total = len(request.download_dirs)
        if self._supports_interactive_buttons(channel):
            title = f"【{request.title}】请选择下载目录"
            buttons = self._create_download_dir_buttons(
                channel=channel,
                request=request,
                items=page_items,
                total=total,
                total_pages=total_pages,
            )
        else:
            if total > self._page_size(channel):
                title = f"【{request.title}】请选择下载目录，请回复对应数字（p: 上一页 n: 下一页）"
            else:
                title = f"【{request.title}】请选择下载目录，请回复对应数字"
            buttons = None

        text = "\n".join(
            f"{index}. {self._format_download_dir_label(download_dir)}"
            for index, download_dir in enumerate(page_items, start=1)
        )
        self.post_message(
            Message(
                channel=channel,
                source=source,
                title=title,
                text=text,
                userid=userid,
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                save_history=False,
            )
        )

    def _create_media_buttons(
            self,
            channel: NotificationChannel,
            request: PendingMediaInteraction,
            items: List[MediaInfo | MusicInfo],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为媒体列表生成选择和翻页按钮。
        """
        version = request.callback_versions["media"] = uuid.uuid4().hex[:10]
        buttons: List[List[Dict[str, str]]] = []
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, media in enumerate(items, start=1):
            if max_per_row == 1:
                button_text = (
                    format_music_candidate(index, media)
                    if isinstance(media, MusicInfo)
                    else f"{index}. {media.title_year}"
                )
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:select:{index}:{version}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:select:{index}:{version}",
                }
            )
            if len(current_row) == max_per_row or index == len(items):
                buttons.append(current_row)
                current_row = []

        if total > self._page_size(channel):
            buttons.extend(self._navigation_buttons(request, total_pages))
        return buttons

    def _create_torrent_buttons(
            self,
            channel: NotificationChannel,
            request: PendingMediaInteraction,
            items: List[Context],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为资源列表生成下载和翻页按钮。
        """
        version = request.callback_versions["torrent"] = uuid.uuid4().hex[:10]
        buttons: List[List[Dict[str, str]]] = [
            [
                {
                    "text": "🤖 自动选择下载",
                    "callback_data": f"media:{request.request_id}:download:0:{version}",
                }
            ]
        ]
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, context in enumerate(items, start=1):
            torrent = context.torrent_info
            if torrent is None:
                continue
            if max_per_row == 1:
                button_text = f"{index}. {torrent.site_name} - {torrent.seeders}↑"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:download:{index}:{version}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:download:{index}:{version}",
                }
            )
            if len(current_row) == max_per_row or index == len(items):
                buttons.append(current_row)
                current_row = []

        if total > self._page_size(channel):
            buttons.extend(self._navigation_buttons(request, total_pages))
        return buttons

    def _create_download_dir_buttons(
            self,
            channel: NotificationChannel,
            request: PendingMediaInteraction,
            items: List[DownloadDirectory],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为下载目录列表生成选择和翻页按钮。
        """
        version = request.callback_versions["download-dir"] = uuid.uuid4().hex[:10]
        buttons: List[List[Dict[str, str]]] = []
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, download_dir in enumerate(items, start=1):
            if max_per_row == 1:
                button_text = f"{index}. {self._format_download_dir_label(download_dir)}"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:download-dir:{index}:{version}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:download-dir:{index}:{version}",
                }
            )
            if len(current_row) == max_per_row or index == len(items):
                buttons.append(current_row)
                current_row = []

        if total > self._page_size(channel):
            buttons.extend(self._navigation_buttons(request, total_pages))
        return buttons

    def _has_next_page(self, request: PendingMediaInteraction) -> bool:
        """
        判断当前视图是否还有下一页。
        """
        _, page, total_pages = self._page_items(
            items=self._get_current_phase_items(request),
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        return page < total_pages - 1

    @staticmethod
    def _get_current_phase_items(request: PendingMediaInteraction) -> List[Any]:
        """
        获取当前阶段用于分页的数据列表。
        """
        if request.phase == "download-dir":
            return request.download_dirs
        return request.items

    @staticmethod
    def _navigation_buttons(
            request: PendingMediaInteraction,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        按当前页状态生成上一页和下一页按钮。
        """
        buttons: List[List[Dict[str, str]]] = []
        nav_row: List[Dict[str, str]] = []
        if request.page > 0:
            nav_row.append(
                {
                    "text": "⬅️ 上一页",
                    "callback_data": f"media:{request.request_id}:page-prev",
                }
            )
        if request.page < total_pages - 1:
            nav_row.append(
                {
                    "text": "下一页 ➡️",
                    "callback_data": f"media:{request.request_id}:page-next",
                }
            )
        if nav_row:
            buttons.append(nav_row)
        return buttons

    @staticmethod
    def _page_items(
            items: List[Any],
            page: int,
            page_size: int,
    ) -> Tuple[List[Any], int, int]:
        """
        返回当前页数据，并把页码限制在有效范围内。
        """
        total_pages = max(1, math.ceil(len(items) / page_size)) if page_size else 1
        page = min(max(0, page), total_pages - 1)
        start = page * page_size
        end = start + page_size
        return items[start:end], page, total_pages

    @classmethod
    def _get_download_dirs(
            cls, media_info: Optional[MediaInfo | MusicInfo] = None
    ) -> List[DownloadDirectory]:
        """
        获取可供消息交互选择的下载目录。
        """
        dir_infos = [
            dir_info
            for dir_info in DirectoryHelper().get_download_dirs()
            if dir_info.download_path
        ]
        download_dirs = [
            DownloadDirectory(
                name=dir_info.name,
                storage=dir_info.storage or "local",
                download_path=dir_info.download_path,
                save_path=FileURI(
                    storage=dir_info.storage or "local",
                    path=dir_info.download_path,
                ).uri,
                priority=dir_info.priority,
                media_type=dir_info.media_type,
                media_category=dir_info.media_category,
                media_category_id=dir_info.media_category_id,
            )
            for dir_info in dir_infos
            if cls._match_download_dir_media(dir_info, media_info)
        ]
        if not download_dirs:
            return []
        if len(download_dirs) == 1:
            return download_dirs
        return [cls._build_auto_download_dir(), *download_dirs]

    @classmethod
    def _build_auto_download_dir(cls) -> DownloadDirectory:
        """
        构造自动匹配下载目录选项。
        """
        return DownloadDirectory(
            name=cls._auto_download_dir_name,
            storage="local",
            priority=-1,
        )

    @classmethod
    def _is_auto_download_dir(cls, download_dir: DownloadDirectory) -> bool:
        """
        判断是否为自动匹配下载目录选项。
        """
        return (
            download_dir.name == cls._auto_download_dir_name
            and not download_dir.download_path
            and not download_dir.save_path
        )

    @staticmethod
    def _match_download_dir_media(
            dir_info: TransferDirectoryConf,
            media_info: Optional[MediaInfo | MusicInfo],
    ) -> bool:
        """
        判断下载目录是否适用于当前媒体。
        """
        return DirectoryHelper().matches_media(dir_info, media_info)

    @staticmethod
    def _format_download_dir_label(download_dir: DownloadDirectory) -> str:
        """
        格式化下载目录展示名称，优先显示用户配置的目录名称。
        """
        save_path = download_dir.save_path or download_dir.download_path or ""
        name = download_dir.name or save_path or "下载目录"
        if save_path and name != save_path:
            return f"{name} ({save_path})"
        return name

    def _page_size(self, channel: Optional[NotificationChannel]) -> int:
        """
        按渠道交互能力选择分页大小。
        """
        return (
            self._button_page_size
            if self._supports_interactive_buttons(channel)
            else self._text_page_size
        )

    @staticmethod
    def _supports_interactive_buttons(channel: Optional[NotificationChannel]) -> bool:
        """
        判断渠道是否同时支持按钮展示与按钮回调。
        """
        return bool(
            channel
            and ChannelCapabilityManager.supports_buttons(channel)
            and ChannelCapabilityManager.supports_callbacks(channel)
        )

    @staticmethod
    def _build_no_exists_messages(
            mediainfo: MediaInfo | MusicInfo,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]],
            show_missing_only: bool,
    ) -> List[str]:
        """
        将缺失集信息转换为可发送的文案。
        """
        if not no_exists:
            return []
        media_source, media_id = resolve_media_identity(media=mediainfo)
        mediakey = build_media_key(media_source, media_id)
        season_map = no_exists.get(mediakey) or {}
        if show_missing_only:
            return [
                f"第 {sea} 季缺失 {episode_rules.compact_numbers(no_exist.episodes) if no_exist.episodes else no_exist.total_episode} 集"
                for sea, no_exist in season_map.items()
            ]
        return [
            f"第 {sea} 季总 {no_exist.total_episode} 集"
            for sea, no_exist in season_map.items()
        ]

    @staticmethod
    def _should_auto_download(userid: Union[str, int], source: str, channel: NotificationChannel) -> bool:
        """
        检查当前连接的自动择优偏好，执行入口另行授权。
        """
        return channel_search_auto_download(channel.value, source, str(userid))

    def _post_invalid_input(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: Optional[str],
            title: str = "输入有误！",
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送统一的非法输入提示。
        """
        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=title,
                save_history=False,
                original_chat_id=original_chat_id,
            )
        )
