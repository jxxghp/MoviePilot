"""Agent 后台提示、持久化定时任务与心跳执行 owner。"""

import asyncio
import uuid
from typing import Callable, Optional

from app.agent.contracts import ReplyMode
from app.agent.lifecycle import AgentLifecycleOwner
from app.agent.middleware.jobs import filter_active_jobs, load_jobs_metadata
from app.agent.orchestrator import AGENT_EXECUTION_ERROR_PREFIX, HEARTBEAT_SESSION_PREFIX
from app.agent.prompt import prompt_manager
from app.agent.runtime import agent_runtime_manager
from app.application.agenttask import get_agent_task_execution_service
from app.chain.agent import AgentChain
from app.foundation.identity import SYSTEM_INTERNAL_USER_ID
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.message import Message
from app.schemas.types import MessageType


class AgentTaskOwner(AgentLifecycleOwner):
    """执行不绑定交互请求的 Agent 后台与调度任务。"""

    async def run_background_prompt(
            self,
            message: str,
            session_prefix: str = "__agent_background",
            output_callback: Optional[Callable[[str], None]] = None,
            reply_mode: ReplyMode = ReplyMode.CAPTURE_ONLY,
            allow_message_tools: Optional[bool] = None,
    ) -> None:
        """
        以独立后台会话执行一段 prompt。
        """
        session_id = f"{session_prefix}_{uuid.uuid4().hex[:8]}__"
        user_id = SYSTEM_INTERNAL_USER_ID

        if reply_mode == ReplyMode.CAPTURE_ONLY:
            allow_message_tools = False
        elif allow_message_tools is None:
            allow_message_tools = True

        try:
            await self.process_message(
                session_id=session_id,
                user_id=user_id,
                message=message,
                channel=None,
                source=None,
                username=get_runtime_setting('SUPERUSER'),
                reply_mode=reply_mode,
                output_callback=output_callback,
                allow_message_tools=allow_message_tools,
                wait_for_completion=True,
            )
        finally:
            await self.clear_session(session_id=session_id, user_id=user_id)

    async def execute_scheduled_task(
            self,
            task_id: int,
            trigger_source: str = "scheduled",
            scheduler_generation: int | None = None,
            remove_schedule: Callable[[int, int, str], bool] | None = None,
    ) -> tuple[bool, str]:
        """
        按持久化上下文唤醒 Agent 执行自主定时任务并向用户回传结果。

        :param task_id: Agent 定时任务 ID
        :param trigger_source: 触发入口，scheduled-自动调度，manual-显式立即执行
        :return: 执行是否成功及结果摘要
        """
        if not get_runtime_setting('AI_AGENT_ENABLE'):
            return False, "AI Agent 未启用"
        accepting_before_claim = self._accepting_tasks
        task_service = get_agent_task_execution_service()
        claim = await task_service.claim(
            task_id=task_id,
            trigger_source=trigger_source,
            scheduler_generation=scheduler_generation,
            remove_schedule=remove_schedule,
        )
        run = claim.run
        if run is None:
            return False, claim.rejection or "Agent 定时任务当前不可执行"

        trigger_description = (
            "已手动触发" if run.trigger_source == "manual" else "已按计划触发"
        )
        task_message = (
            f"定时任务{trigger_description}。请立即完成下面的任务，不要只确认收到，"
            f"也不要重复创建同一个定时任务。\n\n"
            f"任务名称：{run.name}\n"
            f"任务内容：{run.content}\n\n"
            "完成后请直接向用户发送消息报告本次执行结果；如果无法完成，也需发送消息说明原因。"
        )
        success = True
        result = ""
        notification_username = run.username or get_runtime_setting('SUPERUSER')
        try:
            result = await self.process_message(
                session_id=run.session_id,
                user_id=run.user_id,
                message=task_message,
                channel=None,
                source=None,
                username=notification_username,
                original_chat_id=None,
                reply_mode=ReplyMode.DISPATCH,
                allow_message_tools=True,
                wait_for_completion=True,
            )
            result_text = str(result or "").strip()
            success = not result_text.startswith(
                (AGENT_EXECUTION_ERROR_PREFIX, "处理消息时发生错误")
            )
        except asyncio.CancelledError:
            success = False
            result = "Agent 定时任务已取消"
            raise
        except Exception as err:
            success = False
            error_message = str(err)
            if (
                accepting_before_claim
                and not self._accepting_tasks
                and error_message == "AgentManager 未运行或已关闭"
            ):
                error_message = "AgentManager 已关闭"
            result = f"Agent 定时任务执行失败：{error_message}"
            logger.error(f"Agent 定时任务 {task_id} 执行失败: {str(err)}")
            await AgentChain().async_post_message(
                Message(
                    mtype=MessageType.Agent,
                    username=notification_username,
                    title=f"定时任务执行失败：{run.name}",
                    text=result,
                    save_history=False,
                )
            )
        finally:
            await task_service.finalize(
                run,
                success=success,
                result=str(result or ""),
                scheduler_generation=scheduler_generation,
                remove_schedule=remove_schedule,
            )

        return success, str(result or "任务执行完成")

    @staticmethod
    def _build_heartbeat_prompt() -> str:
        """使用程序内置 System Tasks 定义构建心跳任务提示词。"""
        return prompt_manager.render_system_task_message("heartbeat")

    async def heartbeat_check_jobs(self) -> None:
        """
        心跳唤醒：检查并执行待处理的定时任务（Jobs）。
        由定时调度器周期性调用，每次使用独立的会话避免上下文干扰。
        """
        try:
            active_jobs = filter_active_jobs(
                await load_jobs_metadata([str(agent_runtime_manager.jobs_dir)])
            )
            # 先在本地判断是否存在活跃任务。没有任务时直接短路，避免一次完整
            # 的后台 Agent/LLM 空调用。
            if not active_jobs:
                logger.info("智能体心跳唤醒：没有活跃任务，跳过模型调用")
                return

            # 每次使用唯一的 session_id，避免共享上下文
            session_id = f"{HEARTBEAT_SESSION_PREFIX}{uuid.uuid4().hex[:12]}__"
            user_id = SYSTEM_INTERNAL_USER_ID

            logger.info("智能体心跳唤醒：开始检查待处理任务...")
            heartbeat_message = self._build_heartbeat_prompt()

            await self.process_message(
                session_id=session_id,
                user_id=user_id,
                message=heartbeat_message,
                channel=None,
                source=None,
                username=get_runtime_setting('SUPERUSER'),
                reply_mode=ReplyMode.CAPTURE_ONLY,
                allow_message_tools=True,
            )

            # 等待消息队列处理完成
            if session_id in self._session_queues:
                await self._session_queues[session_id].join()

            # 等待worker结束
            if session_id in self._session_workers:
                try:
                    await self._session_workers[session_id]
                except asyncio.CancelledError:
                    pass

            logger.info("智能体心跳唤醒：任务检查完成")

            # 心跳会话用完即弃，清理资源
            await self.clear_session(session_id, user_id)

        except Exception as e:
            logger.error(f"智能体心跳唤醒失败: {e}")
