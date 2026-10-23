"""
智能体运行时审查发现的真实任务、文件、历史和发现回归。
"""

import asyncio
import io
import json
import os
import shlex
import signal
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Barrier
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from app.agent.history.storage import SqliteRecallRepository
from app.agent.middleware.subagents import SubAgentTaskControlMiddleware
from app.agent.runtime import AgentRuntimeManager
from app.agent.tools.impl import _file_write_utils as writes
from app.application.messaging.recall import RecallMessage, RecallQuery, RecallSession


def test_runtime_disk_change_invalidates_loaded_persona(tmp_path):
    """
    外部改动人格文件后签名与实际缓存不能被提前写成同一版本。
    """
    manager = AgentRuntimeManager(agent_root_dir=tmp_path,
        bundled_defaults_dir=Path(__file__).parents[1] / "app/agent/defaults")
    manager._signature_check_interval = 0
    first = manager.load_runtime_config()
    target = first.persona.path
    target.write_text(target.read_text() + "\nREVIEW_CHANGED_PERSONA\n")
    second = manager.load_runtime_config()
    assert "REVIEW_CHANGED_PERSONA" in second.persona.text
    assert second is not first


def test_atomic_write_serializes_sha_compare_and_replace(tmp_path, monkeypatch):
    """
    两个线程同时通过旧SHA检查后，只允许一个版本成功提交。
    """
    target = tmp_path / "target.txt"
    target.write_text("old")
    previous = writes.calculate_file_sha256(target)
    rendezvous = Barrier(2)
    real_hash = writes.calculate_file_sha256

    def compared(path):
        """
        在真实哈希读取后同步，固定旧实现两个校验均成功的窗口。
        """
        digest = real_hash(path)
        try:
            rendezvous.wait(timeout=0.3)
        except Exception:
            pass
        return digest

    monkeypatch.setattr(writes, "calculate_file_sha256", compared)

    def update(value):
        """
        保留真实临时文件、fsync、版本核验和原子替换。
        """
        try:
            writes.atomic_write_text(target, value, previous)
            return "changed"
        except writes.FileVersionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, ["first", "second"]))
    assert sorted(outcomes) == ["changed", "conflict"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_concurrent_file_tools_do_not_overwrite_peer(tmp_path, monkeypatch, edit):
    """
    从真实工具入口同时完成读检查后，新建禁止覆盖和编辑SHA都只允许一个提交。
    """
    from app.agent.tools.impl.edit_file import EditFileTool
    from app.agent.tools.impl.write_file import WriteFileTool

    target = tmp_path / "file.txt"
    if edit:
        target.write_text("alpha beta")
    barrier = Barrier(2)
    actual_write = writes.atomic_write_text

    def write(*args, **kwargs):
        """
        在真实原子写入前加屏障，固定两个调用已通过读侧检查的窗口。
        """
        barrier.wait(timeout=5)
        return actual_write(*args, **kwargs)

    monkeypatch.setattr("app.agent.tools.impl.edit_file.atomic_write_text" if edit else
        "app.agent.tools.impl.write_file.atomic_write_text", write)
    tool = EditFileTool(session_id="review", user_id="review") if edit else WriteFileTool(session_id="review", user_id="review")
    tool.set_agent_context({"is_admin": True})
    calls = [tool.run(file_path=str(target), old_text=old, new_text=new)
             for old, new in [("alpha", "ALPHA"), ("beta", "BETA")]] if edit else [
        tool.run(file_path=str(target), content=value, overwrite=False) for value in ("first", "second")]
    results = await asyncio.gather(*calls)
    assert sum(result.startswith("成功") for result in results) == 1
    assert writes.calculate_file_sha256(target) in next(result for result in results if result.startswith("成功"))


def test_remote_session_clear_only_clears_current_channel_source(monkeypatch):
    """
    清除一个渠道接入不删除相同裸用户在另一接入的会话。
    """
    from datetime import datetime

    from app.chain.message import MessageChain
    from app.schemas.types import NotificationChannel

    monkeypatch.setattr(MessageChain, "_user_sessions", {})
    chain = MessageChain()
    monkeypatch.setattr(chain, "_schedule_agent_session_clear", lambda *_args: None)
    monkeypatch.setattr(chain, "post_message", lambda *_args: None)
    first = chain._session_user_key("same", NotificationChannel.Telegram, "one")
    second = chain._session_user_key("same", NotificationChannel.Telegram, "two")
    chain._user_sessions.update({first: ("first", datetime.now()), second: ("second", datetime.now())})
    chain.remote_clear_session(NotificationChannel.Telegram, "same", "one")
    assert first not in chain._user_sessions
    assert chain._user_sessions[second][0] == "second"


@pytest.mark.asyncio
async def test_finished_subagent_callback_keeps_each_identity():
    """
    同批两个子任务先后结束时，完成时间属于实际结束的任务。
    """
    middleware = SubAgentTaskControlMiddleware(model=None, profiles=(), tools=[])
    first, second = asyncio.Event(), asyncio.Event()

    async def provider(*, description, **_kwargs):
        """
        用独立事件代替远端模型，保留真实任务调度和完成回调。
        """
        await (first if description == "first" else second).wait()
        return description

    middleware._provider = SimpleNamespace(run_task=provider)
    try:
        result = json.loads(await middleware._control_task(action="start",
            tasks=[{"description": "first"}, {"description": "second"}]))
        records = [middleware._tasks[item["task_id"]] for item in result["tasks"]]
        first.set()
        await asyncio.wait_for(asyncio.shield(records[0].task), 2)
        await asyncio.sleep(0)
        assert records[0].finished_at is not None
        assert records[1].finished_at is None
    finally:
        first.set()
        second.set()
        await middleware.close()


@pytest.mark.asyncio
async def test_update_subagent_cannot_restart_after_owner_close():
    """
    更新等待旧任务取消时发生关闭，旧任务收敛后不能产生逃逸新任务。
    """
    middleware = SubAgentTaskControlMiddleware(model=None, profiles=(), tools=[])
    started, cancelling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    new_tasks = []

    async def provider(*, description, **_kwargs):
        """
        旧任务延迟确认取消，让关闭精确进入更新的等待窗口。
        """
        if description == "old":
            started.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    cancelling.set()
            return "old complete"
        new_tasks.append(asyncio.current_task())
        await finish.wait()
        return "new complete"

    middleware._provider = SimpleNamespace(run_task=provider)
    updating = closing = None
    try:
        result = json.loads(await middleware._control_task(action="start", description="old"))
        await started.wait()
        updating = asyncio.create_task(middleware._control_task(action="update",
            task_id=result["tasks"][0]["task_id"], description="new"))
        await cancelling.wait()
        closing = asyncio.create_task(middleware.close())
        await asyncio.sleep(0)
        finish.set()
        assert json.loads(await updating)["success"] is False
        assert await closing is True
        assert not new_tasks
    finally:
        finish.set()
        await asyncio.gather(*(task for task in (updating, closing) if task), return_exceptions=True)
        await asyncio.gather(*new_tasks, return_exceptions=True)
        await middleware.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated_cursor", [False, True])
async def test_mcp_discovery_consumes_next_cursor(monkeypatch, repeated_cursor):
    """
    同一真实MCP会话依次发现各页工具，不把首屏当成完整目录。
    """
    from app.agent.mcp import AgentMcpManager
    from app.schemas.agent import AgentMcpServerConfig

    requests = []

    async def request(method, params=None):
        """
        只替换远端响应，保留初始化和工具投影。
        """
        if method == "initialize":
            return {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
        requests.append(params)
        if params and params.get("cursor") == "page-two":
            return {"tools": [{"name": "second"}], "nextCursor": "page-two" if repeated_cursor else None}
        return {"tools": [{"name": "first"}], "nextCursor": "page-two"}

    @asynccontextmanager
    async def session(_server):
        """
        会话只持有本次测试的调用记录。
        """
        yield SimpleNamespace(request=request, notify=AsyncMock())

    async def open_session(server):
        """
        返回隔离的远端会话，保持正式发现入口的异步打开协议。
        """
        return session(server)

    monkeypatch.setattr("app.agent.mcp._open_mcp_session", open_session)
    server = AgentMcpServerConfig(id="review", name="review", transport="http", url="https://review.invalid/mcp")
    if repeated_cursor:
        with pytest.raises(ValueError, match="重复"):
            await AgentMcpManager().list_server_tools(server)
        assert len(requests) == 2
        return
    tools = await AgentMcpManager().list_server_tools(server)
    assert [tool.name for tool in tools] == ["first", "second"]
    assert requests == [None, {"cursor": "page-two"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated_cursor", [False, True])
async def test_google_model_discovery_consumes_real_sdk_pager(monkeypatch, repeated_cursor):
    """
    使用真实SDK分页载具验证第二页模型确实由应用消费。
    """
    from google.genai import pagers, types

    from app.agent.llm.provider import LLMProviderManager

    first = types.ListModelsResponse(models=[types.Model(name="models/first", display_name="First",
        supported_actions=["generateContent"])], next_page_token="second")
    second = types.ListModelsResponse(models=[types.Model(name="models/second", display_name="Second",
        supported_actions=["generateContent"])], next_page_token="second" if repeated_cursor else None)
    request = AsyncMock(return_value=second)
    pager = pagers.AsyncPager("models", request, first, {})
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(list=AsyncMock(return_value=pager)),
        aclose=AsyncMock()), close=lambda: None)
    monkeypatch.setattr("google.genai.Client", lambda **_kwargs: client)
    manager = LLMProviderManager()
    monkeypatch.setattr(manager, "_models_dev_model", AsyncMock(return_value={}))
    if repeated_cursor:
        with pytest.raises(RuntimeError, match="重复"):
            await manager._list_models_from_google("fictional", use_proxy=False)
        assert request.await_count == 1
        client.aio.aclose.assert_awaited_once()
        return
    result = await manager._list_models_from_google("fictional", use_proxy=False)
    assert [model["id"] for model in result] == ["models/first", "models/second"]
    assert request.await_count == 1
    client.aio.aclose.assert_awaited_once()


def test_recall_browse_applies_time_exclusion_and_sort(tmp_path):
    """
    真实独立SQLite浏览执行开始时间范围、排除列表和排序。
    """
    repository = SqliteRecallRepository(tmp_path)
    for identity, timestamp in [("old", 100), ("in-range", 200), ("new", 300)]:
        repository.append("review", RecallSession(identity, title=identity, started_at=timestamp,
            last_active=timestamp), (RecallMessage(identity, "user", identity, timestamp=timestamp),))
    ranged = repository.search("review", RecallQuery(after=150, before=250))
    assert [item["session_id"] for item in ranged["results"]] == ["in-range"]
    excluded = repository.search("review", RecallQuery(exclude_session_ids=("new",)))
    assert "new" not in [item["session_id"] for item in excluded["results"]]
    oldest = repository.search("review", RecallQuery(sort="oldest"))
    assert oldest["results"][0]["session_id"] == "old"


def test_market_skill_install_preserves_explicit_branch(tmp_path, monkeypatch):
    """
    显式release分支的市场展示和实际下载使用同一分支及来源记录。
    """
    from app.agent.skills.registry import SkillHelper

    requests = []

    def request(**kwargs):
        """
        用不同真实ZIP内容区分默认分支和选定分支。
        """
        branch = kwargs["url"].rsplit("/", 1)[-1]
        requests.append(branch)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as package:
            package.writestr(f"repo-{branch}/skills/review-demo/SKILL.md",
                f"---\nname: review-demo\ndescription: review\nversion: 2\n---\n{branch} contents\n")
        return SimpleNamespace(status_code=200, content=stream.getvalue())

    monkeypatch.setattr(SkillHelper, "_request_github", staticmethod(request))
    monkeypatch.setattr(SkillHelper, "get_user_skills_dir", staticmethod(lambda: tmp_path))
    helper = SkillHelper()
    skills = SkillHelper._list_market_repo_skills.__wrapped__(helper,
        "https://github.com/example/review/tree/release/skills")
    success, message = helper.install_market_skill(skills[0])
    assert success, message
    assert requests == ["release", "release"]
    assert "release contents" in (tmp_path / "review-demo/SKILL.md").read_text()
    assert json.loads((tmp_path / "review-demo/.moviepilot-skill-source.json").read_text())["branch"] == "release"


def test_explicit_market_branch_failure_does_not_install_default(monkeypatch):
    """
    指定分支不可用时受控失败，不悄悄改装默认分支内容。
    """
    from app.agent.skills.registry import SkillHelper

    requests = []

    def request(**kwargs):
        """
        记录明确分支的下载请求并模拟该分支不存在。
        """
        requests.append(kwargs["url"])
        return SimpleNamespace(status_code=404)

    monkeypatch.setattr(SkillHelper, "_request_github", staticmethod(request))
    helper = SkillHelper()
    repo = helper._parse_market_repo("https://github.com/example/review/tree/release/skills")
    assert helper._download_repo_archive(repo) is None
    assert len(requests) == 1 and requests[0].endswith("/release")


@pytest.mark.asyncio
async def test_nullable_tool_boolean_preserves_none():
    """
    工具规范化不能把合法Optional布尔的null变成启用。
    """
    from app.agent.tools.manager import MoviePilotToolsManager

    assert MoviePilotToolsManager._normalize_scalar_value("boolean", None, "enabled") is None


@pytest.mark.asyncio
async def test_mcp_null_task_update_does_not_enable_paused_task(monkeypatch):
    """
    真实MCP公开入口将enabled=null传到任务更新服务时保留暂停状态。
    """
    from app.agent.tools.impl.agent_task import AgentTaskTool
    from app.agent.tools.manager import MoviePilotToolsManager
    from app.api.endpoints import mcp

    task = SimpleNamespace(id=1, last_status="waiting", enabled=False, trigger_type="cron",
        cron_expression="0 0 * * *", run_at=None, name="paused")
    payloads = []

    def update(*, payload, **_kwargs):
        """
        仅替换持久化边界，记录真实工具生成的写入字段。
        """
        payloads.append(dict(payload))
        for key, value in payload.items():
            setattr(task, key, value)
        return True

    repository = SimpleNamespace(get=lambda **_kwargs: task, update=update)
    tool = AgentTaskTool(session_id="review", user_id="1", data=SimpleNamespace(tasks=repository))
    tool.set_agent_context({"is_admin": True})
    manager = MoviePilotToolsManager(user_id="1", session_id="review")
    manager.tools = [tool]
    monkeypatch.setattr(mcp, "moviepilot_tool_manager", manager)
    monkeypatch.setattr("app.application.scheduling.update_agent_task_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.agent.tools.impl.agent_task.agent_task_to_dict",
        lambda item, **_kwargs: {"id": item.id, "name": item.name, "enabled": item.enabled})
    try:
        response = await mcp.handle_tools_call({"name": "agent_task", "arguments": {
            "action": "update", "task_id": 1, "name": "renamed", "enabled": None,
        }})
        assert response.get("isError") is not True
        assert task.name == "renamed" and task.enabled is False
        assert "enabled" not in payloads[-1]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_production_data_context_keeps_config_reconciliation(monkeypatch):
    """
    正式组合根发布数据上下文后，关闭开关仍会协调真实能力owner。
    """
    from app.startup.composition.agent import publish_agent_services
    from app.startup.initializers import agent as module

    manager = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock(return_value=True))
    initializer = module.AgentInitializer()
    composition = SimpleNamespace(data=SimpleNamespace(chat=object(), chat_persistence=object()), execution=object())
    monkeypatch.setattr(module, "agent_manager", None)
    monkeypatch.setattr(module, "_agent_data_context", None)
    monkeypatch.setattr(module, "_injected_agent_manager", None)
    monkeypatch.setattr(module, "_get_injected_agent_manager", lambda: manager)
    monkeypatch.setattr(module, "_get_agent_manager", lambda: manager)
    monkeypatch.setattr(module, "get_runtime_setting", lambda _key: True)
    monkeypatch.setattr(module, "activate_agent_service", AsyncMock(return_value=manager))
    reconcile = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "reconcile_agent_service", reconcile)
    for name in ("configure_agent_chat_service", "configure_agent_chat_persistence", "configure_agent_task_execution"):
        monkeypatch.setattr("app.startup.composition.agent." + name, lambda *_args: None)
    publish_agent_services(composition, data_context_registrar=module.configure_agent_data_context)
    assert await initializer.initialize()
    await initializer.handle_config_changed(SimpleNamespace(event_data={"key": {"AI_AGENT_ENABLE"}}))
    reconcile.assert_awaited_once()
    assert initializer._manager is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_enabled", [False, True])
