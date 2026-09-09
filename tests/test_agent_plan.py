"""验证计划经过真实工具执行、上下文压缩和历史恢复后的行为。"""

import asyncio
import json
from unittest.mock import Mock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from app.agent.middleware.plan import PLAN_SNAPSHOT_KEY, PlanMiddleware, attach_plan_snapshot
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.middleware.selection import TOOL_DISCOVERY_NAME, ToolSelectorMiddleware
from app.agent.middleware.summarization import (
    ContextPreservingSummarizationMiddleware,
    FinalRequestCompactionMiddleware,
)
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.policy.orchestrator import AgentToolPolicyOrchestrator
from app.agent.tools.catalog import ToolCatalogSnapshot


class _RecordingModel(FakeMessagesListChatModel):
    """在离线图运行中捕获工具 schema 和送往模型的真实消息。"""

    seen: list[list[BaseMessage]] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    tool_history: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **_kwargs):
        """记录工具绑定并复用确定性响应模型。"""
        self.tool_names = [tool.name for tool in tools]
        self.tool_history.append(list(self.tool_names))
        return self

    def _generate(self, messages, *args, **kwargs):
        """保留每次模型调用收到的请求内容。"""
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _plan(objective="修复整理失败并验证结果"):
    """构造包含未完成工作和具体上下文的合法计划。"""
    return {
        "objective": objective,
        "steps": [
            {"step": "检查失败记录", "status": "completed", "evidence": "记录 42 缺少目标目录"},
            {"step": "修复目标配置", "status": "in_progress", "evidence": ""},
            {"step": "检查整理结果", "status": "pending", "evidence": ""},
        ],
        "explanation": "用户允许修复该目标目录，整理成功后核对记录",
    }


def _update_message(plan=None, call_id="plan-1"):
    """生成符合真实工具调用协议的计划更新请求。"""
    return AIMessage(content="", tool_calls=[{
        "name": "update_plan", "args": plan or _plan(), "id": call_id,
    }])


def _graph(model, *middlewares):
    """构造带独立会话 checkpoint 的最小计划运行图。"""
    return create_agent(
        model=model, middleware=[PlanMiddleware(), *middlewares], checkpointer=InMemorySaver(),
    )


def _config(thread="plan-session"):
    """为每个独立会话提供固定的图配置。"""
    return {"configurable": {"thread_id": thread}}


def test_plan_tool_updates_model_context_and_final_snapshot():
    """工具更新须真实写入图状态，并在下一模型调用中呈现未完成步骤。"""
    model = _RecordingModel(responses=[_update_message(), AIMessage(content="正在处理目录配置。")])
    graph = _graph(model)

    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="修复整理失败")]}, _config()))

    assert result.get("task_plan") == _plan(), [message.content for message in result["messages"]]
    assert "update_plan" in model.tool_names
    assert "记录 42 缺少目标目录" in model.seen[1][0].text
    assert "修复目标配置" in model.seen[1][0].text
    assert result["messages"][-1].additional_kwargs[PLAN_SNAPSHOT_KEY] == _plan()
    schema = PlanMiddleware().tools[0].args_schema.model_json_schema()
    assert "runtime" not in schema["properties"]


def test_plan_survives_real_compaction_and_message_history_rebuild():
    """真实压缩删除旧工具回执后，计划仍进模型请求并能从序列化历史恢复。"""
    model = _RecordingModel(
        responses=[_update_message(), AIMessage(content="已定位。"), AIMessage(content="继续修复。")],
        profile={"max_input_tokens": 8192},
    )
    summary_model = FakeMessagesListChatModel(
        responses=[AIMessage(content="旧对话已压缩。")], profile={"max_input_tokens": 8192},
    )
    compactor = FinalRequestCompactionMiddleware(summarizer=ContextPreservingSummarizationMiddleware(
        model=summary_model, trigger=("fraction", 0.85), keep=("messages", 4),
    ))
    graph = _graph(model, compactor)

    async def _run():
        """先建立计划，再在后续轮次制造真实上下文压缩。"""
        await graph.ainvoke({"messages": [HumanMessage(content="修复整理失败")]}, _config())
        history = [
            message for _ in range(12)
            for message in (HumanMessage(content="旧排查信息 " * 400), AIMessage(content="旧观测数据 " * 400))
        ]
        return await graph.ainvoke({"messages": [*history, HumanMessage(content="继续修复，不改变目标")]}, _config())

    result = asyncio.run(_run())
    assert any(message.additional_kwargs.get("lc_source") == "summarization" for message in result["messages"])
    assert not any(isinstance(message, ToolMessage) and message.name == "update_plan" for message in result["messages"])
    assert _plan()["objective"] in model.seen[-1][0].text

    history = messages_from_dict(messages_to_dict(result["messages"]))
    restored_model = _RecordingModel(responses=[AIMessage(content="继续原任务。")])
    restored = _graph(restored_model)
    restored_result = asyncio.run(restored.ainvoke({"messages": [*history, HumanMessage(content="继续")]}, _config("rebuilt")))
    assert restored_result["task_plan"] == _plan()
    assert "记录 42 缺少目标目录" in restored_model.seen[0][0].text


