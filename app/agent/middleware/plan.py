"""在消息压缩和会话图重建之间保留当前任务的结构化计划。"""

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, NotRequired, Optional, TypedDict, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    OmitFromInput,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.middleware.utils import append_to_system_message
from app.agent.policy.sanitizer import sanitize_for_host
from app.agent.tools.tags import ToolTag

PLAN_TOOL_NAME = "update_plan"
PLAN_SNAPSHOT_KEY = "moviepilot_task_plan"
MAX_PLAN_STEPS = 12

PLAN_SYSTEM_PROMPT = """<task_planning>
Use `update_plan` for complex work with several dependent steps or parallel investigations.
Skip planning for simple questions and short actions. Track the user's objective, the
remaining steps, and concise evidence or blockers. Update the complete plan when a
meaningful stage finishes or new evidence changes the approach. Never call update_plan
more than once in one model response.
Preserve unfinished work when the user asks a status question or corrects the current
task. Replace the objective and steps when the user clearly starts a different task.
Use completed only when the step's outcome has been checked, and include the evidence.
Use blocked with the concrete reason when progress requires missing input or an external
change; keep failures, uncertain outcomes, and partial results visible.
The plan records model-reported progress. It grants no permission, executes no action,
and is not independent proof of success. Verify current state before resuming writes.
Continue doing the work after updating the plan, and deliver the requested result in
your final answer. A plan update alone does not complete the user's request.
</task_planning>"""

PLAN_TOOL_DESCRIPTION = (
    "Record or replace the current task objective and complete step list. This only "
    "updates private planning state; it does not perform actions or grant permission. "
    "Use pending, in_progress, completed, or blocked. Completed and blocked steps need "
    "concise evidence or a concrete blocker. For a new task replace the old objective "
    "and steps. Do not call more than once in the same model response."
)