async def test_production_data_context_uses_one_real_capability_manager(monkeypatch, initially_enabled):
    """
    正式组合根、真实能力owner和真实AgentManager共享同一实例，关闭配置收敛后台任务。
    """
    from app.agent import loader
    from app.agent.manager import AgentManager
    from app.startup.composition.agent import publish_agent_services
    from app.startup.initializers import agent as module

    memory = SimpleNamespace(configure=Mock(), initialize=Mock(), close=AsyncMock())
    manager = AgentManager(memory=memory)
    enabled = {"value": initially_enabled}
    monkeypatch.setattr(loader, "_agent_runtime", None)
    monkeypatch.setattr("app.agent.manager.agent_manager", manager)
    monkeypatch.setattr(module, "agent_manager", None)
    monkeypatch.setattr(module, "_agent_data_context", None)
    monkeypatch.setattr(module, "_injected_agent_manager", None)
    monkeypatch.setattr(module, "get_runtime_setting", lambda _key: enabled["value"])
    monkeypatch.setattr("app.agent.capabilities.adapter.get_runtime_setting", lambda _key: enabled["value"])
    initializer = module.AgentInitializer()
    monkeypatch.setattr(module, "agent_initializer", initializer)
    context = SimpleNamespace(chat=object(), chat_persistence=object())
    for name in ("configure_agent_chat_service", "configure_agent_chat_persistence", "configure_agent_task_execution"):
        monkeypatch.setattr("app.startup.composition.agent." + name, lambda *_args: None)
    publish_agent_services(SimpleNamespace(data=context, execution=object()),
        data_context_registrar=module.configure_agent_data_context)
    try:
        assert await initializer.initialize()
        if not initially_enabled:
            assert module._get_running_agent_manager() is None
            assert not manager._accepting_tasks
            enabled["value"] = True
            await initializer.handle_config_changed(SimpleNamespace(event_data={"key": {"AI_AGENT_ENABLE"}}))
        assert initializer._compat_injected is False
        assert module._get_running_agent_manager() is manager
        assert module._get_agent_manager() is manager
        assert manager._data is context
        assert manager._accepting_tasks
        enabled["value"] = False
        await initializer.handle_config_changed(SimpleNamespace(event_data={"key": {"AI_AGENT_ENABLE"}}))
        assert module._get_running_agent_manager() is None
        assert not manager._accepting_tasks
        assert manager._idle_cleanup_task is None
        memory.close.assert_awaited_once()
    finally:
        await loader.begin_agent_shutdown()
        await initializer.cleanup()
        await manager.close()


