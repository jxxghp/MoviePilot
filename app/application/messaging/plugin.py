import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

from app.application.messaging.interaction import InteractionContext, MessageGateway
from app.schemas.message import Message
from app.schemas.types import EventType, NotificationChannel


class PluginEventPublisher(Protocol):
    """声明插件输入交互所需的同步事件发布端口。"""

    def send_event(self, event_type: EventType, payload: dict) -> Any:
        """同步发布插件输入事件，并返回宿主事件分发结果。"""
        ...


@dataclass
class PendingPluginInputInteraction:
    """
    记录插件临时接管用户下一条文本输入的会话。
    """

    request_id: str
    user_id: str
    plugin_id: str
    channel: Optional[NotificationChannel]
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
        """返回输入会话的绝对过期时间。"""
        return self.created_at + timedelta(seconds=max(1, self.timeout_seconds))


class PluginInputInteractionManager:
    """
    管理插件输入会话。

    会话按用户和渠道绑定；同一用户在同一渠道只保留一个待输入会话。
    """

    EXPIRED_GRACE_SECONDS = 300

    def __init__(self):
        """初始化活动输入会话、用户渠道索引和过期墓碑。"""
        self._by_id: Dict[str, PendingPluginInputInteraction] = {}
        self._by_user_channel: Dict[Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]], str] = {}
        self._expired_by_user_channel: Dict[
            Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]],
            PendingPluginInputInteraction,
        ] = {}
        self._lock = Lock()

    @staticmethod
    def _user_channel_source_key(
            user_id: Union[str, int],
            channel: Optional[NotificationChannel],
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]]:
        """归一化用户、渠道、来源和会话 ID 的联合索引键。"""
        return str(user_id), channel, source, str(chat_id) if chat_id not in (None, "") else None

    @classmethod
    def _keys_overlap(
            cls,
            left: Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]],
            right: Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]],
    ) -> bool:
        """判断两个输入会话键是否会争用同一条用户回复。"""
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
        """在持锁状态下淘汰过期会话并维护短期过期墓碑。"""
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
            channel: Optional[NotificationChannel],
            source: Optional[str],
            username: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
            prompt_id: Optional[str] = None,
            timeout_seconds: int = 120,
            payload: Optional[Any] = None,
            *,
            prompt_message_id: Optional[Union[str, int]] = None,
    ) -> PendingPluginInputInteraction:
        """创建插件输入会话并替换键范围重叠的旧会话。"""
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
                if channel == NotificationChannel.Telegram and normalized_chat_id and prompt_message_id not in (None, "")
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
            channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> Optional[PendingPluginInputInteraction]:
        """按用户和渠道上下文查询活动输入会话。"""
        with self._lock:
            self._cleanup_locked()
            request_id = self._find_request_id_locked(user_id, channel, source, chat_id)
            if request_id:
                return self._by_id.get(request_id)
            return None

    def pop_by_user(
            self,
            user_id: Union[str, int],
            channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> Optional[PendingPluginInputInteraction]:
        """取出并删除活动或刚过期的输入会话。"""
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
            channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None,
            chat_id: Optional[Union[str, int]] = None,
            *,
            reply_to_message_id: Optional[Union[str, int]] = None,
            bypass_reply_check: bool = False,
    ) -> Tuple[Optional[PendingPluginInputInteraction], Optional[str]]:
        """消费匹配回复的输入会话，并返回 active 或 expired 状态。"""
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
        """校验消息回复关系是否绑定到原始提示。"""
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
            channel: Optional[NotificationChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> Optional[str]:
        """在持锁状态下查找活动请求 ID。"""
        _, request_id = self._find_key_and_request_id_locked(user_id, channel, source, chat_id)
        return request_id

    def _find_key_and_request_id_locked(
            self,
            user_id: Union[str, int],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> Tuple[Optional[Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]]], Optional[str]]:
        """返回首个候选键及其活动请求 ID。"""
        for key in self._candidate_keys(user_id, channel, source, chat_id):
            request_id = self._by_user_channel.get(key)
            if request_id:
                return key, request_id
        return None, None

    def _find_expired_key_and_request_locked(
            self,
            user_id: Union[str, int],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> Tuple[Optional[Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]]],
               Optional[PendingPluginInputInteraction]]:
        """返回仍在宽限期内的过期会话及其索引键。"""
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
            channel: Optional[NotificationChannel],
            source: Optional[str],
            chat_id: Optional[Union[str, int]] = None,
    ) -> List[Tuple[str, Optional[NotificationChannel], Optional[str], Optional[str]]]:
        """按精确到宽松顺序生成输入会话候选键。"""
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
        """删除指定插件输入会话及其联合索引。"""
        with self._lock:
            request = self._by_id.pop(request_id, None)
            if request:
                self._by_user_channel.pop(
                    self._user_channel_source_key(request.user_id, request.channel, request.source, request.chat_id),
                    None,
                )

    def clear(self) -> None:
        """清空活动和过期的插件输入会话。"""
        with self._lock:
            self._by_id.clear()
            self._by_user_channel.clear()
            self._expired_by_user_channel.clear()


