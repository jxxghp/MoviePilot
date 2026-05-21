"""生成反馈 Issue 预览并要求用户按钮确认。"""

from __future__ import annotations

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool, ToolChain
from app.agent.tools.impl.feedback_issue_state import (
    FEEDBACK_CONFIRM_VALUE_PREFIX,
    build_feedback_draft_hash,
    feedback_issue_state_store,
)
from app.agent.tools.impl.submit_feedback_issue import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ISSUE_TYPES,
    MAX_TITLE_CHARS,
    SubmitFeedbackIssueTool,
)
from app.helper.interaction import AgentInteractionOption, agent_interaction_manager
from app.log import logger
from app.schemas import Notification, NotificationType
from app.schemas.message import ChannelCapabilityManager
from app.schemas.types import MessageChannel


class PrepareFeedbackIssueInput(BaseModel):
    """反馈 Issue 预览确认工具输入。"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why a feedback issue preview is being prepared",
    )
    title: str = Field(..., description="Issue title following `[错误报告]: <短描述>`")
    version: str = Field(..., description="Current MoviePilot version")
    environment: str = Field(..., description="Exactly Docker or Windows")
    issue_type: str = Field(..., description="主程序运行问题 / 插件问题 / 其他问题")
    description: str = Field(..., description="Structured issue description")
    original_user_request: str = Field(..., description="Verbatim original user request")
    diagnostics_id: str = Field(
        ...,
        description=(
            "diagnostics_id returned by collect_feedback_diagnostics. Logs are loaded from "
            "the server-side state store via this id — do NOT pass the log text itself."
        ),
    )


class PrepareFeedbackIssueTool(MoviePilotTool):
    """发送 Issue 草稿预览，并创建只能由按钮回调确认的 token。"""

    name: str = "prepare_feedback_issue"
    sends_message: bool = True
    description: str = (
        "Prepare a feedback issue preview and ask the user to confirm via buttons. "
        "Must be called after collect_feedback_diagnostics and before submit_feedback_issue. "
        "Returns a confirmation_token, but submit_feedback_issue will only accept it after "
        "the user actually clicks the confirmation button."
    )
    args_schema: Type[BaseModel] = PrepareFeedbackIssueInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """侧边消息：告知用户正在生成提交预览。"""
        return "生成问题反馈预览并等待确认"

    @staticmethod
    def _truncate_button_text(text: str, max_length: int) -> str:
        """按渠道限制裁剪按钮文案。"""
        if max_length <= 0 or len(text) <= max_length:
            return text
        if max_length <= 3:
            return text[:max_length]
        return text[: max_length - 3] + "..."

    @staticmethod
    def _result_payload(**fields) -> str:
        """统一 JSON 返回，便于 Agent 按字段继续下一步。"""
        return json.dumps(fields, ensure_ascii=False, indent=2)

    async def run(
        self,
        title: str,
        version: str,
        environment: str,
        issue_type: str,
        description: str,
        original_user_request: str,
        diagnostics_id: str,
        **kwargs,
    ) -> str:
        """校验草稿、发送预览按钮，并缓存待确认 token。"""
        if not self._channel or not self._source:
            return self._result_payload(
                success=False,
                reason="no_channel",
                message="当前不在可回传消息的会话中，无法发送 Issue 预览确认按钮。",
            )
        try:
            channel = MessageChannel(self._channel)
        except ValueError:
            return self._result_payload(
                success=False,
                reason="unsupported_channel",
                message=f"不支持的消息渠道: {self._channel}",
            )
        if not (
            ChannelCapabilityManager.supports_buttons(channel)
            and ChannelCapabilityManager.supports_callbacks(channel)
        ):
            return self._result_payload(
                success=False,
                reason="buttons_unsupported",
                message=f"当前渠道 {channel.value} 不支持按钮确认，不能自动提交反馈 Issue。",
            )

        diagnostics = feedback_issue_state_store.get_diagnostics(
            diagnostics_id,
            session_id=self._session_id,
            user_id=self._user_id,
        )
        if not diagnostics:
            return self._result_payload(
                success=False,
                reason="diagnostics_missing",
                message="缺少有效的诊断日志收集记录，请先调用 collect_feedback_diagnostics。",
            )
        # 日志全程只从服务端 state store 流转，避免日志在 LLM 上下文里反复
        # 进出造成响应延迟（见 collect_feedback_diagnostics 中的设计注释）。
        logs = diagnostics.logs

        for value, allowed, field_name in (
            (environment, ALLOWED_ENVIRONMENTS, "environment"),
            (issue_type, ALLOWED_ISSUE_TYPES, "issue_type"),
        ):
            err = SubmitFeedbackIssueTool._validate_enum(value, allowed, field_name)
            if err:
                return self._result_payload(success=False, reason="invalid_input", message=err)

        title = SubmitFeedbackIssueTool._truncate(title, MAX_TITLE_CHARS, marker="…")
        quality_err = SubmitFeedbackIssueTool._check_content_quality(
            title=title,
            description=description,
            original_user_request=original_user_request,
        )
        if quality_err:
            self._agent_context["feedback_issue_rejected_quality"] = True
            self._agent_context["feedback_issue_rejected_quality_reason"] = quality_err
            return self._result_payload(
                success=False,
                reason="rejected_quality",
                message=quality_err,
            )

        draft_hash = build_feedback_draft_hash(
            title=title,
            version=version,
            environment=environment,
            issue_type=issue_type,
            description=description,
            original_user_request=original_user_request,
            logs=logs,
            diagnostics_id=diagnostics_id,
        )

        # 同会话/用户已经发过预览且尚未被用户点击确认：拒绝重复发预览。
        # Why: Issue #5806 实测中 agent 在一次用户输入里连续调用了两次
        # prepare_feedback_issue，导致 TG 里出现两份「确认提交」按钮，用户
        # 点击两次后才进入提交。这里直接挡住重复预览：草稿一致就复用旧
        # token，草稿变了则要求 Agent 自己撤销旧 token 再发新预览（以免
        # 残留按钮指向过期内容）。
        active = feedback_issue_state_store.find_active_confirmation(
            session_id=self._session_id,
            user_id=self._user_id,
        )
        if active is not None:
            if active.draft_hash == draft_hash:
                logger.info(
                    "feedback issue preview deduped: session_id=%s reuse token=%s",
                    self._session_id,
                    active.confirmation_token[:8],
                )
                self._agent_context["user_reply_sent"] = True
                self._agent_context["reply_mode"] = "feedback_issue_confirmation"
                return self._result_payload(
                    success=True,
                    deduped=True,
                    confirmation_token=active.confirmation_token,
                    diagnostics_id=diagnostics_id,
                    message=(
                        "上一份相同内容的反馈预览仍在等待用户点击确认，"
                        "未重复发送按钮。请勿再次调用 prepare_feedback_issue。"
                    ),
                )
            logger.info(
                "feedback issue preview superseded: session_id=%s drop_token=%s",
                self._session_id,
                active.confirmation_token[:8],
            )
            feedback_issue_state_store.invalidate_active_confirmations(
                session_id=self._session_id,
                user_id=self._user_id,
            )

        confirmation = feedback_issue_state_store.create_confirmation(
            session_id=self._session_id,
            user_id=self._user_id,
            username=self._username,
            draft_hash=draft_hash,
            diagnostics_id=diagnostics_id,
        )

        option_value = f"{FEEDBACK_CONFIRM_VALUE_PREFIX}{confirmation.confirmation_token}"
        request = agent_interaction_manager.create_request(
            session_id=self._session_id,
            user_id=str(self._user_id),
            channel=channel.value,
            source=self._source,
            username=self._username,
            title="确认提交问题反馈",
            prompt="请确认是否将以下问题反馈提交到 MoviePilot 上游仓库。",
            options=[
                AgentInteractionOption(label="确认提交", value=option_value),
                AgentInteractionOption(label="取消提交", value="取消提交问题反馈"),
            ],
        )

        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        buttons = [
            [
                {
                    "text": self._truncate_button_text("确认提交", max_text_length),
                    "callback_data": f"agent_interaction:choice:{request.request_id}:1",
                }
            ],
            [
                {
                    "text": self._truncate_button_text("取消提交", max_text_length),
                    "callback_data": f"agent_interaction:choice:{request.request_id}:2",
                }
            ],
        ]
        preview = (
            "请确认是否提交以下问题反馈：\n\n"
            f"标题：{title}\n"
            f"版本：{version}\n"
            f"环境：{environment}\n"
            f"类型：{issue_type}\n"
            f"诊断日志：{'已找到相关日志' if diagnostics.found else '未找到明确相关日志'}\n\n"
            f"{description.strip()[:1800]}"
        )
        await ToolChain().async_post_message(
            Notification(
                channel=channel,
                source=self._source,
                mtype=NotificationType.Agent,
                userid=self._user_id,
                username=self._username,
                title="确认提交问题反馈",
                text=preview,
                buttons=buttons,
            )
        )
        logger.info(
            "feedback issue preview sent: session_id=%s diagnostics_id=%s token=%s",
            self._session_id,
            diagnostics_id,
            confirmation.confirmation_token[:8],
        )
        self._agent_context["user_reply_sent"] = True
        self._agent_context["reply_mode"] = "feedback_issue_confirmation"
        return self._result_payload(
            success=True,
            confirmation_token=confirmation.confirmation_token,
            diagnostics_id=diagnostics_id,
            message=(
                "已通过独立通知卡片发送 Issue 预览和「确认提交 / 取消提交」"
                "按钮给用户。**本轮对话不要再生成任何额外文字回复**——按钮"
                "卡片已经完整表达了 Issue 草稿和操作引导，复述「已生成 "
                "Issue 预览，请点击确认按钮」会和卡片重复并让用户困惑。"
                "请直接结束本轮，等待用户点击按钮触发下一轮。"
            ),
        )