@pytest.mark.asyncio
async def test_message_ingress_separates_channels_and_sources(monkeypatch):
    """
    真实消息入口按渠道和接入源隔离会话，同一入口连续消息仍复用。
    """
    from app.chain.message import MessageChain
    from app.schemas.types import NotificationChannel

    received, submitted = [], []

    async def process(**kwargs):
        """
        记录消息入口交给真实会话路由的身份与内容。
        """
        received.append(kwargs)

    ingress = SimpleNamespace(_ai_prefix="/ai", _user_sessions={}, _session_timeout_minutes=1440,
        runtime_config=SimpleNamespace(ai_agent_enable=True),
        _schedule_agent_session_clear=lambda *_args: None, _has_ai_prefix=lambda text: text.startswith("/ai"),
        _prepare_agent_files=lambda **_kwargs: [], post_message=lambda *_args, **_kwargs: None)
    for name in ("_message_session_service", "_get_or_create_session_id", "_bind_session_id", "_handle_ai_message"):
        setattr(ingress, name, MethodType(getattr(MessageChain, name), ingress))
    if hasattr(MessageChain, "_session_user_key"):
        ingress._session_user_key = MessageChain._session_user_key
    original_submit = asyncio.run_coroutine_threadsafe

    def submit(coroutine, loop):
        """
        仅记录跨线程提交回执，实际协程继续在测试事件循环执行。
        """
        future = original_submit(coroutine, loop)
        submitted.append(future)
        return future

    monkeypatch.setattr("app.chain.message.get_running_agent_manager", lambda: SimpleNamespace(process_message=process))
    monkeypatch.setattr("app.chain.message.main_loop_registry.require", asyncio.get_running_loop)
    monkeypatch.setattr("app.chain.message.asyncio.run_coroutine_threadsafe", submit)
    for channel, source in [(NotificationChannel.Telegram, "one"), (NotificationChannel.Feishu, "one"),
                            (NotificationChannel.Telegram, "two"), (NotificationChannel.Telegram, "one")]:
        assert ingress._handle_ai_message("/ai review", channel, source, "same", "review")
        await asyncio.wrap_future(submitted[-1])
    identities = [item["session_id"] for item in received]
    assert len(set(identities[:3])) == 3
    assert identities[0] == identities[3]