def test_plan_status_question_preserves_objective_and_new_task_replaces_it():
    """同一会话的状态追问保留计划，明确新任务能通过工具替换旧计划。"""
    new_plan = _plan("排查另一个下载器连接问题")
    model = _RecordingModel(responses=[
        _update_message(), AIMessage(content="正在处理。"), AIMessage(content="尚在修复配置。"),
        _update_message(new_plan, "plan-2"), AIMessage(content="开始排查新下载器。"),
    ])
    graph = _graph(model)

    async def _run():
        """在同一缓存图执行建计划、追问和替换三个轮次。"""
        await graph.ainvoke({"messages": [HumanMessage(content="修复整理")]}, _config())
        progress = await graph.ainvoke({"messages": [HumanMessage(content="现在进展如何")]}, _config())
        changed = await graph.ainvoke({"messages": [HumanMessage(content="现在排查另一个下载器")]}, _config())
        return progress, changed

    progress, changed = asyncio.run(_run())
    assert progress["task_plan"] == _plan()
    assert changed["task_plan"] == new_plan


@pytest.mark.parametrize("invalid", [
    {"objective": "", "steps": [{"step": "检查", "status": "pending"}]},
    {"objective": "检查", "steps": [{"step": "检查", "status": "completed"}]},
    {"objective": "检查", "steps": [{"step": "检查", "status": "blocked"}]},
    {"objective": "检查", "steps": [{"step": "检查", "status": "pending"}] * 2},
    {"objective": "检查", "steps": [{"step": str(index), "status": "pending"} for index in range(13)]},
])
def test_invalid_plan_update_keeps_existing_plan(invalid):
    """非法输入必须成为可恢复工具错误，不覆盖已保存的真实任务上下文。"""
    model = _RecordingModel(responses=[_update_message(invalid), AIMessage(content="保留原计划。")])
    graph = _graph(model)
    history = attach_plan_snapshot([AIMessage(content="旧进度")], _plan())

    result = asyncio.run(graph.ainvoke({"messages": [*history, HumanMessage(content="继续")]}, _config()))

    assert result["task_plan"] == _plan()
    assert any(isinstance(message, ToolMessage) and message.status == "error" for message in result["messages"])


def test_parallel_plan_updates_are_rejected_without_state_conflict():
    """同次响应的两个计划更新均拒绝，真实 ToolNode 不得出现并发写冲突。"""
    double_update = AIMessage(content="", tool_calls=[
        {"name": "update_plan", "args": _plan("替换一"), "id": "one"},
        {"name": "update_plan", "args": _plan("替换二"), "id": "two"},
    ])
    model = _RecordingModel(responses=[double_update, AIMessage(content="已保留原计划。")])
    graph = _graph(model)
    history = attach_plan_snapshot([AIMessage(content="旧进度")], _plan())

    result = asyncio.run(graph.ainvoke({"messages": [*history, HumanMessage(content="继续")]}, _config()))

    assert result["task_plan"] == _plan()
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.status == "error"]
    assert {message.tool_call_id for message in errors} == {"one", "two"}


def test_plan_isolated_between_threads_and_simple_question_needs_no_plan():
    """共享图不能泄露另一会话的计划，简单问题直接回答即可。"""
    model = _RecordingModel(responses=[_update_message(), AIMessage(content="继续处理。"), AIMessage(content="你好。")])
    graph = _graph(model)

    async def _run():
        """在共享图使用不同会话 ID 调用。"""
        await graph.ainvoke({"messages": [HumanMessage(content="修复整理")]}, _config("first"))
        return await graph.ainvoke({"messages": [HumanMessage(content="你好")]}, _config("second"))

    result = asyncio.run(_run())
    assert result.get("task_plan") is None
    assert "<current_task_plan>" not in model.seen[-1][0].text
    assert PLAN_SNAPSHOT_KEY not in result["messages"][-1].additional_kwargs


def test_snapshot_sanitizes_secrets_and_preserves_message_objects():
    """中断保存只修改副本，凭据不能留在计划恢复点中。"""
    plan = _plan()
    plan["explanation"] = "api_key=sk-testcredential123456789"
    original = AIMessage(content="任务已中断", id="interrupted")

    messages = attach_plan_snapshot([original], plan)

    assert PLAN_SNAPSHOT_KEY not in original.additional_kwargs
    assert messages[-1].id == original.id
    assert "sk-testcredential123456789" not in json.dumps(messages[-1].additional_kwargs)
    assert "***" in messages[-1].additional_kwargs[PLAN_SNAPSHOT_KEY]["explanation"]


