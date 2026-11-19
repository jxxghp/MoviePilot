"""
临时知识只进入本轮主模型，真实图、历史、压缩和学习边界均不得复制原文。
"""

import asyncio
import json
import traceback
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from app.agent.evidence import scope as scope_module
from app.agent.evidence.middleware import EphemeralEvidenceError, EphemeralEvidenceMiddleware
from app.agent.evidence.scope import (
    EVIDENCE_HISTORY_NOTE,
    EVIDENCE_MARKER,
    EVIDENCE_TTL_SECONDS,
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    MAX_TOTAL_CHARS,
    EvidenceScope,
    evidence_call,
    evidence_in_use,
    evidence_turn,
    materialize_evidence,
    publish_evidence,
    redact_evidence_message,
)
from app.agent.learning.review import ReviewSnapshot
from app.agent.learning.session import LearningSession
from app.agent.learning.tools import LearningTools
from app.agent.middleware.learning import LearningCaptureMiddleware
from app.agent.middleware.output import ToolOutputMiddleware
from app.agent.middleware.summarization import (
    ContextPreservingSummarizationMiddleware,
    FinalRequestCompactionMiddleware,
)
from app.agent.orchestrator import MoviePilotAgent
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.policy.orchestrator import AgentToolPolicyOrchestrator
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.result import messages_for_persistence

SENTINEL = "private-synthetic-knowledge-c91e-"
BODY = SENTINEL * 700


class _EmptyInput(BaseModel):
    """
    合成工具不接受模型提供的来源、认证或原文。
    """


class _KnowledgeTool(MoviePilotTool):
    """
    经真实工具基类执行和记录日志，只在格式化时发布合成资料。
    """

    name: str = Field(default="fixture_knowledge", description="临时知识合成工具名称")
    description: str = Field(default="读取本轮合成知识。", description="合成工具用途")
    args_schema: type[BaseModel] = Field(default=_EmptyInput, description="不含任何输入字段的契约")

    async def _check_permission(self) -> Optional[str]:
        """
        身份由合成测试预先绑定，不访问真实用户数据库。
        """
        return None

    async def run(self, *args: Any, **kwargs: Any) -> str:
        """
        返回固定合成原文，不发起网络或读取业务配置。
        """
        del args, kwargs
        return BODY

    def format_agent_result(self, result: Any, **tool_arguments: Any) -> str:
        """
        在工具基类日志和通用截断之前替换为无正文回执。
        """
        del tool_arguments
        return publish_evidence(result)


