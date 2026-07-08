import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.schemas import Notification
from app.schemas.message import ChannelCapabilityManager
from app.schemas.types import MessageChannel


@dataclass
class PendingSlashInteraction:
    """
    通用 slash 命令交互上下文。
    """

    request_id: str
    user_id: str
    channel: Optional[MessageChannel]
    source: Optional[str]
    username: Optional[str]
    command: str
    page: int = 0
    awaiting_input: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


class SlashInteractionManager:
    """
    管理单个 slash 命令的交互会话。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        self._by_id: Dict[str, PendingSlashInteraction] = {}
        self._by_user: Dict[str, str] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        expire_before = datetime.now() - self._ttl
        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.created_at < expire_before
        ]
        for request_id in expired:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def create_or_replace(
            self,
            user_id: Union[str, int],
            command: str,
            channel: Optional[MessageChannel],
            source: Optional[str],
            username: Optional[str],
    ) -> PendingSlashInteraction:
        with self._lock:
            self._cleanup_locked()
            user_key = str(user_id)
            old_request_id = self._by_user.get(user_key)
            if old_request_id:
                self._by_id.pop(old_request_id, None)
            request = PendingSlashInteraction(
                request_id=uuid.uuid4().hex[:12],
                user_id=user_key,
                command=command,
                channel=channel,
                source=source,
                username=username,
            )
            self._by_id[request.request_id] = request
            self._by_user[user_key] = request.request_id
            return request

    def get_by_user(
            self, user_id: Union[str, int]
    ) -> Optional[PendingSlashInteraction]:
        with self._lock:
            self._cleanup_locked()
            request_id = self._by_user.get(str(user_id))
            if not request_id:
                return None
            return self._by_id.get(request_id)

    def get_by_id(
            self, request_id: str, user_id: Union[str, int]
    ) -> Optional[PendingSlashInteraction]:
        with self._lock:
            self._cleanup_locked()
            request = self._by_id.get(request_id)
            if not request or str(request.user_id) != str(user_id):
                return None
            return request

    def remove(self, request_id: str) -> None:
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def clear(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_user.clear()


def supports_interaction_buttons(channel: Optional[MessageChannel]) -> bool:
    """
    渠道同时支持按钮和回调时，优先使用按钮交互。
    """
    return bool(
        channel
        and ChannelCapabilityManager.supports_buttons(channel)
        and ChannelCapabilityManager.supports_callbacks(channel)
    )


def supports_markdown(channel: Optional[MessageChannel]) -> bool:
    """
    仅在支持 Markdown 的渠道上输出 Markdown 内容。
    """
    return bool(channel and ChannelCapabilityManager.supports_markdown(channel))


def page_items(
        items: Sequence[Any],
        page: int,
        page_size: int,
) -> Tuple[List[Any], int, int]:
    """
    对列表做分页并规范化页码。
    """
    total = len(items)
    if total == 0:
        return [], 0, 1
    total_pages = max(1, math.ceil(total / max(1, page_size)))
    page = min(max(0, page), total_pages - 1)
    start = page * page_size
    end = start + page_size
    return list(items[start:end]), page, total_pages


def build_navigation_buttons(
        prefix: str,
        request: Any,
        page: int,
        total_pages: int,
) -> List[List[dict]]:
    """
    构造标准上一页/下一页按钮。
    """
    buttons = []
    nav_row = []
    if page > 0:
        nav_row.append(
            {
                "text": "⬅️ 上一页",
                "callback_data": f"{prefix}:{request.request_id}:page-prev",
            }
        )
    if page < total_pages - 1:
        nav_row.append(
            {
                "text": "下一页 ➡️",
                "callback_data": f"{prefix}:{request.request_id}:page-next",
            }
        )
    if nav_row:
        buttons.append(nav_row)
    return buttons


def update_or_post_message(
        chain,
        channel: MessageChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        title: str,
        text: str,
        buttons: Optional[List[List[dict]]] = None,
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
) -> None:
    """
    优先编辑原消息，失败时回退为发送新消息。
    """
    if (
            original_message_id
            and original_chat_id
            and ChannelCapabilityManager.supports_editing(channel)
    ):
        edit_kwargs = {}
        if channel == MessageChannel.WebAgent:
            edit_kwargs["metadata"] = {"userid": userid}
        edited = chain.edit_message(
            channel=channel,
            source=source,
            message_id=original_message_id,
            chat_id=original_chat_id,
            title=title,
            text=text,
            buttons=buttons,
            **edit_kwargs,
        )
        if edited:
            return

    chain.post_message(
        Notification(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title=title,
            text=text,
            buttons=buttons,
            save_history=False,
        )
    )


def escape_markdown_table_cell(value: object) -> str:
    """
    最小化转义 Markdown 表格中的特殊字符。
    """
    text = str(value or "").replace("\n", "<br>")
    return text.replace("|", "\\|")


def format_markdown_table(
        headers: Sequence[str],
        rows: Sequence[Sequence[object]],
) -> str:
    """
    生成 Markdown 表格文本。
    """
    header_line = (
            "| "
            + " | ".join(escape_markdown_table_cell(item) for item in headers)
            + " |"
    )
    separator_line = "| " + " | ".join("---" for _ in headers) + " |"
    data_lines = [
        "| "
        + " | ".join(escape_markdown_table_cell(item) for item in row)
        + " |"
        for row in rows
    ]
    return "\n".join([header_line, separator_line, *data_lines])


@dataclass
class PendingMediaInteraction:
    """
    记录一次搜索/下载/订阅交互的当前上下文。
    """

    request_id: str
    user_id: str
    channel: Optional[MessageChannel]
    source: Optional[str]
    username: Optional[str]
    action: str
    keyword: str
    phase: str = "media"
    page: int = 0
    title: str = ""
    meta: Optional[MetaBase] = None
    current_media: Optional[MediaInfo] = None
    items: List[Any] = field(default_factory=list)
    download_dirs: List[Any] = field(default_factory=list)
    pending_download_mode: Optional[str] = None
    pending_download_context: Optional[Any] = None
    pending_no_exists: Optional[Dict[Any, Any]] = None
    pending_torrent_page: int = 0
    created_at: datetime = field(default_factory=datetime.now)


class MediaInteractionManager:
    """
    管理用户当前激活的媒体交互状态。

    每个用户只保留一个有效会话，避免旧按钮与新一轮搜索混用。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        self._by_id: Dict[str, PendingMediaInteraction] = {}
        self._by_user: Dict[str, str] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        """
        清理超时会话，避免内存中残留旧交互状态。
        """
        expire_before = datetime.now() - self._ttl
        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.created_at < expire_before
        ]
        for request_id in expired:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def create_or_replace(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
            source: Optional[str],
            username: Optional[str],
            action: str,
            keyword: str,
            title: str = "",
            meta: Optional[MetaBase] = None,
            items: Optional[List[Any]] = None,
    ) -> PendingMediaInteraction:
        """
        为用户创建新的交互状态，并替换旧会话。
        """
        with self._lock:
            self._cleanup_locked()
            user_key = str(user_id)
            old_request_id = self._by_user.get(user_key)
            if old_request_id:
                self._by_id.pop(old_request_id, None)

            request = PendingMediaInteraction(
                request_id=uuid.uuid4().hex[:12],
                user_id=user_key,
                channel=channel,
                source=source,
                username=username,
                action=action,
                keyword=keyword,
                title=title,
                meta=meta,
                items=list(items or []),
            )
            self._by_id[request.request_id] = request
            self._by_user[user_key] = request.request_id
            return request

    def get_by_user(
            self, user_id: Union[str, int]
    ) -> Optional[PendingMediaInteraction]:
        """
        按用户读取当前会话，供文本回复和旧按钮兼容使用。
        """
        with self._lock:
            self._cleanup_locked()
            request_id = self._by_user.get(str(user_id))
            if not request_id:
                return None
            return self._by_id.get(request_id)

    def get_by_id(
            self, request_id: str, user_id: Union[str, int]
    ) -> Optional[PendingMediaInteraction]:
        """
        按请求 ID 读取会话，并校验用户归属。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._by_id.get(request_id)
            if not request or str(request.user_id) != str(user_id):
                return None
            return request

    def remove(self, request_id: str) -> None:
        """
        主动结束一条会话。
        """
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def clear(self) -> None:
        """
        清空所有交互状态，主要用于测试。
        """
        with self._lock:
            self._by_id.clear()
            self._by_user.clear()


media_interaction_manager = MediaInteractionManager()


@dataclass
class PendingPluginInputInteraction:
    """
    记录插件临时接管用户下一条文本输入的会话。
    """

    request_id: str
    user_id: str
    plugin_id: str
    channel: Optional[MessageChannel]
    source: Optional[str]
    username: Optional[str]
    chat_id: Optional[str] = None
    prompt_id: Optional[str] = None
    payload: Optional[Any] = None
    timeout_seconds: int = 120
    created_at: datetime = field(default_factory=datetime.now)
    # Optional reply binding for channels that can report reply_to_message_id.
    prompt_message_id: Optional[str] = None

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=max(1, self.timeout_seconds))


class PluginInputInteractionManager:
    """
    管理插件输入会话。

    会话按用户和渠道绑定；同一用户在同一渠道只保留一个待输入会话。
    """

    EXPIRED_GRACE_SECONDS = 300

    def __init__(self):
        self._by_id: Dict[str, PendingPluginInputInteraction] = {}
        self._by_user_channel: Dict[Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]], str] = {}
        self._expired_by_user_channel: Dict[
            Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]],
            PendingPluginInputInteraction,
        ] = {}
        self._lock = Lock()

    @staticmethod
    def _user_channel_source_key(
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]]:
        return str(user_id), channel, source, str(chat_id) if chat_id not in (None, "") else None

    @classmethod
    def _keys_overlap(
            cls,
            left: Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]],
            right: Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]],
    ) -> bool:
        left_user, left_channel, left_source, left_chat_id = left
        right_user, right_channel, right_source, right_chat_id = right
        if left_user != right_user:
            return False
        if left_chat_id and right_chat_id and left_chat_id != right_chat_id:
            return False
        if (left_channel is None and left_source is None) or (right_channel is None and right_source is None):
            return left_channel == right_channel and left_source == right_source
        channel_overlap = left_channel == right_channel or left_channel is None or right_channel is None
        source_overlap = left_source == right_source or left_source is None or right_source is None
        return channel_overlap and source_overlap

    def _cleanup_locked(self) -> None:
        now = datetime.now()
        expired_tombstones = [
            key
            for key, request in self._expired_by_user_channel.items()
            if request.expires_at + timedelta(seconds=self.EXPIRED_GRACE_SECONDS) < now
        ]
        for key in expired_tombstones:
            self._expired_by_user_channel.pop(key, None)

        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.expires_at < now
        ]
        for request_id in expired:
            request = self._by_id.pop(request_id, None)
            if request:
                key = self._user_channel_source_key(
                    request.user_id,
                    request.channel,
                    request.source,
                    request.chat_id,
                )
                self._by_user_channel.pop(key, None)
                self._expired_by_user_channel[key] = request

    def create_or_replace(
            self,
            user_id: Union[str, int],
            plugin_id: str,
            channel: Optional[MessageChannel],
            source: Optional[str],
            username: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
            prompt_id: Optional[str] = None,
            timeout_seconds: int = 120,
            payload: Optional[Any] = None,
            *,
            prompt_message_id: Optional[Union[str, int]] = None,
    ) -> PendingPluginInputInteraction:
        with self._lock:
            self._cleanup_locked()
            key = self._user_channel_source_key(user_id, channel, source, chat_id)
            old_request_ids = [
                request_id
                for stored_key, request_id in self._by_user_channel.items()
                if self._keys_overlap(stored_key, key)
            ]
            for old_request_id in old_request_ids:
                self._by_id.pop(old_request_id, None)
            self._by_user_channel = {
                stored_key: request_id
                for stored_key, request_id in self._by_user_channel.items()
                if request_id not in old_request_ids
            }
            self._expired_by_user_channel = {
                stored_key: request
                for stored_key, request in self._expired_by_user_channel.items()
                if not self._keys_overlap(stored_key, key)
            }

            normalized_chat_id = str(chat_id) if chat_id not in (None, "") else None
            normalized_prompt_message_id = (
                str(prompt_message_id)
                if channel == MessageChannel.Telegram and normalized_chat_id and prompt_message_id not in (None, "")
                else None
            )

            request = PendingPluginInputInteraction(
                request_id=uuid.uuid4().hex[:12],
                user_id=str(user_id),
                plugin_id=plugin_id,
                channel=channel,
                source=source,
                username=username,
                chat_id=normalized_chat_id,
                prompt_id=prompt_id,
                prompt_message_id=normalized_prompt_message_id,
                timeout_seconds=timeout_seconds,
                payload=payload,
            )
            self._by_id[request.request_id] = request
            self._by_user_channel[key] = request.request_id
            return request

    def get_by_user(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel] = None,
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> Optional[PendingPluginInputInteraction]:
        with self._lock:
            self._cleanup_locked()
            request_id = self._find_request_id_locked(user_id, channel, source, chat_id)
            if request_id:
                return self._by_id.get(request_id)
            return None

    def pop_by_user(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel] = None,
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> Optional[PendingPluginInputInteraction]:
        with self._lock:
            self._cleanup_locked()
            key, request_id = self._find_key_and_request_id_locked(user_id, channel, source, chat_id)
            if request_id:
                self._by_user_channel.pop(key, None)
                return self._by_id.pop(request_id, None)
            expired_key, request = self._find_expired_key_and_request_locked(user_id, channel, source, chat_id)
            if expired_key:
                self._expired_by_user_channel.pop(expired_key, None)
            return request

    def consume_by_user(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel] = None,
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
            *,
            reply_to_message_id: Optional[Union[str, int]] = None,
            bypass_reply_check: bool = False,
    ) -> Tuple[Optional[PendingPluginInputInteraction], Optional[str]]:
        with self._lock:
            key, request_id = self._find_key_and_request_id_locked(user_id, channel, source, chat_id)

            if request_id:
                request = self._by_id.get(request_id)
                if not request:
                    self._by_user_channel.pop(key, None)
                elif request.expires_at < datetime.now():
                    self._by_user_channel.pop(key, None)
                    self._by_id.pop(request_id, None)
                    if request.prompt_message_id:
                        return None, None
                    return request, "expired"
                elif not self._reply_matches_prompt(
                        request,
                        chat_id,
                        reply_to_message_id,
                        ignore_reply_to_message_id=bypass_reply_check,
                ):
                    return None, None
                else:
                    self._by_user_channel.pop(key, None)
                    self._by_id.pop(request_id, None)
                    return request, "active"
            self._cleanup_locked()
            key, request = self._find_expired_key_and_request_locked(user_id, channel, source, chat_id)
            if request:
                self._expired_by_user_channel.pop(key, None)
                if request.prompt_message_id:
                    return None, None
                return request, "expired"
            self._cleanup_locked()
            return None, None

    @staticmethod
    def _reply_matches_prompt(
            request: PendingPluginInputInteraction,
            chat_id: Optional[Union[str, int]],
            reply_to_message_id: Optional[Union[str, int]],
            *,
            ignore_reply_to_message_id: bool = False,
    ) -> bool:
        if not request.prompt_message_id:
            return True
        if not request.chat_id or chat_id in (None, ""):
            return False
        if str(chat_id) != str(request.chat_id):
            return False
        if ignore_reply_to_message_id:
            return True
        if reply_to_message_id in (None, ""):
            return False
        return str(reply_to_message_id) == str(request.prompt_message_id)

    def _find_request_id_locked(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> Optional[str]:
        _, request_id = self._find_key_and_request_id_locked(user_id, channel, source, chat_id)
        return request_id

    def _find_key_and_request_id_locked(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> Tuple[Optional[Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]]], Optional[str]]:
        for key in self._candidate_keys(user_id, channel, source, chat_id):
            request_id = self._by_user_channel.get(key)
            if request_id:
                return key, request_id
        return None, None

    def _find_expired_key_and_request_locked(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> Tuple[Optional[Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]]],
               Optional[PendingPluginInputInteraction]]:
        now = datetime.now()
        for key in self._candidate_keys(user_id, channel, source, chat_id):
            request = self._expired_by_user_channel.get(key)
            if not request:
                continue
            if request.expires_at + timedelta(seconds=self.EXPIRED_GRACE_SECONDS) < now:
                self._expired_by_user_channel.pop(key, None)
                continue
            return key, request
        return None, None

    def _candidate_keys(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> List[Tuple[str, Optional[MessageChannel], Optional[str], Optional[str]]]:
        chat_key = str(chat_id) if chat_id not in (None, "") else None
        candidates = [
            self._user_channel_source_key(user_id, channel, source, chat_key),
        ]
        if source is not None:
            candidates.append(self._user_channel_source_key(user_id, channel, None, chat_key))
        if channel is not None and source is not None:
            candidates.append(self._user_channel_source_key(user_id, None, source, chat_key))
        if channel is None and source is None:
            wildcard_key = self._user_channel_source_key(user_id, None, None, chat_key)
            candidates.append(wildcard_key)
        if chat_key is not None:
            candidates.append(self._user_channel_source_key(user_id, channel, source, None))
            if source is not None:
                candidates.append(self._user_channel_source_key(user_id, channel, None, None))
            if channel is not None and source is not None:
                candidates.append(self._user_channel_source_key(user_id, None, source, None))
            if channel is None and source is None:
                candidates.append(self._user_channel_source_key(user_id, None, None, None))
        return candidates

    def remove(self, request_id: str) -> None:
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user_channel.pop(
                    self._user_channel_source_key(request.user_id, request.channel, request.source, request.chat_id),
                    None,
                )

    def clear(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_user_channel.clear()
            self._expired_by_user_channel.clear()


plugin_input_interaction_manager = PluginInputInteractionManager()


@dataclass(frozen=True)
class AgentInteractionOption:
    """
    Agent 交互选项。
    """

    label: str
    value: str
    description: Optional[str] = None


@dataclass
class PendingAgentInteraction:
    """
    待处理的 Agent 客户端交互请求。
    """

    request_id: str
    session_id: str
    user_id: str
    channel: Optional[str]
    source: Optional[str]
    username: Optional[str]
    title: Optional[str]
    prompt: str
    options: List[AgentInteractionOption]
    created_at: datetime = field(default_factory=datetime.now)


class AgentInteractionManager:
    """
    管理 Agent 发起的客户端交互请求。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        self._pending_interactions: Dict[str, PendingAgentInteraction] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        expire_before = datetime.now() - self._ttl
        expired_ids = [
            request_id
            for request_id, request in self._pending_interactions.items()
            if request.created_at < expire_before
        ]
        for request_id in expired_ids:
            self._pending_interactions.pop(request_id, None)

    def create_request(
            self,
            session_id: str,
            user_id: str,
            channel: Optional[str],
            source: Optional[str],
            username: Optional[str],
            title: Optional[str],
            prompt: str,
            options: List[AgentInteractionOption],
    ) -> PendingAgentInteraction:
        """
        创建一条待用户确认的 Agent 交互请求。
        """
        with self._lock:
            self._cleanup_locked()
            request_id = uuid.uuid4().hex[:12]
            while request_id in self._pending_interactions:
                request_id = uuid.uuid4().hex[:12]
            request = PendingAgentInteraction(
                request_id=request_id,
                session_id=session_id,
                user_id=str(user_id),
                channel=channel,
                source=source,
                username=username,
                title=title,
                prompt=prompt,
                options=options,
            )
            self._pending_interactions[request_id] = request
            return request

    def resolve(
            self,
            request_id: str,
            option_index: int,
            user_id: Optional[str] = None,
    ) -> Optional[tuple[PendingAgentInteraction, AgentInteractionOption]]:
        """
        消费一条 Agent 交互请求，并返回选中的选项。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._pending_interactions.get(request_id)
            if not request:
                return None
            if user_id is not None and str(request.user_id) != str(user_id):
                return None
            if option_index < 1 or option_index > len(request.options):
                return None
            option = request.options[option_index - 1]
            self._pending_interactions.pop(request_id, None)
            return request, option

    def clear(self) -> None:
        """
        清空所有 Agent 交互请求。
        """
        with self._lock:
            self._pending_interactions.clear()


agent_interaction_manager = AgentInteractionManager()


@dataclass
class PendingSkillsInteraction:
    """
    记录一次 /skills 会话的上下文，便于按钮和文本回复共用同一状态。
    """

    request_id: str
    user_id: str
    channel: Optional[MessageChannel]
    source: Optional[str]
    username: Optional[str]
    view: str = "root"
    local_page: int = 0
    market_page: int = 0
    market_query: str = ""
    awaiting_input: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


class SkillsInteractionManager:
    """
    管理用户当前的技能交互状态。

    每个用户同一时间只保留一个有效会话，避免旧按钮继续生效。
    """

    _ttl = timedelta(hours=24)

    def __init__(self):
        self._by_id: Dict[str, PendingSkillsInteraction] = {}
        self._by_user: Dict[str, str] = {}
        self._lock = Lock()

    def _cleanup_locked(self):
        """
        清理超时会话，避免按钮回调无限积累。
        """
        expire_before = datetime.now() - self._ttl
        expired = [
            request_id
            for request_id, request in self._by_id.items()
            if request.created_at < expire_before
        ]
        for request_id in expired:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def create_or_replace(
            self,
            user_id: Union[str, int],
            channel: Optional[MessageChannel],
            source: Optional[str],
            username: Optional[str],
    ) -> PendingSkillsInteraction:
        """
        为用户创建新会话，并替换掉旧的技能交互状态。
        """
        with self._lock:
            self._cleanup_locked()
            user_key = str(user_id)
            old_request_id = self._by_user.get(user_key)
            if old_request_id:
                self._by_id.pop(old_request_id, None)
            request_id = uuid.uuid4().hex[:12]
            request = PendingSkillsInteraction(
                request_id=request_id,
                user_id=user_key,
                channel=channel,
                source=source,
                username=username,
            )
            self._by_id[request_id] = request
            self._by_user[user_key] = request_id
            return request

    def get_by_user(
            self, user_id: Union[str, int]
    ) -> Optional[PendingSkillsInteraction]:
        """
        按用户获取当前有效会话，供纯文本回复路由使用。
        """
        with self._lock:
            self._cleanup_locked()
            request_id = self._by_user.get(str(user_id))
            if not request_id:
                return None
            return self._by_id.get(request_id)

    def get_by_id(
            self, request_id: str, user_id: Union[str, int]
    ) -> Optional[PendingSkillsInteraction]:
        """
        按请求 ID 获取会话，并校验会话归属用户。
        """
        with self._lock:
            self._cleanup_locked()
            request = self._by_id.get(request_id)
            if not request or str(request.user_id) != str(user_id):
                return None
            return request

    def remove(self, request_id: str) -> None:
        """
        主动结束会话，释放用户和请求 ID 的双向索引。
        """
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user.pop(str(request.user_id), None)

    def clear(self):
        """
        清空所有会话，主要用于测试场景。
        """
        with self._lock:
            self._by_id.clear()
            self._by_user.clear()


skills_interaction_manager = SkillsInteractionManager()
