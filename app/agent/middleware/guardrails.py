"""把每轮循环检测接入真实工具回执，不把重复读取变成缓存命中。"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_config
from langgraph.graph.message import add_messages
from langgraph.types import Command

from app.agent.code.authority import RPC_RESULT_KEY
from app.agent.guardrails.completion import (
    CONTINUATION_NUDGE,
    FRAGMENT_NUDGE,
    degenerate_final,
    intermediate_ack,
    trailing_continue_intent,
)
from app.agent.guardrails.controller import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    append_toolguard_guidance,
    toolguard_synthetic_result,
)
from app.agent.policy.api import resolve_api_operation
from app.agent.policy.contracts import ActionEffect, AuthSource, ExecutionOutcome, ToolOrigin, ToolPolicyContext
from app.agent.tools.base import TOOL_RESULT_RECORDER
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.result import EXECUTION_OUTCOME_KEY, TOOL_OBSERVATION_MARKER, inspect_tool_result

TURN_STATUS_KEY = 'moviepilot_turn_status'
GUARDRAIL_KEY = 'moviepilot_guardrail'
GUARDRAIL_REPLY_PREFIX = '本轮未完成：'
_TOOL_ALIASES = {'task': 'delegate_task', 'update_plan': 'todo_list', 'get_tool_execution': 'process_manage',
                 'edit_file': 'patch', 'apply_patch': 'patch', 'search_web': 'web_search',
                 'send_local_file': 'send_message', 'send_voice_message': 'send_message'}
_BROWSER_ALIASES = {'goto': 'browser_navigate', 'open_tab': 'browser_navigate', 'focus_tab': 'browser_navigate',
                    'click': 'browser_click', 'click_ref': 'browser_click', 'fill': 'browser_type', 'fill_ref': 'browser_type',
                    'select': 'browser_click', 'select_ref': 'browser_click', 'snapshot': 'browser_snapshot',
                    'get_content': 'browser_snapshot', 'screenshot': 'browser_get_images', 'wait': 'process_manage'}


@dataclass
class CallObservation:
    """只在一个工具批次内保留原始回执，按模型发出顺序消费，避免并行完成乱序。"""

    name: str
    arguments: dict[str, Any]
    content: str | None
    outcome: ExecutionOutcome


@dataclass
class GuardRun:
    """并行子任务共享编译图但不共享本轮计数，临时结果不写进 checkpoint。"""

    controller: ToolCallGuardrailController
    pending: dict[str, CallObservation] = field(default_factory=dict)
    outcomes: set[ExecutionOutcome] = field(default_factory=set)
    continuations: int = 0
    has_tools: bool = False
    codex_mode: bool = False
    message_updates: list[ToolMessage] = field(default_factory=list)
    identity: object | None = None


class ToolGuardrailsMiddleware(AgentMiddleware):  # type: ignore[misc]
    """交互式 Web/API 默认警告，消息网关及后台默认有界停止；新用户轮次重置。"""

    def __init__(self, context: ToolPolicyContext) -> None:
        """入口来自宿主认证信息，模型不能切换有人值守或后台策略。"""
        self.context = context
        unattended = (context.origin not in {ToolOrigin.SUBAGENT, ToolOrigin.AGENT_API}
                      and (context.origin is ToolOrigin.BACKGROUND or context.auth_source is AuthSource.CHANNEL))
        self.config = ToolCallGuardrailConfig(hard_stop_enabled=unattended)
        self._runs: dict[str, GuardRun] = {}

    @staticmethod
    def _scope() -> str:
        """LangGraph 宿主配置提供线程身份，不能由模型工具参数选择。"""
        try:
            return str(get_config().get('configurable', {}).get('thread_id') or '')
        except RuntimeError:
            return ''

    def _run(self) -> GuardRun:
        """按真实图线程隔离运行状态，异常遗留也不能使缓存无界增长。"""
        scope = self._scope()
        if scope not in self._runs:
            if len(self._runs) >= 64:
                raise RuntimeError('循环检测运行状态未释放，拒绝继续创建任务')
            self._runs[scope] = GuardRun(ToolCallGuardrailController(self.config))
        return self._runs[scope]

    @property
    def controller(self) -> ToolCallGuardrailController:
        """返回当前图线程自己的检测器。"""
        return self._run().controller

    @property
    def pending(self) -> dict[str, CallObservation]:
        """原始结果只归属当前运行，不能被并行子任务消费。"""
        return self._run().pending

    @property
    def outcomes(self) -> set[ExecutionOutcome]:
        """保存本轮实际观察到的结果状态，未知不推断为成功。"""
        return self._run().outcomes

    def release(self, thread_id: str) -> None:
        """宿主在取消或异常路径也释放该子任务的临时结果。"""
        self._runs.pop(thread_id, None)

    def _begin(self, runtime: Any) -> GuardRun:
        """以 LangGraph 每次运行的 control 身份重置，复用现有模型节点不增加预算开销。"""
        run = self._run()
        identity = getattr(runtime, 'control', None)
        if run.identity is not identity:
            run = GuardRun(ToolCallGuardrailController(self.config), identity=identity)
            self._runs[self._scope()] = run
        return run

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """记录当前模型实际提供的工具及 Codex 协议，结束检查不能凭模型名称猜入口。"""
        run = self._begin(request.runtime)
        run.has_tools = bool(request.tools)
        run.codex_mode = bool(getattr(request.model, 'use_responses_api', False)
                              and 'chatgpt.com/backend-api/codex' in str(getattr(request.model, 'openai_api_base', '') or ''))
        prepared = await self.prepare_model({'messages': request.messages}, None)
        if prepared:
            updates = prepared['messages']
            run.message_updates = [message for message in updates if isinstance(message, ToolMessage)]
            if prepared.get('jump_to') == 'end':
                return ModelResponse(result=[updates[-1]])
            request = request.override(messages=add_messages(request.messages, updates))
        try:
            return await handler(request)
        except BaseException:
            self.release(self._scope())
            raise

    async def after_model_update(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        """登记请求；只有短尾部行动或 Codex 特定碎片才最多追加两次继续提示。"""
        del runtime
        messages = state.get('messages', [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        active_ids = {message.id for message in messages}
        updates = [message for message in self._run().message_updates if message.id in active_ids]
        self._run().message_updates.clear()
        message = messages[-1]
        for call in message.tool_calls:
            self.pending[call['id']] = CallObservation(call['name'], dict(call.get('args') or {}), None, ExecutionOutcome.UNKNOWN)
        continuation = None if message.tool_calls or not self._run().has_tools else self._continue(messages, message)
        if continuation:
            if continuation.get('jump_to') != 'model':
                self.release(self._scope())
            return {**continuation, 'messages': [*updates, *continuation['messages']]}
        if not message.tool_calls:
            self.release(self._scope())
        return {'messages': updates} if updates else None

    def _continue(self, messages: list[Any], message: AIMessage) -> dict[str, Any] | None:
        """合成提示不进入真实用户历史；预算耗尽仍报告部分完成，不伪造工具成果。"""
        run = self._run()
        text = re.sub(r'<think>.*?(?:</think>|$)', '', message.text, flags=re.DOTALL).strip()
        user_text = next((item.text for item in reversed(messages) if isinstance(item, HumanMessage)
                          and not item.additional_kwargs.get(TOOL_OBSERVATION_MARKER)), '')
        tool_results = 0
        for previous in reversed(messages[:-1]):
            if isinstance(previous, HumanMessage):
                break
            tool_results += isinstance(previous, ToolMessage)
        stalled = trailing_continue_intent(text)
        fragment = run.codex_mode and tool_results > 0 and degenerate_final(text, user_text)
        ack = run.codex_mode and intermediate_ack(text, user_text, tool_results=sum(isinstance(item, ToolMessage) for item in messages))
        if not (stalled or fragment or ack):
            return None
        if run.continuations >= 2:
            return {'messages': [message.model_copy(update={
                'content': GUARDRAIL_REPLY_PREFIX + '模型仍只描述下一步，未给出完成结果。\n' + text,
                'additional_kwargs': {**message.additional_kwargs, TURN_STATUS_KEY: 'partial',
                                      GUARDRAIL_KEY: {'code': 'continuation_exhausted', 'count': 2}},
            })]}
        run.continuations += 1
        nudge = HumanMessage(content=FRAGMENT_NUDGE if fragment and not stalled else CONTINUATION_NUDGE,
                             additional_kwargs={TOOL_OBSERVATION_MARKER: True})
        return {'messages': [nudge], 'jump_to': 'model'}

    def _name(self, request: ToolCallRequest) -> str:
        """映射宿主多动作工具，轮询豁免不能扩大到启动、写入或终止动作。"""
        name, arguments = request.tool_call['name'], request.tool_call.get('args') or {}
        if name == 'execute_command':
            return 'process_manage' if arguments.get('action') in {'read', 'wait', 'status', 'list'} else 'terminal'
        if name == 'subagent_task':
            return 'process_manage' if arguments.get('action') in {'list', 'status', 'wait'} else 'delegate_task'
        if name in _TOOL_ALIASES:
            return _TOOL_ALIASES[name]
        if name == 'browse_webpage':
            return _BROWSER_ALIASES.get(str(arguments.get('action') or ''), str(name))
        if name == 'agent_task' and arguments.get('action') in {'create', 'update', 'run', 'delete'}:
            return 'cronjob_manage'
        if isinstance(request.tool, MoviePilotApiTool):
            operation_id = str(arguments.get('operation_id') or '')
            operation = resolve_api_operation(operation_id)
            name = f'{name}:{operation_id}'
            if operation is not None:
                config = self.controller.config
                if operation.effect is ActionEffect.SAFE_READ:
                    self.controller.config = replace(config, idempotent_tools=config.idempotent_tools | {name})
                elif operation.effect not in {ActionEffect.SENSITIVE_READ, ActionEffect.UNKNOWN}:
                    self.controller.config = replace(config, progress_tools=config.progress_tools | {name})
        return str(name)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]) -> Any:
        """执行前检查上轮证据和总预算，执行后暂存原文而不改变真实工具结果。"""
        if isinstance(request.state, dict) and RPC_RESULT_KEY in request.state:
            return await handler(request)
        name = self._name(request)
        arguments = dict(request.tool_call.get('args') or {})
        if name == 'delegate_task' and arguments.get('action') in {'cancel', 'update'}:
            arguments['action'] = {'cancel': 'stop', 'update': 'steer'}[arguments['action']]
        decision = self.controller.before_call(name, arguments)
        if not decision.allows_execution:
            return ToolMessage(content=toolguard_synthetic_result(decision), tool_call_id=request.tool_call['id'],
                               name=request.tool_call['name'], status='error',
                               additional_kwargs={EXECUTION_OUTCOME_KEY: 'failed', GUARDRAIL_KEY: decision.to_metadata()})
        self.pending[request.tool_call['id']] = CallObservation(name, arguments, None, ExecutionOutcome.UNKNOWN)
        raw: dict[str, str] = {}
        previous = TOOL_RESULT_RECORDER.get()

        def record(tool_name: str, text: str) -> dict[str, Any]:
            """在通用截断前观察原文，同时保留外层归档器的精确引用。"""
            raw[tool_name] = text
            return previous(tool_name, text) if previous else {}

        token = TOOL_RESULT_RECORDER.set(record)
        try:
            result = await handler(request)
        except asyncio.CancelledError:
            self.release(self._scope())
            raise
        finally:
            TOOL_RESULT_RECORDER.reset(token)
        messages = result.update.get('messages', []) if isinstance(result, Command) and isinstance(result.update, dict) else [result]
        for message in messages:
            if isinstance(message, ToolMessage) and message.tool_call_id == request.tool_call['id']:
                content = raw.get(request.tool_call['name'], message.content)
                outcome = inspect_tool_result(message)
                self.pending[message.tool_call_id] = CallObservation(name, arguments, content if isinstance(content, str) else None, outcome)
                self.outcomes.add(outcome)
        return result

    def _observe(self, message: ToolMessage, active_ids: set[str]) -> ToolMessage:
        """在结果进入下一次模型请求前附加提示；被压缩掉的引用不能继续产生存根。"""
        item = self.pending.pop(message.tool_call_id)
        if message.additional_kwargs.get(GUARDRAIL_KEY):
            return message
        if item.content is None:
            item.content = message.content if isinstance(message.content, str) else None
            item.outcome = inspect_tool_result(message)
        self.outcomes.add(item.outcome)
        failed = item.outcome in {ExecutionOutcome.FAILED, ExecutionOutcome.UNKNOWN}
        decision = self.controller.after_call(item.name, item.arguments, item.content, failed=failed)
        observation = self.controller.observe_call(item.name, item.arguments, item.content,
                                                  tool_call_id=message.tool_call_id, failed=failed)
        if not isinstance(message.content, str):
            return message
        stub = observation.stub
        if self.controller.first_result_call_id not in active_ids:
            self.controller.replace_result_reference(message.tool_call_id)
            stub = None
        # 保留可分页的原始回执元数据；存根不隐藏 pending/unknown 等宿主状态。
        content = f'{stub}\nexecution_outcome={item.outcome.value}' if stub else message.content
        if stub:
            content += self._fresh_reference(message.content)
        content = append_toolguard_guidance(content, decision)
        if observation.notice:
            content += '\n\n' + observation.notice
        return message.model_copy(update={'content': content})

    @staticmethod
    def _fresh_reference(content: str) -> str:
        """大结果缓存会淘汰旧引用，重复存根指向这次实际执行刚保存的正文。"""
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return ''
        if isinstance(payload, dict) and payload.get('tool_result_truncated') and payload.get('result_id'):
            return f"\nFull fresh result: read_tool_result(result_id={payload['result_id']!r}, offset=0)."
        return ''

    async def prepare_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        """批次结果按请求顺序判定短循环；触发停止时保留计划和未知副作用证据。"""
        del runtime
        messages = state.get('messages', [])
        active_ids = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
        by_id = {message.tool_call_id: message for message in messages if isinstance(message, ToolMessage)}
        updates = []
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    if call['id'] in self.pending and call['id'] in by_id:
                        updates.append(self._observe(by_id[call['id']], active_ids))
        decision = self.controller.halt_decision
        if decision:
            outcome = 'unknown' if ExecutionOutcome.UNKNOWN in self.outcomes else 'waiting' if ExecutionOutcome.PENDING in self.outcomes else 'blocked'
            text = (f'{GUARDRAIL_REPLY_PREFIX}在 {decision.tool_name} 上重复尝试且没有取得进展，已停止继续调用。'
                    '任务尚未完成，请依据最近回执调整方法；已执行的操作不会因停止而撤销。')
            updates.append(AIMessage(content=text, additional_kwargs={TURN_STATUS_KEY: outcome, GUARDRAIL_KEY: decision.to_metadata()}))
            return {'messages': updates, 'jump_to': 'end'}
        return {'messages': updates} if updates else None