class _Model(FakeMessagesListChatModel):
    """
    记录主模型实际读取的消息，用预置响应驱动真实 ToolNode。
    """

    requests: list[list[BaseMessage]] = Field(default_factory=list, description="合成模型实际读取的消息副本")

    def bind_tools(self, _tools: list[Any], **_kwargs: Any) -> "_Model":
        """
        维持真实图的工具绑定入口，不访问外部提供方。
        """
        return self

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        """
        只在测试模型端保留原文，用来证明获授权请求确实获得资料。
        """
        self.requests.append(deepcopy(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _message(reference: str, content: str = "安全回执", *, call_id: str = "call", name: str = "fixture_knowledge") -> ToolMessage:
    """
    构造具有明确工具调用归属的宿主消息。
    """
    return ToolMessage(content=content, tool_call_id=call_id, name=name, additional_kwargs={EVIDENCE_MARKER: reference})


def _staged(content: str = BODY) -> ToolMessage:
    """
    在真实调用边界发布原文，只返回本地引用消息。
    """
    with evidence_call("call", "fixture_knowledge") as reference:
        assert SENTINEL not in publish_evidence(content)
        identity = reference()
        assert identity is not None
        return _message(identity)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_receipt", [False, True])
async def test_published_tool_failure_never_exposes_private_payload(malformed_receipt: bool) -> None:
    """
    工具发布临时资料后发生异常或生成非标准回执，均以安全错误终止且不回显正文。
    """
    middleware = EphemeralEvidenceMiddleware()
    request = ToolCallRequest(tool_call={"id": "call", "name": "fixture_knowledge", "args": {}},
                              tool=None, state={"messages": []}, runtime=None)

    async def fail(_request: ToolCallRequest) -> Any:
        """
        先经真实发布边界登记资料，再模拟工具执行或回执构造失败。
        """
        publish_evidence(BODY)
        if malformed_receipt:
            return BODY
        raise ValueError(BODY)

    with evidence_turn(), pytest.raises(EphemeralEvidenceError) as failure:
        await middleware.awrap_tool_call(request, fail)
    assert SENTINEL not in "".join(traceback.format_exception(failure.value))


def test_sync_model_projection_keeps_state_private() -> None:
    """
    同步模型也只在本次请求副本中读取原文，图消息和状态持续持有安全引用。
    """
    middleware = EphemeralEvidenceMiddleware()
    model = _Model(responses=[AIMessage(content="unused")])
    with evidence_turn():
        message = _staged()
        request = ModelRequest(model=model, messages=[message], tools=[], state={"messages": [message]}, runtime=None)

        def infer(current: ModelRequest) -> ModelResponse:
            """
            验证真实正文仅到达模型处理器，返回可持久化的普通回答。
            """
            assert SENTINEL in str(current.messages)
            return ModelResponse([AIMessage(content="根据当前资料给出必要事实")])

        result = middleware.wrap_model_call(request, infer)
        assert result.result[0].content == "根据当前资料给出必要事实"
        assert SENTINEL not in str(request.messages) + str(request.state)


@pytest.mark.parametrize("has_evidence", [False, True])
def test_sync_model_errors_hide_only_private_evidence(has_evidence: bool) -> None:
    """
    使用临时资料的同步请求隐藏提供方错误原文，普通请求仍保持既有错误类型。
    """
    middleware = EphemeralEvidenceMiddleware()
    model = _Model(responses=[AIMessage(content="unused")])
    with evidence_turn():
        message = _staged() if has_evidence else HumanMessage(content="普通任务")
        request = ModelRequest(model=model, messages=[message], tools=[], state={"messages": [message]}, runtime=None)

        def fail(_current: ModelRequest) -> ModelResponse:
            """
            模拟提供方拒绝请求，同时保留正常请求的故障传播语义。
            """
            raise ValueError(BODY if has_evidence else "ordinary-provider-error")

        with pytest.raises(EphemeralEvidenceError if has_evidence else ValueError) as failure:
            middleware.wrap_model_call(request, fail)
        assert SENTINEL not in "".join(traceback.format_exception(failure.value))


def test_publish_without_both_host_boundaries_fails_closed() -> None:
    """
    直接调用、缺少本轮或缺少真实调用边界时，不能通过普通结果回显正文。
    """
    assert json.loads(publish_evidence(BODY))["success"] is False
    with evidence_turn():
        assert json.loads(publish_evidence(BODY))["success"] is False
    with evidence_call("call", "fixture_knowledge"):
        assert json.loads(publish_evidence(BODY))["success"] is False


def test_scope_limits_ttl_ownership_and_closed_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    超限不截断、调用不符不可读，过期仍禁止学习，关闭拒绝再次发布。
    """
    now = [10.0]
    monkeypatch.setattr(scope_module.time, "monotonic", lambda: now[0])
    scope = EvidenceScope()
    assert scope.publish("x" * (MAX_ENTRY_CHARS + 1), "call", "tool") is None
    refs = [scope.publish("x" * MAX_ENTRY_CHARS, str(i), "tool") for i in range(MAX_TOTAL_CHARS // MAX_ENTRY_CHARS)]
    assert all(refs) and scope.publish("more", "other", "tool") is None
    assert scope.resolve(refs[0], "other", "tool") is None
    assert scope.resolve(refs[0], "0", "other") is None
    now[0] += EVIDENCE_TTL_SECONDS
    assert scope.resolve(refs[0], "0", "tool") is None and scope.used
    refs = [scope.publish("short", str(i), "tool") for i in range(MAX_ENTRIES)]
    assert all(refs) and scope.publish("short", "extra", "tool") is None
    scope.close()
    scope.close()
    assert scope.publish(BODY, "late", "tool") is None
    assert scope.resolve(refs[0], "0", "tool") is None


@pytest.mark.asyncio
async def test_concurrent_turns_and_late_child_cannot_share_or_repopulate() -> None:
    """
    即使调用 ID 相同，异步会话仍隔离；继承旧上下文的迟到子任务也失败关闭。
    """
    ready = asyncio.Event()

    async def worker(label: str) -> tuple[str, str]:
        """
        独立容器沿 asyncio 上下文传播，不依赖图状态保存对象。
        """
        with evidence_turn():
            message = _staged(label)
            await asyncio.sleep(0)
            return message.additional_kwargs[EVIDENCE_MARKER], materialize_evidence(message).content

    first, second = await asyncio.gather(worker("first"), worker("second"))
    assert first[0] != second[0] and "first" in first[1] and "second" not in first[1]
    assert "second" in second[1] and "first" not in second[1]
    assert materialize_evidence(_message(first[0])).content == EVIDENCE_HISTORY_NOTE

    async def late() -> str:
        """
        模拟已取得上下文、但在执行轮关闭后才返回的工具线程或任务。
        """
        await ready.wait()
        return publish_evidence(BODY)

    with evidence_turn():
        with evidence_call("late", "fixture_knowledge"):
            task = asyncio.create_task(late())
    ready.set()
    assert json.loads(await task)["success"] is False
    assert not evidence_in_use()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_real_graph_never_checkpoints_archives_or_learns_raw_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    """
    主模型真正读取正文，而图中每一代 checkpoint、日志、结果缓存和学习均没有原文。
    """
    log = Mock()
    monkeypatch.setattr("app.agent.tools.base.logger", log)
    monkeypatch.setattr("app.agent.policy.orchestrator.logger", log)
    context = ToolPolicyContext(session_id="ephemeral", user_id="user", origin=ToolOrigin.AGENT_INTERACTIVE,
                                principal_type=PrincipalType.HUMAN, auth_source=AuthSource.WEB_SESSION, agent_context={})
    output = ToolOutputMiddleware(context)
    tool = _KnowledgeTool(session_id="ephemeral", user_id="user")
    model = _Model(responses=[
        AIMessage(content="", tool_calls=[{"name": tool.name, "args": {}, "id": "call"}]),
        AIMessage(content="已结合本轮资料回答必要事实。"), AIMessage(content="需要重新检索才能核对原文。"),
    ])
    learning = LearningSession(session_id="ephemeral", skill_root=tmp_path / "skills", memory_root=tmp_path / "memory",
                               public_roots=(), on_usage=lambda _usage: None)
    saver = InMemorySaver()
    graph = create_agent(model, tools=[tool], middleware=[output, EphemeralEvidenceMiddleware(), LearningCaptureMiddleware(learning)], checkpointer=saver)
    config = {"configurable": {"thread_id": "ephemeral"}}
    with evidence_turn() as scope:
        if streaming:
            events = [event async for event in graph.astream({"messages": [HumanMessage(content="查阅规则")]}, config, stream_mode="updates")]
            assert SENTINEL not in str(events)
        else:
            await graph.ainvoke({"messages": [HumanMessage(content="查阅规则")]}, config)
        assert scope.used and SENTINEL in str(model.requests[-1])
        assert learning.snapshot is None
        result = graph.get_state(config).values["messages"]
        observation = AgentToolPolicyOrchestrator().start(context=context, tool=tool, arguments={})
        AgentToolPolicyOrchestrator.finish(observation, next(item for item in result if isinstance(item, ToolMessage)))
        assert SENTINEL not in str(log.mock_calls)
        assert not output._results
        assert SENTINEL not in str([item.values for item in graph.get_state_history(config)])
        assert SENTINEL not in repr(saver.storage) + repr(saver.writes)
        assert SENTINEL not in str(messages_for_persistence(result))
    with evidence_turn():
        await graph.ainvoke({"messages": [HumanMessage(content="继续核对出处")]}, config)
        assert SENTINEL not in str(model.requests[-1])
        assert EVIDENCE_HISTORY_NOTE in str(model.requests[-1])
        assert learning.snapshot is not None
        assert SENTINEL not in str(learning.snapshot.messages)
        assert EVIDENCE_MARKER not in str(learning.snapshot.messages)


@pytest.mark.asyncio
async def test_summary_and_compaction_state_command_are_redacted() -> None:
    """
    压缩预算看到真实正文，摘要输入没有正文，保留消息写回时也不能带回正文。
    """
    summary_model = _Model(responses=[AIMessage(content="安全摘要")])
    summarizer = ContextPreservingSummarizationMiddleware(model=summary_model)
    middleware = EphemeralEvidenceMiddleware()
    with evidence_turn():
        message = _staged()
        request = ModelRequest(model=summary_model, messages=[HumanMessage(content="任务要求"), message], tools=[],
                               state={"messages": [message]}, runtime=None)

        async def compact(current: ModelRequest) -> ModelResponse:
            """
            通过真实摘要器再模拟近期历史写回，覆盖两条不同的泄漏路径。
            """
            assert SENTINEL in str(current.messages)
            await summarizer.acreate_summary(current.messages)
            response = FinalRequestCompactionMiddleware._with_state_update(ModelResponse([AIMessage(content="必要事实")]), current.messages)
            assert SENTINEL not in str(response.command.update)
            assert EVIDENCE_MARKER in response.command.update["messages"][-2].additional_kwargs
            return response.model_response

        result = await middleware.awrap_model_call(request, compact)
        assert SENTINEL not in str(summary_model.requests)
        assert result.result[0].content == "必要事实"
        assert SENTINEL not in str(request.state) and SENTINEL not in str(request.messages)


@pytest.mark.asyncio
async def test_real_final_compaction_counts_evidence_without_writing_it_back() -> None:
    """
    实际压缩器为正文腾出预算，近期资料仍到主模型，所有 checkpoint 只持有占位。
    """
    tool = _KnowledgeTool(session_id="compaction", user_id="user")
    model = _Model(profile={"max_input_tokens": 12000}, responses=[
        AIMessage(content="", tool_calls=[{"name": tool.name, "args": {}, "id": "call"}]),
        AIMessage(content="已核对必要事实。"),
    ])
    summary = _Model(profile={"max_input_tokens": 12000}, responses=[AIMessage(content="保留原任务约束的安全摘要。")])
    summarizer = ContextPreservingSummarizationMiddleware(model=summary, trigger=("fraction", .85), keep=("messages", 4))
    graph = create_agent(model, tools=[tool], middleware=[EphemeralEvidenceMiddleware(), FinalRequestCompactionMiddleware(summarizer=summarizer)],
                         checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "compaction"}}
    history = [item for _ in range(3) for item in [HumanMessage(content="历史任务 " + "history-context " * 450), AIMessage(content="已处理。")]]
    with evidence_turn():
        await graph.ainvoke({"messages": [*history, HumanMessage(content="核对当前规则")]}, config)
        assert summary.requests and SENTINEL not in str(summary.requests)
        assert SENTINEL in str(model.requests[-1])
        assert SENTINEL not in str([item.values for item in graph.get_state_history(config)])


@pytest.mark.parametrize("failure", [None, RuntimeError, TimeoutError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_orchestrator_closes_scope_before_channel_teardown(monkeypatch: pytest.MonkeyPatch, failure: Optional[type[BaseException]]) -> None:
    """
    真实执行入口在成功、错误、超时和取消后都先清原文，再进行可能失败的渠道收尾。
    """
    agent = MoviePilotAgent(session_id="scope-owner", user_id="user")
    captured = []

    async def build(**_kwargs):
        """
        在真实执行轮内取得资料；构图失败也必须经过容器退出。
        """
        captured.append(_staged())
        assert evidence_in_use()
        if failure is not None:
            raise failure("合成失败")
        return SimpleNamespace(ainvoke=AsyncMock(), get_state=lambda _config: SimpleNamespace(values={"messages": []}))

    async def teardown():
        """
        渠道收尾在容器清空后执行，不能延长原文寿命。
        """
        assert not evidence_in_use()
        assert materialize_evidence(captured[0]).content == EVIDENCE_HISTORY_NOTE

    monkeypatch.setattr(agent, "_create_agent", build)
    monkeypatch.setattr(agent, "_learning_enabled", lambda: False)
    monkeypatch.setattr(agent, "_should_stream", lambda: False)
    monkeypatch.setattr(agent, "_save_final_agent_state", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "_recover_interrupted_agent", AsyncMock())
    monkeypatch.setattr(agent, "_dispatch_execution_notice", AsyncMock())
    monkeypatch.setattr(agent, "_send_agent_tokens_usage_event", lambda **_kwargs: None)
    monkeypatch.setattr(agent.stream_handler, "stop_streaming", teardown)
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await agent._execute_agent([])
    else:
        await agent._execute_agent([])
    assert captured and not evidence_in_use()
    await agent.cleanup()


@pytest.mark.asyncio
async def test_provider_error_does_not_log_or_return_raw_request() -> None:
    """
    提供方异常即使含请求原文，也不能经上层 traceback 或用户错误回执泄漏。
    """
    middleware = EphemeralEvidenceMiddleware()
    model = _Model(responses=[AIMessage(content="unused")])
    with evidence_turn():
        message = _staged()
        request = ModelRequest(model=model, messages=[message], tools=[], state={"messages": [message]}, runtime=None)

        async def fail(_current: ModelRequest) -> ModelResponse:
            """
            模拟兼容模型服务把请求正文拼进异常消息。
            """
            raise ValueError(BODY)

        with pytest.raises(EphemeralEvidenceError):
            try:
                await middleware.awrap_model_call(request, fail)
            except EphemeralEvidenceError:
                assert SENTINEL not in traceback.format_exc()
                raise


def test_history_and_interrupted_recovery_remove_duplicate_payloads() -> None:
    """
    历史与恢复不仅清正文，还清除可能再次带正文的 artifact 和协议附加字段。
    """
    original = _message("unavailable", BODY)
    original.artifact = {"body": BODY}
    original.additional_kwargs["duplicate"] = BODY
    original.response_metadata["duplicate"] = BODY
    for safe in [redact_evidence_message(original), *messages_for_persistence([original]), MoviePilotAgent._sanitize_recovery_message(original)]:
        assert SENTINEL not in str(safe.model_dump())
        assert safe.tool_call_id == original.tool_call_id and safe.name == original.name and safe.status == original.status
        assert EVIDENCE_MARKER not in safe.additional_kwargs
    assert original.content == BODY and original.artifact["body"] == BODY
    human = HumanMessage(content="用户自己的资料保留")
    assert redact_evidence_message(human) is human


@pytest.mark.asyncio
async def test_learning_snapshot_and_write_tools_reject_evidence_turn() -> None:
    """
    资料过期或压缩后也不能转入后台复盘，显式学习写入口同样拒绝。
    """
    skills = SimpleNamespace(manage=Mock(side_effect=AssertionError("不应进入技能存储")))
    memory = SimpleNamespace(manage=Mock(side_effect=AssertionError("不应进入记忆存储")))
    tools = LearningTools(skills, memory)
    with evidence_turn():
        _staged()
        request = SimpleNamespace(messages=[])
        assert ReviewSnapshot.capture(request, None) is None
        assert json.loads(await tools.skill_manage(operations=[]))["success"] is False
        assert json.loads(await tools.manage_memory(action="add", content="知识摘录"))["success"] is False
        assert not skills.manage.called and not memory.manage.called
    assert not evidence_in_use()