plugin_input_interaction_manager = PluginInputInteractionManager()


class PluginInputInteractionHandler:
    """消费插件申请接管的下一条用户文本输入。"""

    def __init__(
            self,
            messenger: MessageGateway,
            event_publisher: PluginEventPublisher,
    ):
        """保存消息投递接口和宿主注入的事件发布端口。"""
        self._messenger = messenger
        self._event_publisher = event_publisher

    def handle_text(
            self,
            *,
            context: InteractionContext,
            text: str,
            reply_to_message_id: Optional[Union[str, int]] = None,
            images=None,
            audio_refs=None,
            files=None,
            has_audio_input: bool = False,
    ) -> bool:
        """消费插件输入会话，并派发 MessageAction 事件。"""
        if not text or not text.strip() or images or audio_refs or files or has_audio_input:
            return False
        if text.startswith("CALLBACK:"):
            return False

        channel = context.channel
        source = context.source
        userid = context.user_id
        username = context.username
        original_chat_id = context.original_chat_id

        is_cancel_text = text.strip().lower() in {"取消", "退出", "q", "quit", "exit"}
        request, status = plugin_input_interaction_manager.consume_by_user(
            userid,
            channel,
            source,
            original_chat_id,
            reply_to_message_id=reply_to_message_id,
            bypass_reply_check=is_cancel_text,
        )
        if not request:
            return False

        if status == "expired":
            self._event_publisher.send_event(
                EventType.MessageAction,
                {
                    "plugin_id": request.plugin_id,
                    "__mp_target_plugin_id": request.plugin_id,
                    "text": f"plugin_input_expired|{request.request_id}",
                    "userid": userid,
                    "channel": channel,
                    "source": source,
                    "username": username,
                    "chat_id": original_chat_id,
                    "reply_to_message_id": reply_to_message_id,
                    "prompt_id": request.prompt_id,
                    "input_session_id": request.request_id,
                    "expired": True,
                    "payload": request.payload,
                },
            )
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="插件输入已超时，请重新发起操作。",
                    save_history=False,
                )
            )
            return not text.strip().startswith("/")

        if is_cancel_text:
            self._event_publisher.send_event(
                EventType.MessageAction,
                {
                    "plugin_id": request.plugin_id,
                    "__mp_target_plugin_id": request.plugin_id,
                    "text": f"plugin_input_cancel|{request.request_id}",
                    "userid": userid,
                    "channel": channel,
                    "source": source,
                    "username": username,
                    "chat_id": original_chat_id,
                    "reply_to_message_id": reply_to_message_id,
                    "prompt_id": request.prompt_id,
                    "input_session_id": request.request_id,
                    "cancelled": True,
                    "payload": request.payload,
                },
            )
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已取消插件输入",
                    save_history=False,
                )
            )
            return True

        self._event_publisher.send_event(
            EventType.MessageAction,
            {
                "plugin_id": request.plugin_id,
                "__mp_target_plugin_id": request.plugin_id,
                "text": f"plugin_input|{request.request_id}",
                "input_text": text,
                "userid": userid,
                "channel": channel,
                "source": source,
                "username": username,
                "chat_id": original_chat_id,
                "reply_to_message_id": reply_to_message_id,
                "prompt_id": request.prompt_id,
                "input_session_id": request.request_id,
                "payload": request.payload,
            },
        )
        return True

    @staticmethod
    def parse_callback(callback_data: str) -> Optional[Tuple[str, str]]:
        """解析插件按钮回调，格式错误时返回 None。"""
        if not callback_data.startswith("[PLUGIN]"):
            return None
        # 用 partition 避免缺少分隔符的回调抛异常
        plugin_id, separator, content = callback_data.partition("|")
        if not separator:
            return None
        return plugin_id.replace("[PLUGIN]", "", 1), content