@pytest.mark.asyncio
async def test_message_namespace_separates_real_agent_memory(monkeypatch):
    """
    真实会话owner和记忆读取链不向同裸用户的另一渠道提供前一渠道消息。
    """
    from langchain_core.messages import AIMessage

    from app.agent.manager import AgentManager
    from app.agent.memory import MemoryManager
    from app.agent.orchestrator import MoviePilotAgent
    from app.chain.message import MessageChain
    from app.schemas.types import NotificationChannel

    port = SimpleNamespace(get=AsyncMock(return_value=None), async_save_agent_messages=AsyncMock(),
        async_append_display_messages=AsyncMock())
    memory = MemoryManager(chat=port, persistence=port)
    manager = AgentManager(memory=memory)
    manager._accepting_tasks = True
    calls, submitted = [], []
    ingress = SimpleNamespace(_ai_prefix="/ai", _user_sessions={}, _session_timeout_minutes=1440,
        runtime_config=SimpleNamespace(ai_agent_enable=True),
        _schedule_agent_session_clear=lambda *_args: None, _has_ai_prefix=lambda text: text.startswith("/ai"),
        _prepare_agent_files=lambda **_kwargs: [], post_message=lambda *_args, **_kwargs: None,
        _session_user_key=MessageChain._session_user_key)
    for name in ("_message_session_service", "_get_or_create_session_id", "_bind_session_id", "_handle_ai_message"):
        setattr(ingress, name, MethodType(getattr(MessageChain, name), ingress))

    async def graph(agent, streaming=False):
        """
        只在模型图边界提供确定响应，真实记忆读写和消息owner继续执行。
        """
        del streaming
        state = {}
        agent._last_agent_cache_hit = False

        async def invoke(payload, config):
            """
            为正式记忆与会话流程提供确定的模型回复，并保留已传入的上下文。
            """
            del config
            calls.append(json.dumps([message.content for message in payload["messages"]], ensure_ascii=False))
            state["messages"] = [*payload["messages"], AIMessage(content="review reply")]

        return SimpleNamespace(ainvoke=invoke, get_state=lambda _config: SimpleNamespace(values=state))

    original_submit = asyncio.run_coroutine_threadsafe

    def submit(coroutine, loop):
        """
        保留跨线程提交回执，供测试等待真实消息流程完成。
        """
        future = original_submit(coroutine, loop)
        submitted.append(future)
        return future

    monkeypatch.setattr("app.chain.message.get_running_agent_manager", lambda: manager)
    monkeypatch.setattr("app.chain.message.main_loop_registry.require", asyncio.get_running_loop)
    monkeypatch.setattr("app.chain.message.asyncio.run_coroutine_threadsafe", submit)
    monkeypatch.setattr("app.agent.session._async_start_processing_status", AsyncMock(return_value=None))
    monkeypatch.setattr("app.agent.orchestrator.get_configured_agent_chat_persistence", lambda: port)
    monkeypatch.setattr(MoviePilotAgent, "_create_agent", graph)
    monkeypatch.setattr(MoviePilotAgent, "prepare_chat_title", AsyncMock())
    monkeypatch.setattr(MoviePilotAgent, "_learning_enabled", lambda _self: False)
    monkeypatch.setattr(MoviePilotAgent, "_should_stream", lambda _self: False)
    monkeypatch.setattr(MoviePilotAgent, "send_agent_message", AsyncMock())
    try:
        for text, channel, source in [("first private", NotificationChannel.Telegram, "one"),
                                      ("second private", NotificationChannel.Feishu, "two"),
                                      ("third private", NotificationChannel.Telegram, "one")]:
            assert ingress._handle_ai_message("/ai " + text, channel, source, "same", "review")
            await asyncio.wrap_future(submitted[-1])
            key = ingress._session_user_key("same", channel, source)
            session_id = ingress._user_sessions[key][0]
            await manager._session_queues[session_id].join()
        assert len(manager.active_agents) == 2
        assert "first private" not in calls[1]
        assert "second private" in calls[1]
        assert "first private" in calls[2]
        assert "third private" in calls[2]
        assert "second private" not in calls[2]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_webagent_background_turns_preserve_display_history(tmp_path, monkeypatch):
    """
    两轮真实SQLite后台快照保存完整展示历史，不依赖前端二次上传。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.application.messaging import agent as application
    from app.application.messaging.chat import AgentChatPersistenceService, AgentChatService
    from app.application.messaging.webagent.stream import WebAgentStreamDependencies, build_agent_web_agent_stream
    from app.db.models.agentchat import AgentChat
    from app.db.oper.agentchat import AgentChatOper

    engine = create_engine(f"sqlite:///{tmp_path / 'chat.db'}")
    AgentChat.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    async def get(session_id, user_id=None):
        """
        每次查询使用真实短会话。
        """
        with factory() as session:
            return AgentChatOper(session).get(session_id, user_id)

    async def run(operation):
        """
        在隔离数据库测试中直接执行持久化服务提交的同步操作。
        """
        return operation()

    def transaction(operation):
        """
        每轮实际提交独立的临时数据库事务。
        """
        with factory() as session:
            result = operation(session)
            session.commit()
            return result

    async def process(**kwargs):
        """
        通过正式输出回调提供本轮回复，触发展示历史的追加保存。
        """
        kwargs["output_callback"]("reply to " + kwargs["message"])

    service = AgentChatService(SimpleNamespace(async_get=get))
    persistence = AgentChatPersistenceService(AgentChatOper, SimpleNamespace(run=run), transaction)
    tasks = []

    def create_task(coroutine):
        """
        记录后台任务句柄，确保两轮历史写入都完成后再检查数据库。
        """
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    dependencies = WebAgentStreamDependencies(event_publisher_factory=application.WebAgentEventPublisher,
        bind_user_session=lambda *_args: None, apply_display_event=application.apply_web_agent_display_event,
        build_display_message=application.build_web_agent_display_message,
        build_input_attachments=application.build_web_agent_input_attachments,
        build_message_events_async=application.build_web_agent_message_events_async,
        save_display_snapshot=application.save_web_agent_display_snapshot,
        split_output=application.split_web_agent_output, create_background_task=create_task,
        source="web-agent", heartbeat_seconds=1)
    monkeypatch.setattr(application.agent_application, "get_running_agent_manager", lambda: SimpleNamespace(process_message=process))
    monkeypatch.setattr(application.agent_application, "get_web_agent_type", lambda: object)
    user = SimpleNamespace(id=1, name="review", is_superuser=True)
    try:
        for text in ("first", "second"):
            stream = build_agent_web_agent_stream(command=application.WebAgentStreamCommand(text=text,
                session_id="review"), current_user=user, session_id="review", prompt=text, display_prompt=text,
                has_audio_input=False, is_secret_confirmation_control=False, protected_transport_supported=False,
                service=service, persistence=persistence, is_disconnected=AsyncMock(return_value=False),
                dependencies=dependencies)
            async for _event in stream:
                pass
            await asyncio.gather(*tasks)
            tasks.clear()
        record = await service.get("review")
        assert [message["content"] for message in record.messages] == [
            "first", "reply to first", "second", "reply to second",
        ]
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        engine.dispose()


@pytest.mark.asyncio
async def test_view_image_uses_read_permission_for_public_skill(tmp_path, monkeypatch):
    """
    普通用户读取公共技能图片沿用读取门禁，写入权限保持由基类决定。
    """
    from app.agent.tools.impl.view_image import ViewImageTool

    target = tmp_path / "skills/review/reference.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2)).save(target)
    monkeypatch.setattr(ViewImageTool, "_get_non_admin_local_file_roots", classmethod(lambda _cls: [tmp_path]))
    tool = ViewImageTool(session_id="review", user_id="review")
    tool.set_agent_context({"is_admin": False})
    result = json.loads(await tool.run(file_path=str(target)))
    assert result["success"] is True
    _path, write_error = await tool._check_local_file_access(str(target), operation="写入")
    assert write_error is not None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="此场景依赖POSIX独立进程组")
@pytest.mark.parametrize("retention_expired", [False, True])
async def test_terminal_close_cleans_descendant_after_leader_exit(tmp_path, retention_expired):
    """
    仅测试所创建进程组：组长先退出，关闭owner仍结束存活后代。
    """
    import psutil

    from app.agent.terminal.manager import _TerminalSessionManager
    from app.agent.terminal.ownership import TerminalScope, bind_terminal_scope

    manager = _TerminalSessionManager()
    owner = TerminalScope("review", "review-descendant", "test")
    pidfile = tmp_path / "child.pid"
    child = "import time; time.sleep(30)"
    parent = ("import subprocess,sys,pathlib; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))")
    pid = None
    try:
        with bind_terminal_scope(owner):
            result = await manager.start(command=shlex.join([sys.executable, "-c", parent]),
                use_pty=False, yield_time_ms=0, shell="/bin/sh", login=False)
            session = manager._sessions[result["session_id"]]
            await asyncio.wait_for(asyncio.shield(session.wait_task), 5)
            pid = int(pidfile.read_text())
            assert os.getpgid(pid) == session.pid
            if retention_expired:
                session.updated_at = 0
                manager._cleanup_finished_sessions_locked()
                assert session.session_id in manager._sessions
            assert await manager.close_owner(owner)
            async with asyncio.timeout(2):
                while True:
                    try:
                        status = psutil.Process(pid).status()
                    except psutil.NoSuchProcess:
                        break
                    if status == psutil.STATUS_ZOMBIE:
                        break
                    await asyncio.sleep(0.02)
            assert not manager._sessions
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await manager.close()