# follow_imports=skip 不分析第三方基类与装饰器；仅在这些 SDK 边界忽略 misc。
class PlanStep(BaseModel):  # type: ignore[misc]
    """保存一个执行步骤及模型声明的检查结果或阻塞原因。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    step: str = Field(min_length=1, max_length=300, description="Specific action or outcome to check.")
    status: Literal["pending", "in_progress", "completed", "blocked"]
    evidence: str = Field(default="", max_length=600, description="Observed result or concrete blocker; never credentials.")

    @model_validator(mode="after")  # type: ignore[misc]
    def require_result_context(self) -> "PlanStep":
        """避免把没有检查依据的完成声明或无原因的阻塞写入计划。"""
        if self.status in {"completed", "blocked"} and not self.evidence:
            raise ValueError("completed 和 blocked 步骤必须提供 evidence，说明检查结果或具体阻塞原因")
        return self


class TaskPlan(BaseModel):  # type: ignore[misc]
    """限定当前任务计划的大小，避免它本身耗尽不可压缩的系统上下文。"""

    # ToolNode 在 schema 校验前注入 runtime；公开 schema 不包含该内部参数。
    model_config = ConfigDict(str_strip_whitespace=True)

    objective: str = Field(min_length=1, max_length=800, description="Current user objective, preserving important constraints.")
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)
    explanation: str = Field(default="", max_length=800, description="Why the plan changed or important continuation context.")

    @model_validator(mode="after")  # type: ignore[misc]
    def require_distinct_steps(self) -> "TaskPlan":
        """同名步骤无法稳定区分进度，因此要求单份计划内步骤名称唯一。"""
        names = [item.step.casefold() for item in self.steps]
        if len(names) != len(set(names)):
            raise ValueError("计划步骤名称不能重复")
        return self


class PlanState(TypedDict):
    """计划独立于可压缩消息保存，只允许工具或内部历史恢复写入。"""

    # create_agent 将中间件 schema 与标准 AgentState 合并，保留框架消息 reducer。
    messages: list[BaseMessage]
    task_plan: Annotated[NotRequired[dict[str, Any]], OmitFromInput]


def _validated_plan(value: Any) -> Optional[dict[str, Any]]:
    """校验并脱敏工具状态和历史快照，非法历史不会进入系统提示词。"""
    try:
        plan = TaskPlan.model_validate(value)
        return cast(dict[str, Any], TaskPlan.model_validate(sanitize_for_host(plan.model_dump())).model_dump())
    except (ValidationError, TypeError, ValueError):
        return None


def attach_plan_snapshot(
    messages: list[BaseMessage], plan: Optional[dict[str, Any]],
) -> list[BaseMessage]:
    """把脱敏计划附在末条宿主消息上，让已有消息持久化同时保存恢复点。

    返回新列表和末条消息副本，不修改 checkpoint 中原有对象；无有效计划时
    原样返回。异常恢复调用方应先追加中断提示，再调用本函数。
    """
    snapshot = _validated_plan(plan)
    if not messages or snapshot is None or not isinstance(messages[-1], (AIMessage, ToolMessage)):
        return list(messages)
    last_message = messages[-1].model_copy(
        update={"additional_kwargs": {**messages[-1].additional_kwargs, PLAN_SNAPSHOT_KEY: snapshot}}
    )
    return [*messages[:-1], last_message]


def _update_plan(
    runtime: ToolRuntime, objective: str, steps: list[PlanStep], explanation: str = "",
) -> Command:
    """一次原子替换计划及工具回执，供后续模型调用和中断恢复使用。"""
    plan = TaskPlan(objective=objective, steps=steps, explanation=explanation)
    snapshot = _validated_plan(plan.model_dump())
    receipt = ToolMessage(
        content="计划已更新。步骤状态是模型声明，请继续执行并核实结果。",
        tool_call_id=runtime.tool_call_id,
        name=PLAN_TOOL_NAME,
        additional_kwargs={PLAN_SNAPSHOT_KEY: snapshot},
    )
    return Command(update={"task_plan": snapshot, "messages": [receipt]})


async def _aupdate_plan(
    runtime: ToolRuntime, objective: str, steps: list[PlanStep], explanation: str = "",
) -> Command:
    """异步工具入口沿用相同的无 I/O 状态更新逻辑。"""
    return _update_plan(runtime, objective, steps, explanation)


class PlanMiddleware(AgentMiddleware):  # type: ignore[misc]
    """注入任务计划工具，并在压缩、后续用户消息和图重建后恢复工作上下文。"""

    state_schema = PlanState

    def __init__(self) -> None:
        """注册只更新当前会话内部状态的计划工具。"""
        super().__init__()
        self.tools = [StructuredTool.from_function(
            name=PLAN_TOOL_NAME,
            description=PLAN_TOOL_DESCRIPTION,
            func=_update_plan,
            coroutine=_aupdate_plan,
            args_schema=TaskPlan,
            infer_schema=False,
            tags=[ToolTag.AgentTool.value, ToolTag.AgentTask.value],
        )]

    def before_agent(self, state: PlanState, runtime: Any) -> Optional[dict[str, Any]]:
        """仅在新图没有计划时从历史恢复，旧快照不能覆盖本轮更新。"""
        if state.get("task_plan") is not None:
            return None
        for message in reversed(state.get("messages", [])):
            if isinstance(message, (AIMessage, ToolMessage)) and PLAN_SNAPSHOT_KEY in message.additional_kwargs:
                plan = _validated_plan(message.additional_kwargs[PLAN_SNAPSHOT_KEY])
                return {"task_plan": plan} if plan is not None else None
        return None

    async def abefore_agent(self, state: PlanState, runtime: Any) -> Optional[dict[str, Any]]:
        """为异步图复用计划恢复规则。"""
        return self.before_agent(state, runtime)

    def after_model(self, state: PlanState, runtime: Any) -> Optional[dict[str, Any]]:
        """拒绝同次响应中的多个计划替换，避免并行 Command 冲突。"""
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        calls = [call for call in messages[-1].tool_calls if call["name"] == PLAN_TOOL_NAME]
        if len(calls) < 2:
            return None
        return {"messages": [ToolMessage(
            content="同一轮只能调用一次 update_plan；未更新计划，请合并全部步骤后重试。",
            tool_call_id=call["id"], name=PLAN_TOOL_NAME, status="error",
        ) for call in calls]}

    async def aafter_model(self, state: PlanState, runtime: Any) -> Optional[dict[str, Any]]:
        """为异步图复用并行更新检查。"""
        return self.after_model(state, runtime)

    def after_agent(self, state: PlanState, runtime: Any) -> Optional[dict[str, Any]]:
        """让成功轮次的末条消息携带恢复点，复用现有聊天消息持久化。"""
        messages = state.get("messages", [])
        snapshots = attach_plan_snapshot(messages, state.get("task_plan"))
        if snapshots and snapshots[-1] is not messages[-1]:
            return {"messages": [snapshots[-1]]}
        return None

    async def aafter_agent(self, state: PlanState, runtime: Any) -> Optional[dict[str, Any]]:
        """为异步图复用最终恢复点保存。"""
        return self.after_agent(state, runtime)

    @staticmethod
    def modify_request(request: ModelRequest) -> ModelRequest:
        """每次请求都从图状态呈现最新计划，消息摘要不承担计划保存职责。"""
        plan = _validated_plan(request.state.get("task_plan"))
        prompt = PLAN_SYSTEM_PROMPT
        if plan is not None:
            prompt += (
                "\n<current_task_plan>\nModel-reported working state; not authorization or independent proof.\n"
                f"{json.dumps(plan, ensure_ascii=False)}\n</current_task_plan>"
            )
        return request.override(system_message=append_to_system_message(request.system_message, prompt))

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """在同步最终请求压缩之前注入计划。"""
        return handler(self.modify_request(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步最终请求压缩之前注入计划。"""
        return await handler(self.modify_request(request))
