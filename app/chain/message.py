import asyncio
import base64
import math
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Dict, Union, List, Tuple
from urllib.parse import unquote, urlparse

from app.agent import ReplyMode, agent_manager, prompt_manager
from app.agent.llm import AgentCapabilityManager, LLMHelper
from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.site import SiteChain, site_interaction_manager
from app.chain.skills import SkillsChain, skills_interaction_manager
from app.chain.subscribe import SubscribeChain, subscribe_interaction_manager
from app.chain.transfer import TransferChain
from app.core.config import settings, global_vars
from app.core.context import MediaInfo, Context
from app.core.meta import MetaBase
from app.db.models import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.db.user_oper import UserOper
from app.helper.interaction import agent_interaction_manager, media_interaction_manager, PendingMediaInteraction
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import Notification, CommingMessage, NotExistMediaInfo
from app.schemas.message import ChannelCapabilityManager, ChannelCapability
from app.schemas.types import EventType, MessageChannel, MediaType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class MessageChain(ChainBase):
    """
    外来消息处理链
    """

    # 用户会话信息 {userid: (session_id, last_time)}
    _user_sessions: Dict[Union[str, int], tuple] = {}
    # 会话超时时间（分钟）
    _session_timeout_minutes: int = 24 * 60

    @staticmethod
    def _schedule_agent_session_clear(session_id: str, userid: Union[str, int]) -> None:
        """
        异步调度 Agent 会话清理，避免同步消息链阻塞在模型资源释放上。
        """
        if not session_id:
            return
        clear_task = None
        try:
            clear_task = agent_manager.clear_session(session_id=session_id, user_id=str(userid))
            asyncio.run_coroutine_threadsafe(
                clear_task,
                global_vars.loop,
            )
        except Exception as e:
            if clear_task:
                clear_task.close()
            logger.warning(f"调度清理智能体会话失败: {e}")

    def _cleanup_expired_user_sessions(self, current_time: datetime) -> None:
        """
        清理超过复用窗口的用户会话映射，并同步释放旧 Agent 实例。
        """
        timeout = timedelta(minutes=self._session_timeout_minutes)
        for userid, (session_id, last_time) in list(self._user_sessions.items()):
            if current_time - last_time <= timeout:
                continue
            self._user_sessions.pop(userid, None)
            self._schedule_agent_session_clear(session_id, userid)

    @dataclass
    class _ProcessingStatus:
        channel: MessageChannel
        source: str
        userid: Optional[Union[str, int]] = None
        message_id: Optional[Union[str, int]] = None
        chat_id: Optional[Union[str, int]] = None
        metadata: Optional[Dict[str, Any]] = None

        def to_dict(self) -> Dict[str, Any]:
            """转换为模块接口可安全传递的普通字典。"""
            return {
                "channel": self.channel.value,
                "source": self.source,
                "userid": self.userid,
                "message_id": self.message_id,
                "chat_id": self.chat_id,
                "metadata": self.metadata or {},
            }

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
        files = info.files
        if not text and not images and not audio_refs and not files:
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
            files=files,
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
            images: Optional[List[CommingMessage.MessageImage]] = None,
            audio_refs: Optional[List[str]] = None,
            files: Optional[List[CommingMessage.MessageAttachment]] = None,
    ) -> None:
        """
        识别消息内容，执行操作
        """
        images = CommingMessage.MessageImage.normalize_list(images)

        processing_status = None
        continues_async = False
        try:
            # 语音输入只用于转写为文本，不默认改变回复形式。
            has_audio_input = bool(audio_refs)
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

            if not text.startswith("CALLBACK:"):
                self._record_user_message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    text=text,
                )

            if not self._is_agent_message(
                    channel=channel,
                    userid=userid,
                    text=text,
                    images=images,
                    files=files,
                    has_audio_input=has_audio_input,
            ):
                processing_status = self._mark_message_processing_started(
                    channel=channel,
                    source=source,
                    userid=userid,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                    text=text,
                )

            continues_async = self._handle_message_core(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                text=text,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                images=images,
                audio_refs=audio_refs,
                files=files,
                has_audio_input=has_audio_input,
                processing_status=processing_status,
            )
        finally:
            if continues_async is not True:
                self._mark_message_processing_finished(
                    channel=channel,
                    source=source,
                    userid=userid,
                    status=processing_status,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                )

    def _handle_message_core(
            self,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            images: Optional[List[CommingMessage.MessageImage]] = None,
            audio_refs: Optional[List[str]] = None,
            files: Optional[List[CommingMessage.MessageAttachment]] = None,
            has_audio_input: bool = False,
            processing_status: Optional[_ProcessingStatus] = None,
    ) -> bool:
        """执行实际消息路由，便于统一包裹处理中状态。"""

        if text.startswith("CALLBACK:"):
            if ChannelCapabilityManager.supports_callbacks(channel):
                return self._handle_callback(
                    text=text,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                    processing_status=processing_status,
                )
            else:
                logger.warning(
                    "渠道 %s 不支持回调，但收到了回调消息：%s",
                    channel.value,
                    text,
                )
            return False

        if text.startswith("/") and not text.lower().startswith("/ai"):
            self.eventmanager.send_event(
                EventType.CommandExcute,
                {
                    "cmd": text,
                    "user": userid,
                    "channel": channel,
                    "source": source,
                    "processing_status": processing_status.to_dict()
                    if processing_status
                    else None,
                },
            )
            return bool(processing_status)

        if text.lower().startswith("/ai"):
            return self._handle_ai_message(
                text=text,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                images=images,
                files=files,
                has_audio_input=has_audio_input,
            )

        latest_slash_interaction = self._get_latest_slash_interaction(userid)
        if latest_slash_interaction == "sites":
            if SiteChain().handle_text_interaction(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    text=text,
            ):
                return False

        if latest_slash_interaction == "subscribes":
            if SubscribeChain().handle_text_interaction(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    text=text,
            ):
                return False

        if latest_slash_interaction == "skills":
            if SkillsChain().handle_text_interaction(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    text=text,
            ):
                return False

        if media_interaction_manager.get_by_user(userid):
            if MediaInteractionChain().handle_text_interaction(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    text=text,
            ):
                return False

        if (
                settings.AI_AGENT_ENABLE
                and (settings.AI_AGENT_GLOBAL or images or files or has_audio_input)
        ):
            return self._handle_ai_message(
                text=text,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                images=images,
                files=files,
                has_audio_input=has_audio_input,
            )

        if MediaInteractionChain().handle_text_interaction(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                text=text,
        ):
            return False

        self.eventmanager.send_event(
            EventType.UserMessage,
            {
                "text": text,
                "userid": userid,
                "channel": channel,
                "source": source,
            },
        )
        return False

    def _is_agent_message(
            self,
            channel: MessageChannel,
            userid: Union[str, int],
            text: str,
            images: Optional[List[CommingMessage.MessageImage]] = None,
            files: Optional[List[CommingMessage.MessageAttachment]] = None,
            has_audio_input: bool = False,
    ) -> bool:
        """
        判断本条消息是否会进入 Agent worker，由 Agent worker 管理 typing 生命周期。
        """
        if text.startswith("CALLBACK:"):
            return self._parse_agent_choice_callback(text[9:]) is not None
        if text.lower().startswith("/ai"):
            return True
        if text.startswith("/"):
            return False
        if not (
                settings.AI_AGENT_ENABLE
                and (settings.AI_AGENT_GLOBAL or images or files or has_audio_input)
        ):
            return False
        if self._get_latest_slash_interaction(userid):
            return False
        if media_interaction_manager.get_by_user(userid):
            return False
        return True

    def _mark_message_processing_started(
            self,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]],
            original_chat_id: Optional[Union[str, int]],
            text: str,
    ) -> Optional[_ProcessingStatus]:
        """为支持的渠道标记“消息正在处理”。"""
        status = self.start_message_processing_status(
            channel=channel,
            source=source,
            userid=userid,
            message_id=original_message_id,
            chat_id=original_chat_id,
            text=text,
        )
        if not status:
            return None

        metadata = status.get("metadata")
        return self._ProcessingStatus(
            channel=channel,
            source=source,
            userid=status.get("userid", userid),
            message_id=status.get("message_id", original_message_id),
            chat_id=status.get("chat_id", original_chat_id),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def _mark_message_processing_finished(
            self,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            status: Optional[_ProcessingStatus] = None,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[Union[str, int]] = None,
    ) -> None:
        """
        结束渠道侧“消息正在处理”状态。
        不同渠道的表现可能是 reaction、typing 等，消息链只负责调用通用模块接口。
        """
        if not status:
            return
        self.finish_message_processing_status(
            status=status.to_dict(),
            channel=channel,
            source=source,
            userid=userid,
            message_id=status.message_id or original_message_id,
            chat_id=status.chat_id or original_chat_id,
        )

    def _handle_callback(
            self,
            text: str,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            processing_status: Optional[_ProcessingStatus] = None,
    ) -> bool:
        """
        处理按钮回调
        """

        # 提取回调数据
        callback_data = text[9:]  # 去掉 "CALLBACK:" 前缀
        logger.info(f"处理按钮回调：{callback_data}")

        if self._handle_transfer_callback(
                callback_data=callback_data,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
        ):
            return False

        if SkillsChain().handle_callback_interaction(
                callback_data=callback_data,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
        ):
            return False

        if SiteChain().handle_callback_interaction(
                callback_data=callback_data,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
        ):
            return False

        if SubscribeChain().handle_callback_interaction(
                callback_data=callback_data,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
        ):
            return False

        if MediaInteractionChain().handle_callback_interaction(
                callback_data=callback_data,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
        ):
            return False

        if self._handle_agent_choice_callback(
                callback_data=callback_data,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
        ):
            return True

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
            return False

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
        return False

    @staticmethod
    def _get_latest_slash_interaction(userid: Union[str, int]) -> Optional[str]:
        """
        返回当前用户最近一次激活的 slash 交互类型。
        """
        candidates = []
        for name, manager in (
                ("sites", site_interaction_manager),
                ("subscribes", subscribe_interaction_manager),
                ("skills", skills_interaction_manager),
        ):
            request = manager.get_by_user(userid)
            if request:
                candidates.append((request.created_at, name))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    @staticmethod
    def _parse_transfer_callback(
            callback_data: str,
    ) -> Optional[tuple[str, int]]:
        """
        解析整理失败通知按钮回调。
        """
        for prefix, action in (
                ("transfer_retry_", "retry"),
                ("transfer_ai_retry_", "ai_retry"),
        ):
            if callback_data.startswith(prefix):
                history_id = callback_data.replace(prefix, "", 1)
                if history_id.isdigit():
                    return action, int(history_id)
        return None

    def _handle_transfer_callback(
            self,
            callback_data: str,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> bool:
        """
        处理整理失败通知中的重试类按钮。
        """
        callback = self._parse_transfer_callback(callback_data)
        if not callback:
            return False

        action, history_id = callback
        if action == "retry":
            self._retry_transfer_history(
                history_id=history_id,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
        else:
            self._take_over_transfer_history_by_ai(
                history_id=history_id,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
        return True

    @staticmethod
    def _parse_agent_choice_callback(
            callback_data: str,
    ) -> Optional[tuple[str, int]]:
        """
        解析 Agent 按钮选择回调。
        """
        if callback_data.startswith("agent_interaction:choice:"):
            try:
                _, _, request_id, option_index = callback_data.split(":", 3)
            except ValueError:
                return None
        elif callback_data.startswith("agent_choice:"):
            # 兼容旧格式，避免已发送的按钮失效
            try:
                _, request_id, option_index = callback_data.split(":", 2)
            except ValueError:
                return None
        else:
            return None
        if not request_id or not option_index.isdigit():
            return None
        return request_id, int(option_index)

    def _handle_agent_choice_callback(
            self,
            callback_data: str,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        将 Agent 按钮选择回传为同一会话中的下一条用户消息。
        """
        callback = self._parse_agent_choice_callback(callback_data)
        if not callback:
            return False

        request_id, option_index = callback
        resolved = agent_interaction_manager.resolve(
            request_id=request_id,
            option_index=option_index,
            user_id=str(userid),
        )
        if not resolved:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="该选择已失效，请重新发起选择",
                )
            )
            return False

        request, option = resolved
        selected_text = option.value
        self._update_interaction_message_feedback(
            channel=channel,
            source=source,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            title=request.title,
            prompt=request.prompt,
            selected_label=option.label,
        )
        self._bind_session_id(userid, request.session_id)
        self._record_user_message(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            text=selected_text,
        )
        return self._handle_ai_message(
            text=selected_text,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            session_id=request.session_id,
        )

    def _update_interaction_message_feedback(
            self,
            channel: MessageChannel,
            source: str,
            original_message_id: Optional[Union[str, int]],
            original_chat_id: Optional[str],
            prompt: str,
            selected_label: str,
            title: Optional[str] = None,
    ) -> None:
        """
        在用户点击交互按钮后，立即更新原消息，明确显示已选择的内容。
        """
        if not original_message_id or not original_chat_id:
            return

        lines = [prompt.strip()]
        if selected_label:
            lines.append(f"已选择：{selected_label}")
        feedback_text = "\n\n".join(line for line in lines if line)
        self.edit_message(
            channel=channel,
            source=source,
            message_id=original_message_id,
            chat_id=original_chat_id,
            title=title,
            text=feedback_text,
        )

    def _retry_transfer_history(
            self,
            history_id: int,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        立即重新整理一条失败的整理记录。
        """
        self.post_message(
            Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"开始重新整理记录 #{history_id} ...",
            )
        )

        state, errmsg = TransferChain().redo_transfer_history(history_id)
        if state:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"整理记录 #{history_id} 已重新整理",
                    link=settings.MP_DOMAIN("#/history"),
                )
            )
            return

        self.post_message(
            Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="重新整理失败",
                text=errmsg,
                link=settings.MP_DOMAIN("#/history"),
            )
        )

    def _take_over_transfer_history_by_ai(
            self,
            history_id: int,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        由智能助手接管一条失败的整理记录。
        """

        def __build_manual_redo_prompt(his: TransferHistory) -> str:
            """构建手动 AI 整理提示词。"""

            src_fileitem = his.src_fileitem or {}
            source_path = src_fileitem.get("path") if isinstance(src_fileitem, dict) else ""
            source_path = source_path or his.src or ""
            season_episode = f"{his.seasons or ''}{his.episodes or ''}".strip()
            # 键名必须与 System Tasks.yaml 中 manual_transfer_redo 模板的占位符一致
            template_context = {
                "history_id": his.id,
                "current_status": "success" if his.status else "failed",
                "recognized_title": his.title or "unknown",
                "media_type": his.type or "unknown",
                "category": his.category or "unknown",
                "year": his.year or "unknown",
                "season_episode": season_episode or "unknown",
                "source_path": source_path or "unknown",
                "source_storage": his.src_storage or "local",
                "destination_path": his.dest or "unknown",
                "destination_storage": his.dest_storage or "unknown",
                "transfer_mode": his.mode or "unknown",
                "tmdbid": his.tmdbid or "none",
                "doubanid": his.doubanid or "none",
                "error_message": his.errmsg or "none",
            }
            return prompt_manager.render_system_task_message(
                "manual_transfer_redo",
                template_context=template_context,
            )

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

        history = TransferHistoryOper().get(history_id)
        if not history:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="重新整理失败",
                    text=f"整理记录 #{history_id} 不存在",
                    link=settings.MP_DOMAIN("#/history"),
                )
            )
            return

        redo_prompt = __build_manual_redo_prompt(history)

        self.post_message(
            Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"已将整理记录 #{history_id} 交给智能助手处理",
                text="处理完成后会在这里回复结果。",
                link=settings.MP_DOMAIN("#/history"),
            )
        )

        async def _run_ai_takeover():
            final_output = ""

            def _capture_output(text_output: str):
                nonlocal final_output
                final_output = text_output or ""

            try:
                await agent_manager.run_background_prompt(
                    message=redo_prompt,
                    session_prefix=f"__agent_manual_redo_{history_id}",
                    output_callback=_capture_output,
                    reply_mode=ReplyMode.CAPTURE_ONLY,
                    persist_output_message=False,
                    allow_message_tools=False,
                )
                await self.async_post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="智能助手整理完成",
                        text=final_output.strip()
                             or f"整理记录 #{history_id} 已由智能助手处理完成。",
                        link=settings.MP_DOMAIN("#/history"),
                    )
                )
            except Exception as e:
                await self.async_post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="智能助手整理失败",
                        text=str(e),
                        link=settings.MP_DOMAIN("#/history"),
                    )
                )

        asyncio.run_coroutine_threadsafe(_run_ai_takeover(), global_vars.loop)

    def _get_or_create_session_id(self, userid: Union[str, int]) -> str:
        """
        获取或创建会话ID
        如果用户上次会话在15分钟内，则复用相同的会话ID；否则创建新的会话ID
        """
        current_time = datetime.now()
        self._cleanup_expired_user_sessions(current_time)

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

    def _bind_session_id(self, userid: Union[str, int], session_id: str) -> None:
        """
        将用户会话绑定到指定的 session_id，并刷新最后活动时间。
        """
        old_session = self._user_sessions.get(userid)
        if old_session and old_session[0] != session_id:
            self._schedule_agent_session_clear(old_session[0], userid)
        self._user_sessions[userid] = (session_id, datetime.now())

    def _record_user_message(
            self,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
    ) -> None:
        """
        保存一条用户消息到消息历史与数据库。
        """
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
            clear_task = None
            try:
                clear_task = agent_manager.clear_session(
                    session_id=session_id, user_id=str(userid)
                )
                asyncio.run_coroutine_threadsafe(
                    clear_task,
                    global_vars.loop,
                )
            except Exception as e:
                if clear_task:
                    clear_task.close()
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

    @staticmethod
    def _format_token_count(value: Optional[int]) -> str:
        return f"{value:,}" if value is not None else "未知"

    @classmethod
    def _format_session_status_text(cls, status: Dict[str, Any]) -> str:
        context_window_tokens = status.get("context_window_tokens")
        last_input_tokens = status.get("last_input_tokens")
        if context_window_tokens and status.get("model_call_count"):
            context_ratio = status.get("last_context_usage_ratio")
            if context_ratio is None and last_input_tokens is not None:
                context_ratio = last_input_tokens / context_window_tokens
            context_usage_text = (
                f"{cls._format_token_count(last_input_tokens)} / "
                f"{cls._format_token_count(context_window_tokens)} "
                f"({context_ratio * 100:.2f}%)"
                if context_ratio is not None
                else f"{cls._format_token_count(last_input_tokens)} / "
                     f"{cls._format_token_count(context_window_tokens)}"
            )
        else:
            context_usage_text = "暂无模型调用数据"

        lines = [
            f"会话ID: {status.get('session_id') or '未知'}",
            f"执行状态: {'运行中' if status.get('is_processing') else '空闲'}",
            f"当前模型: {status.get('model') or '未知'}",
            f"上下文窗口: {cls._format_token_count(context_window_tokens)} tokens",
            f"最近一次上下文占用: {context_usage_text}",
            f"最近一次 tokens: 输入 {cls._format_token_count(status.get('last_input_tokens'))} / 输出 {cls._format_token_count(status.get('last_output_tokens'))} / 总计 {cls._format_token_count(status.get('last_total_tokens'))}",
            f"当前会话累计 tokens: 输入 {cls._format_token_count(status.get('total_input_tokens'))} / 输出 {cls._format_token_count(status.get('total_output_tokens'))} / 总计 {cls._format_token_count(status.get('total_tokens'))}",
            f"模型调用次数: {status.get('model_call_count', 0)}",
            f"排队消息数: {status.get('pending_messages', 0)}",
            f"最后更新: {status.get('last_updated_at') or '暂无'}",
        ]
        return "\n".join(lines)

    def remote_session_status(
            self,
            channel: MessageChannel,
            userid: Union[str, int],
            source: Optional[str] = None,
    ):
        """查询当前用户的智能体会话状态。"""
        session_info = self._user_sessions.get(userid)
        if not session_info:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    title="您当前没有活跃的智能体会话",
                    userid=userid,
                )
            )
            return

        session_id, _ = session_info
        status = agent_manager.get_session_status(session_id=session_id)
        self.post_message(
            Notification(
                channel=channel,
                source=source,
                title="当前智能体会话状态",
                text=self._format_session_status_text(status),
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
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            images: Optional[List[CommingMessage.MessageImage]] = None,
            files: Optional[List[CommingMessage.MessageAttachment]] = None,
            session_id: Optional[str] = None,
            has_audio_input: bool = False,
    ) -> bool:
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
                return False

            images = CommingMessage.MessageImage.normalize_list(images)

            # 提取用户消息
            if text.lower().startswith("/ai"):
                user_message = text[3:].strip()  # 移除 "/ai" 前缀（大小写不敏感）
            else:
                user_message = text.strip()  # 按原消息处理

            if not user_message and not images and not files:
                self.post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="请输入您的问题或需求",
                    )
                )
                return False

            # 生成或复用会话ID
            session_id = session_id or self._get_or_create_session_id(userid)
            self._bind_session_id(userid, session_id)

            # 将可直接输入给 LLM 的附件统一转换为 data URL
            original_images = images
            all_files = list(files or [])
            if images and LLMHelper.supports_image_input():
                images = self._download_attachments_to_data_urls(
                    images, channel, source
                )
                if original_images and not images and not user_message and not files:
                    self.post_message(
                        Notification(
                            channel=channel,
                            source=source,
                            userid=userid,
                            username=username,
                            title="附件读取失败，请稍后重试",
                        )
                    )
                    return False
            elif images:
                image_attachments = self._build_image_attachments(images)
                if (
                        original_images
                        and not image_attachments
                        and not user_message
                        and not files
                ):
                    self.post_message(
                        Notification(
                            channel=channel,
                            source=source,
                            userid=userid,
                            username=username,
                            title="附件读取失败，请稍后重试",
                        )
                    )
                    return False
                all_files.extend(image_attachments)
                images = None

            prepared_files = self._prepare_agent_files(
                session_id=session_id,
                files=all_files,
                channel=channel,
                source=source,
            )
            if all_files and not prepared_files and not user_message and not images:
                self.post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="文件读取失败，请稍后重试",
                    )
                )
                return False

            process_kwargs = {
                "session_id": session_id,
                "user_id": str(userid),
                "message": user_message,
                "images": images,
                "files": prepared_files,
                "channel": channel.value if channel else None,
                "source": source,
                "username": username,
                "original_message_id": str(original_message_id)
                if original_message_id
                else None,
                "original_chat_id": original_chat_id,
            }
            if has_audio_input:
                process_kwargs["has_audio_input"] = True
            # 在事件循环中处理
            asyncio.run_coroutine_threadsafe(
                agent_manager.process_message(**process_kwargs),
                global_vars.loop,
            )
            return True

        except Exception as e:
            logger.error(f"处理AI智能体消息失败: {e}")
            self.messagehelper.put(
                f"AI智能体处理失败: {str(e)}", role="system", title="MoviePilot助手"
            )
            return False

    def _transcribe_audio_refs(
            self, audio_refs: List[str], channel: MessageChannel, source: str
    ) -> Optional[str]:
        """
        下载并识别语音消息，仅处理当前已接入的渠道。
        """
        if not audio_refs:
            return None
        if not AgentCapabilityManager.is_audio_input_available():
            logger.warning("音频输入能力未配置或未启用，跳过语音识别")
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
                        "download_wechat_media_bytes",
                        media_ref=audio_ref,
                        source=source,
                    )
                    filename = "input.amr"
                elif audio_ref.startswith("wxclaw://voice/"):
                    content = self.run_module(
                        "download_wechat_media_bytes",
                        media_ref=audio_ref,
                        source=source,
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.amr"
                    )
                elif audio_ref.startswith("slack://file/"):
                    content = self.run_module(
                        "download_slack_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("discord://file/"):
                    content = self.run_module(
                        "download_discord_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("qq://file/"):
                    content = self.run_module(
                        "download_qq_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("vocechat://file/"):
                    content = self.run_module(
                        "download_vocechat_file_bytes",
                        file_ref=audio_ref,
                        source=source,
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("synology://file/"):
                    content = self.run_module(
                        "download_synologychat_file_bytes",
                        file_ref=audio_ref,
                        source=source,
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("wxbot://voice"):
                    continue
                elif audio_ref.startswith("feishu://file/"):
                    content = self.run_module(
                        "download_feishu_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.opus"
                    )
                elif audio_ref.startswith("http"):
                    resp = RequestUtils(timeout=30).get_res(audio_ref)
                    content = resp.content if resp and resp.content else None
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
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

                transcript = AgentCapabilityManager.transcribe_audio(
                    content=content, filename=filename
                )
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

    def _download_attachments_to_data_urls(
            self,
            attachments: List[CommingMessage.MessageImage],
            channel: MessageChannel,
            source: str,
    ) -> Optional[List[str]]:
        """
        下载可直接提供给 LLM 的附件内容，并统一转换为 data URL。
        """
        normalized_attachments = CommingMessage.MessageImage.normalize_list(attachments) or []
        if not normalized_attachments:
            return None
        data_urls = []
        for attachment in normalized_attachments:
            attachment_ref = attachment.ref
            try:
                before_count = len(data_urls)
                if attachment_ref.startswith("data:"):
                    data_urls.append(attachment_ref)
                elif attachment_ref.startswith("tg://file_id/"):
                    file_id = attachment_ref.replace("tg://file_id/", "")
                    base64_data = self.run_module(
                        "download_telegram_file_to_base64",
                        file_id=file_id,
                        source=source,
                    )
                    if base64_data:
                        data_urls.append(f"data:image/jpeg;base64,{base64_data}")
                elif attachment_ref.startswith(
                        "wxwork://media_id/"
                ) or attachment_ref.startswith(
                    "wxbot://image/"
                ) or attachment_ref.startswith(
                    "wxclaw://image/"
                ):
                    data_url = self.run_module(
                        "download_wechat_image_to_data_url",
                        image_ref=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif attachment_ref.startswith("feishu://image/"):
                    data_url = self.run_module(
                        "download_feishu_image_to_data_url",
                        image_ref=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif channel == MessageChannel.Slack:
                    data_url = self.run_module(
                        "download_slack_file_to_data_url",
                        file_url=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif attachment_ref.startswith("vocechat://file/"):
                    data_url = self.run_module(
                        "download_vocechat_image_to_data_url",
                        image_ref=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif attachment_ref.startswith("http"):
                    resp = RequestUtils(timeout=30).get_res(attachment_ref)
                    if resp and resp.content:
                        base64_data = base64.b64encode(resp.content).decode()
                        mime_type = resp.headers.get("Content-Type", "image/jpeg")
                        data_urls.append(f"data:{mime_type};base64,{base64_data}")
                else:
                    logger.debug(
                        "暂不支持直接转换为 data URL 的附件引用: channel=%s, source=%s, ref=%s",
                        channel.value if channel else None,
                        source,
                        attachment_ref,
                    )
                    continue

                if len(data_urls) > before_count:
                    logger.info(
                        "附件读取成功并已转换为 data URL: channel=%s, source=%s, ref=%s, mime_type=%s",
                        channel.value if channel else None,
                        source,
                        attachment_ref,
                        attachment.mime_type,
                    )
            except Exception as err:
                logger.error(
                    "附件读取失败，无法转换为 data URL: channel=%s, source=%s, ref=%s, error=%s",
                    channel.value if channel else None,
                    source,
                    attachment_ref,
                    err,
                )
        return data_urls if data_urls else None

    def _build_image_attachments(
            self, images: List[CommingMessage.MessageImage]
    ) -> List[CommingMessage.MessageAttachment]:
        """
        将图片引用转换为附件描述，以便按文件方式交给 Agent 处理。
        """
        images = CommingMessage.MessageImage.normalize_list(images)
        if not images:
            return []

        attachments = []
        for index, image in enumerate(images, start=1):
            image_ref = image.ref
            if not image_ref:
                continue
            name = image.name or self._guess_image_attachment_name(image_ref, index)
            mime_type = image.mime_type or self._guess_image_mime_type(image_ref, name)
            attachments.append(
                CommingMessage.MessageAttachment(
                    ref=image_ref,
                    name=name,
                    mime_type=mime_type,
                    size=image.size,
                )
            )
        return attachments

    def _prepare_agent_files(
            self,
            session_id: str,
            files: Optional[List[CommingMessage.MessageAttachment]],
            channel: MessageChannel,
            source: str,
    ) -> Optional[List[dict]]:
        """
        下载用户上传的附件，落盘到临时目录，并生成 Agent 可消费的文件描述。
        """
        if not files:
            return None

        prepared_files = []
        for attachment in files:
            payload = {
                "name": attachment.name,
                "mime_type": attachment.mime_type,
                "size": attachment.size,
                "ref": attachment.ref,
                "status": "download_failed",
            }
            try:
                content = self._download_message_file_bytes(
                    file_ref=attachment.ref,
                    channel=channel,
                    source=source,
                )
                if not content:
                    prepared_files.append(payload)
                    continue

                local_path = self._save_agent_attachment(
                    session_id=session_id,
                    filename=attachment.name,
                    content=content,
                    mime_type=attachment.mime_type,
                )
                payload.update(
                    {
                        "local_path": str(local_path),
                        "status": "ready",
                    }
                )
            except Exception as err:
                logger.error(f"准备附件上下文失败: {attachment.ref}, error: {err}")
                payload["error"] = str(err)
            prepared_files.append(payload)

        return prepared_files or None

    def _download_message_file_bytes(
            self, file_ref: str, channel: MessageChannel, source: str
    ) -> Optional[bytes]:
        """
        下载消息附件的原始字节内容。
        """
        if not file_ref:
            return None
        if file_ref.startswith("data:"):
            return self._decode_data_url_bytes(file_ref)
        if file_ref.startswith("tg://file_id/"):
            file_id = file_ref.replace("tg://file_id/", "", 1)
            return self.run_module(
                "download_telegram_file_bytes", file_id=file_id, source=source
            )
        if file_ref.startswith("tg://document_file_id/"):
            file_id = file_ref.replace("tg://document_file_id/", "", 1)
            return self.run_module(
                "download_telegram_file_bytes", file_id=file_id, source=source
            )
        if file_ref.startswith("wxwork://media_id/"):
            return self.run_module(
                "download_wechat_media_bytes", media_ref=file_ref, source=source
            )
        if file_ref.startswith("wxwork://file_media_id/"):
            return self.run_module(
                "download_wechat_media_bytes", media_ref=file_ref, source=source
            )
        if file_ref.startswith("wxbot://image/"):
            data_url = self.run_module(
                "download_wechat_image_to_data_url", image_ref=file_ref, source=source
            )
            return self._decode_data_url_bytes(data_url) if data_url else None
        if file_ref.startswith("wxclaw://image/"):
            data_url = self.run_module(
                "download_wechat_image_to_data_url", image_ref=file_ref, source=source
            )
            return self._decode_data_url_bytes(data_url) if data_url else None
        if file_ref.startswith("wxbot://file/"):
            file_url = unquote(file_ref.replace("wxbot://file/", "", 1))
            resp = RequestUtils(timeout=30).get_res(file_url)
            return resp.content if resp and resp.content else None
        if file_ref.startswith("wxclaw://file/") or file_ref.startswith("wxclaw://voice/"):
            return self.run_module(
                "download_wechat_media_bytes", media_ref=file_ref, source=source
            )
        if file_ref.startswith("feishu://file/"):
            return self.run_module(
                "download_feishu_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("slack://file/"):
            return self.run_module(
                "download_slack_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("discord://file/"):
            return self.run_module(
                "download_discord_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("qq://file/"):
            return self.run_module(
                "download_qq_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("vocechat://file/"):
            return self.run_module(
                "download_vocechat_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("synology://file/"):
            return self.run_module(
                "download_synologychat_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("http"):
            if channel == MessageChannel.Slack:
                data_url = self.run_module(
                    "download_slack_file_to_data_url", file_url=file_ref, source=source
                )
                return self._decode_data_url_bytes(data_url) if data_url else None
            resp = RequestUtils(timeout=30).get_res(file_ref)
            return resp.content if resp and resp.content else None
        logger.debug(
            "暂不支持的附件引用: channel=%s, source=%s, ref=%s",
            channel.value if channel else None,
            source,
            file_ref,
        )
        return None

    def _save_agent_attachment(
            self,
            session_id: str,
            filename: Optional[str],
            content: bytes,
            mime_type: Optional[str] = None,
    ) -> Path:
        """
        将用户上传文件写入临时目录，并返回本地路径。
        """
        safe_name = self._sanitize_attachment_name(filename, mime_type)
        base_dir = settings.TEMP_PATH / "agent_uploads" / session_id
        base_dir.mkdir(parents=True, exist_ok=True)

        file_id = uuid.uuid4().hex[:8]
        local_path = base_dir / f"{file_id}_{safe_name}"
        local_path.write_bytes(content or b"")
        return local_path

    @staticmethod
    def _sanitize_attachment_name(
            filename: Optional[str], mime_type: Optional[str] = None
    ) -> str:
        """
        规范化附件文件名，避免路径穿越和非法字符。
        """
        name = Path(filename or "attachment").name
        name = re.sub(r"[^\w.\-]+", "_", name, flags=re.ASCII).strip("._")
        if not name:
            name = "attachment"
        if "." not in name:
            mime = (mime_type or "").split(";", 1)[0].strip().lower()
            default_ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "image/bmp": ".bmp",
                "application/json": ".json",
                "text/plain": ".txt",
                "text/markdown": ".md",
                "text/csv": ".csv",
            }.get(mime)
            if default_ext:
                name = f"{name}{default_ext}"
        return name

    @staticmethod
    def _guess_image_attachment_name(image_ref: str, index: int) -> str:
        """
        根据图片引用推测附件名。
        """
        if not image_ref:
            return f"image_{index}.jpg"
        if image_ref.startswith("data:"):
            mime_part = image_ref[5:].split(";", 1)[0].strip().lower()
            ext = mimetypes.guess_extension(mime_part) or ".jpg"
            return f"image_{index}{ext}"

        parsed = urlparse(unquote(image_ref))
        name = Path(parsed.path).name if parsed.path else ""
        if name and "." in name:
            return name
        return f"image_{index}.jpg"

    @staticmethod
    def _guess_image_mime_type(image_ref: str, filename: Optional[str]) -> str:
        """
        根据图片引用或文件名推测 MIME 类型。
        """
        if image_ref and image_ref.startswith("data:"):
            mime = image_ref[5:].split(";", 1)[0].strip().lower()
            return mime or "image/jpeg"
        guessed, _ = mimetypes.guess_type(filename or "")
        if guessed and guessed.startswith("image/"):
            return guessed
        return "image/jpeg"

    @staticmethod
    def _decode_data_url_bytes(data_url: Optional[str]) -> Optional[bytes]:
        """
        将 data URL 解码为原始字节。
        """
        if not data_url or not data_url.startswith("data:"):
            return None
        try:
            _, payload = data_url.split(",", 1)
        except ValueError:
            return None
        try:
            return base64.b64decode(payload)
        except Exception as e:
            logger.error(e)
            return None


class MediaInteractionChain(ChainBase):
    """
    处理媒体搜索、订阅、资源选择和翻页等交互流程。
    """

    _button_page_size = 8
    _text_page_size = 8

    @staticmethod
    def has_pending_interaction(user_id: Union[str, int]) -> bool:
        """
        判断用户当前是否存在未结束的媒体交互。
        """
        return media_interaction_manager.get_by_user(user_id) is not None

    @staticmethod
    def _get_noexits_info(
            meta: MetaBase, mediainfo: MediaInfo
    ) -> Dict[Union[int, str], Dict[int, NotExistMediaInfo]]:
        """
        构造媒体缺失集信息，用于全量重搜或自动下载补全集数。
        """
        if mediainfo.type == MediaType.TV:
            if not mediainfo.seasons:
                mediainfo = MediaChain().recognize_media(
                    mtype=mediainfo.type,
                    tmdbid=mediainfo.tmdb_id,
                    doubanid=mediainfo.douban_id,
                    cache=False,
                )
                if not mediainfo:
                    logger.warn("媒体信息识别失败，无法补充季集信息")
                    return {}
                if not mediainfo.seasons:
                    logger.warn(
                        "媒体信息中没有季集信息，标题：%s，tmdbid：%s，doubanid：%s",
                        mediainfo.title,
                        mediainfo.tmdb_id,
                        mediainfo.douban_id,
                    )
                    return {}

            mediakey = mediainfo.tmdb_id or mediainfo.douban_id
            no_exists = {mediakey: {}}
            if meta.begin_season:
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

    def handle_callback_interaction(
            self,
            callback_data: str,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        处理按钮回调，并将当前视图刷新到原消息上。
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False

        request_id, action, index = parsed
        if request_id:
            request = media_interaction_manager.get_by_id(request_id, userid)
        else:
            request = media_interaction_manager.get_by_user(userid)

        if not request:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="交互已失效，请重新搜索或订阅",
                )
            )
            return True

        request.channel = channel
        request.source = source
        request.username = username

        if action == "page-prev":
            if request.page <= 0:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是第一页了！",
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
            )
            return True

        return False

    def handle_text_interaction(
            self,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
    ) -> bool:
        """
        处理文本式交互。

        有会话时优先处理数字选择和翻页；无会话时负责识别搜索/订阅类入口。
        """
        request = media_interaction_manager.get_by_user(userid)
        normalized = (text or "").strip()
        lowered = normalized.lower()

        if request and lowered in {"退出", "关闭", "q", "quit", "exit"}:
            media_interaction_manager.remove(request.request_id)
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="媒体交互已结束",
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
                )
                return True
            request.channel = channel
            request.source = source
            request.username = username
            index = int(normalized)
            if request.phase == "torrent":
                self._handle_torrent_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
            else:
                self._handle_media_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
            return True

        if lowered in {"p", "prev", "上一页"}:
            if not request:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
                return True
            if request.page <= 0:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是第一页了！",
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
                )
                return True
            if not self._has_next_page(request):
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是最后一页了！",
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
        )
        return True

    @staticmethod
    def _resolve_action(text: str) -> Tuple[Optional[str], str]:
        """
        将用户输入归类为搜索、订阅或普通聊天。
        """
        if text.startswith("订阅"):
            return "Subscribe", re.sub(r"订阅[:：\s]*", "", text)
        if text.startswith("洗版"):
            return "ReSubscribe", re.sub(r"洗版[:：\s]*", "", text)
        if text.startswith("搜索") or text.startswith("下载"):
            return "ReSearch", re.sub(r"(搜索|下载)[:：\s]*", "", text)
        if StringUtils.is_link(text):
            return None, text
        if not StringUtils.is_media_title_like(text):
            return None, text
        return "Search", text

    def _start_media_interaction(
            self,
            action: str,
            content: str,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        根据用户输入搜索媒体，并进入媒体选择阶段。
        """
        meta, medias = MediaChain().search(content)
        if not meta.name:
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="无法识别输入内容！",
            )
            return
        if not medias:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{meta.name} 没有找到对应的媒体信息！",
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
            title=meta.name,
            meta=meta,
            items=medias,
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
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        处理媒体选择阶段的序号输入。
        """
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
            )
            return

        mediainfo: MediaInfo = page_items[page_index - 1]
        request.current_media = mediainfo

        if request.action in {"Search", "ReSearch"}:
            self._search_media_resources(
                request=request,
                mediainfo=mediainfo,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return

        if request.action in {"Subscribe", "ReSubscribe"}:
            self._subscribe_media(
                request=request,
                mediainfo=mediainfo,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )

    def _search_media_resources(
            self,
            request: PendingMediaInteraction,
            mediainfo: MediaInfo,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        根据已选媒体搜索资源，并切换到资源选择阶段。
        """
        exist_flag, no_exists = DownloadChain().get_no_exists_info(
            meta=request.meta,
            mediainfo=mediainfo,
        )
        if exist_flag and request.action == "Search":
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"【{mediainfo.title_year}{request.meta.sea} 媒体库中已存在，如需重新下载请发送：搜索 名称 或 下载 名称】",
                )
            )
            return
        if exist_flag:
            no_exists = self._get_noexits_info(request.meta, mediainfo)

        messages = self._build_no_exists_messages(
            mediainfo=mediainfo,
            no_exists=no_exists,
            show_missing_only=request.action == "Search",
        )
        if messages:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{mediainfo.title_year}：\n" + "\n".join(messages),
                )
            )

        logger.info("开始搜索 %s ...", mediainfo.title_year)
        self.post_message(
            Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"开始搜索 {mediainfo.type.value} {mediainfo.title_year} ...",
            )
        )

        contexts = SearchChain().process(mediainfo=mediainfo, no_exists=no_exists)
        if not contexts:
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{mediainfo.title}{request.meta.sea} 未搜索到需要的资源！",
                )
            )
            return

        contexts = TorrentHelper().sort_torrents(contexts)
        if self._should_auto_download(userid):
            logger.info("用户 %s 在自动下载用户中，开始自动择优下载 ...", userid)
            self._auto_download(
                request=request,
                cache_list=contexts,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                no_exists=no_exists,
            )
            return

        request.phase = "torrent"
        request.page = 0
        request.title = mediainfo.title
        request.items = list(contexts)
        self._render_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _subscribe_media(
            self,
            request: PendingMediaInteraction,
            mediainfo: MediaInfo,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        根据已选媒体创建订阅或洗版订阅。
        """
        best_version = request.action == "ReSubscribe"
        if not best_version:
            exist_flag, _ = DownloadChain().get_no_exists_info(
                meta=request.meta,
                mediainfo=mediainfo,
            )
            if exist_flag:
                self.post_message(
                    Notification(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title=f"【{mediainfo.title_year}{request.meta.sea} 媒体库中已存在，如需洗版请发送：洗版 XXX】",
                    )
                )
                return

        mp_name = (
            UserOper().get_name(**{f"{channel.name.lower()}_userid": userid})
            if channel
            else None
        )
        SubscribeChain().add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=request.meta.begin_season,
            channel=channel,
            source=source,
            userid=userid,
            username=mp_name or username,
            best_version=best_version,
        )

    def _handle_torrent_selection(
            self,
            request: PendingMediaInteraction,
            page_index: Optional[int],
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        处理资源选择阶段的下载操作。
        """
        if request.phase != "torrent":
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return

        if page_index == 0:
            self._auto_download(
                request=request,
                cache_list=request.items,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
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
            )
            return

        context: Context = page_items[page_index - 1]
        DownloadChain().download_single(
            context,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )

    def _auto_download(
            self,
            request: PendingMediaInteraction,
            cache_list: List[Context],
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
    ) -> None:
        """
        自动择优下载当前资源列表，并在未完成时补建订阅。
        """
        downloadchain = DownloadChain()
        if no_exists is None:
            exist_flag, no_exists = downloadchain.get_no_exists_info(
                meta=request.meta,
                mediainfo=request.current_media,
            )
            if exist_flag:
                no_exists = self._get_noexits_info(request.meta, request.current_media)

        downloads, lefts = downloadchain.batch_download(
            contexts=cache_list,
            no_exists=no_exists,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )
        if downloads and not lefts:
            logger.info("%s 下载完成", request.current_media.title_year)
            return

        logger.info("%s 未下载未完整，添加订阅 ...", request.current_media.title_year)
        if downloads and request.current_media.type == MediaType.TV:
            note = [
                download.meta_info.begin_episode
                for download in downloads
                if download.meta_info.begin_episode
            ]
        else:
            note = None

        mp_name = (
            UserOper().get_name(**{f"{channel.name.lower()}_userid": userid})
            if channel
            else None
        )
        SubscribeChain().add(
            title=request.current_media.title,
            year=request.current_media.year,
            mtype=request.current_media.type,
            tmdbid=request.current_media.tmdb_id,
            season=request.meta.begin_season,
            channel=channel,
            source=source,
            userid=userid,
            username=mp_name or username,
            state="R",
            note=note,
        )

    def _render_interaction(
            self,
            request: PendingMediaInteraction,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        按当前阶段渲染媒体列表或资源列表。
        """
        if request.phase == "torrent":
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
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新媒体选择列表。
        """
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
            Notification(
                channel=channel,
                source=source,
                title=title,
                userid=userid,
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            ),
            medias=page_items,
        )

    def _post_torrents_message(
            self,
            request: PendingMediaInteraction,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新资源选择列表。
        """
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
            if total > self._page_size(channel):
                title = f"【{request.title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择 p: 上一页 n: 下一页）"
            else:
                title = f"【{request.title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择）"
            buttons = None

        self.post_torrents_message(
            Notification(
                channel=channel,
                source=source,
                title=title,
                userid=userid,
                link=settings.MP_DOMAIN("#/resource"),
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            ),
            torrents=page_items,
        )

    def _create_media_buttons(
            self,
            channel: MessageChannel,
            request: PendingMediaInteraction,
            items: List[MediaInfo],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为媒体列表生成选择和翻页按钮。
        """
        buttons: List[List[Dict[str, str]]] = []
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, media in enumerate(items, start=1):
            if max_per_row == 1:
                button_text = f"{index}. {media.title_year}"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:select:{index}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:select:{index}",
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
            channel: MessageChannel,
            request: PendingMediaInteraction,
            items: List[Context],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为资源列表生成下载和翻页按钮。
        """
        buttons: List[List[Dict[str, str]]] = [
            [
                {
                    "text": "🤖 自动选择下载",
                    "callback_data": f"media:{request.request_id}:download:0",
                }
            ]
        ]
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, context in enumerate(items, start=1):
            torrent = context.torrent_info
            if max_per_row == 1:
                button_text = f"{index}. {torrent.site_name} - {torrent.seeders}↑"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:download:{index}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:download:{index}",
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
            items=request.items,
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        return page < total_pages - 1

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

    def _page_size(self, channel: Optional[MessageChannel]) -> int:
        """
        按渠道交互能力选择分页大小。
        """
        return (
            self._button_page_size
            if self._supports_interactive_buttons(channel)
            else self._text_page_size
        )

    @staticmethod
    def _supports_interactive_buttons(channel: Optional[MessageChannel]) -> bool:
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
            mediainfo: MediaInfo,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]],
            show_missing_only: bool,
    ) -> List[str]:
        """
        将缺失集信息转换为可发送的文案。
        """
        if not no_exists:
            return []
        mediakey = mediainfo.tmdb_id or mediainfo.douban_id
        season_map = no_exists.get(mediakey) or {}
        if show_missing_only:
            return [
                f"第 {sea} 季缺失 {StringUtils.str_series(no_exist.episodes) if no_exist.episodes else no_exist.total_episode} 集"
                for sea, no_exist in season_map.items()
            ]
        return [
            f"第 {sea} 季总 {no_exist.total_episode} 集"
            for sea, no_exist in season_map.items()
        ]

    @staticmethod
    def _should_auto_download(userid: Union[str, int]) -> bool:
        """
        判断当前用户是否命中自动下载名单。
        """
        auto_download_user = settings.AUTO_DOWNLOAD_USER
        return bool(
            auto_download_user
            and (
                    auto_download_user == "all"
                    or any(userid == user for user in auto_download_user.split(","))
            )
        )

    def _post_invalid_input(
            self,
            channel: MessageChannel,
            source: str,
            userid: Union[str, int],
            username: Optional[str],
            title: str = "输入有误！",
    ) -> None:
        """
        发送统一的非法输入提示。
        """
        self.post_message(
            Notification(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=title,
            )
        )