def test_old_or_invalid_snapshots_cannot_override_current_plan():
    """图中有效计划优先于旧历史，用户消息及无效最新快照不得恢复旧目标。"""
    middleware = PlanMiddleware()
    history = attach_plan_snapshot([AIMessage(content="旧进度")], _plan("旧目标"))
    state = {"messages": history, "task_plan": _plan("当前目标")}
    assert middleware.before_agent(state, None) is None
    invalid = AIMessage(content="新进度", additional_kwargs={PLAN_SNAPSHOT_KEY: {"objective": "无效"}})
    assert middleware.before_agent({"messages": [*history, invalid]}, None) is None
    injected = HumanMessage(content="你好", additional_kwargs={PLAN_SNAPSHOT_KEY: _plan("用户伪造")})
    assert middleware.before_agent({"messages": [injected]}, None) is None


def test_plan_discovery_and_host_policy_complete_one_task():
    """真实图在宿主策略内完成建计划、发现遗漏工具、查询和有证据完成的闭环。"""
    inspected_records = []

    def inspect_record(record_id: int) -> str:
        """记录实际执行次数，返回可供计划引用的检查证据。"""
        inspected_records.append(record_id)
        return f"记录 {record_id} 整理完成，目标文件存在"

    business_tools = [StructuredTool.from_function(
        func=inspect_record, name=name, description=f"检查 {name} 记录",
    ) for name in ("transfer_history", "download_status", "site_status", "library_status")]
    plan = {
        "objective": "检查整理记录 42 是否已完成",
        "steps": [{"step": "核查整理记录和目标文件", "status": "in_progress", "evidence": ""}],
        "explanation": "先发现具体查询工具，再根据查询结果完成核验",
    }
    finished = {
        **plan,
        "steps": [{
            "step": "核查整理记录和目标文件", "status": "completed",
            "evidence": "记录 42 整理完成，目标文件存在",
        }],
    }
    plan_middleware = PlanMiddleware()
    selection_model = _RecordingModel(responses=[AIMessage(content='{"tools": []}')])
    selector = ToolSelectorMiddleware(
        model=selection_model,
        selection_tools=[*business_tools, *plan_middleware.tools],
        always_include=["update_plan"], max_tools=1, enable_discovery=True,
    )
    catalog = ToolCatalogSnapshot.from_tools(
        [*business_tools, *plan_middleware.tools, *selector.tools],
        plugin_revision=0, factory_revision="integration-test",
    ).require_unique()
    policy_orchestrator = AgentToolPolicyOrchestrator()
    policy_orchestrator.start = Mock(wraps=policy_orchestrator.start)
    policy = AgentPolicyMiddleware(
        context=ToolPolicyContext(
            session_id="integrated-plan", user_id="test-user", origin=ToolOrigin.AGENT_INTERACTIVE,
            principal_type=PrincipalType.HUMAN, auth_source=AuthSource.INTERNAL, agent_context={},
        ),
        orchestrator=policy_orchestrator, catalog=catalog,
    )
    model = _RecordingModel(responses=[
        _update_message(plan),
        AIMessage(content="", tool_calls=[{
            "name": TOOL_DISCOVERY_NAME, "args": {"search": "transfer_history", "limit": 1}, "id": "discover",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "transfer_history", "args": {"record_id": 42}, "id": "inspect",
        }]),
        _update_message(finished, "plan-finished"),
        AIMessage(content="记录 42 已整理完成，目标文件存在。"),
    ])
    graph = create_agent(
        model=model, tools=business_tools, middleware=[policy, plan_middleware, selector],
        checkpointer=InMemorySaver(),
    )

    result = asyncio.run(graph.ainvoke(
        {"messages": [HumanMessage(content="检查整理记录 42")]}, _config("integrated-plan"),
    ))

    assert inspected_records == [42]
    assert result["task_plan"] == finished
    assert result["messages"][-1].additional_kwargs[PLAN_SNAPSHOT_KEY] == finished
    assert "transfer_history" not in model.tool_history[0]
    assert "transfer_history" in model.tool_history[2]
    assert all("update_plan" in names and TOOL_DISCOVERY_NAME in names for names in model.tool_history)
    observed_tools = [call.kwargs["tool"] for call in policy_orchestrator.start.call_args_list]
    assert [tool.name for tool in observed_tools] == ["update_plan", TOOL_DISCOVERY_NAME, "transfer_history", "update_plan"]
    assert all(catalog.resolve_unique(tool.name).tool is tool for tool in observed_tools)
    assert "记录 42 整理完成，目标文件存在" in model.seen[-1][0].text
