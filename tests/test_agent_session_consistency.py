"""
选择回执、执行状态和展示快照的一致性回归。
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.agent.session import AgentSessionOwner, _MessageTask
from app.application.messaging import agent as web_agent
from app.application.messaging.interaction.agent import (
    AgentInteractionOption,
    agent_interaction_manager,
    build_agent_choice_callback,
)
from app.db.oper.agentchat import AgentChatOper
from app.db.session import SessionFactory
from app.domain.chat import merge_choice_state
from app.schemas.types import NotificationChannel


@pytest.fixture(autouse=True)
def clear_choices():
    """
    隔离进程内选择收据，不影响其它会话测试。
    """
    agent_interaction_manager.clear()
    yield
    agent_interaction_manager.clear()


@pytest.fixture
def choice():
    """
    在临时数据库中建立带待选卡片的用户会话。
    """
    session_id = f"web-agent:consistency-{uuid4().hex}"
    user = SimpleNamespace(id=501, name="choice-user", is_superuser=False)
    request = agent_interaction_manager.create_request(
        session_id=session_id, user_id=str(user.id), channel=NotificationChannel.WebAgent.value,
        source="web-agent", username=user.name, title="请选择", prompt="选择分类",
        options=[AgentInteractionOption("电影", "选择电影"), AgentInteractionOption("电视剧", "选择电视剧")],
    )
    messages = [{"id": "assistant-choice", "role": "assistant", "content": "", "choices": [{
        "id": request.request_id, "status": "pending", "prompt": request.prompt,
    }]}]
    with SessionFactory() as db:
        AgentChatOper(db).save_display_messages(session_id=session_id, user_id=str(user.id), messages=messages)
        db.commit()
    return request, user, messages


@pytest.mark.asyncio
async def test_choice_callback_persists_first_selection_and_replays_without_changing_option(choice):
    """
    回调返回前已落库；重复点击其它按钮仍返回首次选择且不新增回显。
    """
    request, user, _ = choice
    first = await web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 1), user)
    repeated = await web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 2), user)
    assert first["already_selected"] is False
    assert repeated["already_selected"] is False
    assert repeated["message"] == first["message"] == "选择电影"
    assert repeated["feedback"] == first["feedback"]
    assert agent_interaction_manager.claim_web_choice(request.request_id, str(user.id), request.session_id) is not None
    executed = await web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 2), user)
    assert executed["already_selected"] is True and executed["message"] == first["message"]
    with SessionFactory() as db:
        row = AgentChatOper(db).get(request.session_id)
        assert row.display_messages[0]["choices"][0]["status"] == "selected"
        assert row.display_messages[0]["choices"][0]["selected_value"] == "选择电影"
        receipts = [message for message in row.display_messages if message.get("choice_selection")]
        assert len(receipts) == 1 and receipts[0]["content"] == ""


@pytest.mark.asyncio
async def test_choice_rejects_other_user_and_reassigned_session_without_consuming(choice):
    """
    选择收据与持久会话授权同时校验，越权不能读出或消费原选项。
    """
    request, user, _ = choice
    callback = build_agent_choice_callback(request.request_id, 1)
    assert await web_agent.resolve_and_save_web_agent_choice(callback, SimpleNamespace(id=502)) is None
    service = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(user_id="502", username="other")))
    assert await web_agent.resolve_and_save_web_agent_choice(callback, user, service=service) is None
    assert request.selected_index is None


@pytest.mark.asyncio
async def test_choice_write_failure_does_not_claim_execution_and_can_retry(choice, monkeypatch):
    """
    持久化失败原样报错，Agent 不会领取未落库的选择。
    """
    request, user, _ = choice
    callback = build_agent_choice_callback(request.request_id, 1)
    with monkeypatch.context() as patcher:
        patcher.setattr(web_agent, "save_web_agent_display_snapshot", AsyncMock(side_effect=RuntimeError("write failed")))
        with pytest.raises(RuntimeError, match="write failed"):
            await web_agent.resolve_and_save_web_agent_choice(callback, user)
    assert agent_interaction_manager.claim_web_choice(request.request_id, str(user.id), request.session_id) is None
    result = await web_agent.resolve_and_save_web_agent_choice(callback, user)
    assert result["already_selected"] is False


@pytest.mark.asyncio
async def test_choice_stream_claim_is_once_only_and_uses_server_selection(choice, monkeypatch):
    """
    即使重复直接提交流请求，选择业务也只准入一次，正文采用服务端原选项。
    """
    request, user, _ = choice
    resolved = await web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 1), user)
    manager = SimpleNamespace(submit_steering_message=AsyncMock(return_value=None), process_message=AsyncMock(return_value="完成"))
    monkeypatch.setattr(web_agent.agent_application, "get_running_agent_manager", lambda: manager)
    monkeypatch.setattr(web_agent.agent_application, "get_web_agent_type", lambda: object)
    monkeypatch.setattr(web_agent, "get_api_runtime_config_snapshot", lambda: SimpleNamespace(ai_agent_enable=True))
    monkeypatch.setattr(web_agent, "bind_web_agent_user_session", Mock())
    save = AsyncMock()
    monkeypatch.setattr(web_agent, "save_web_agent_display_snapshot", save)
    command = web_agent.WebAgentStreamCommand(
        text="客户端改写正文", session_id=request.session_id, echo_user=False,
        choice_selection=resolved["choice_selection"],
    )
    first = await web_agent.build_web_agent_stream(command, current_user=user,
        is_disconnected=AsyncMock(return_value=False), protected_transport_supported=False)
    assert [event["type"] async for event in first.events] == ["start", "done"]
    second = await web_agent.build_web_agent_stream(command, current_user=user,
        is_disconnected=AsyncMock(return_value=False), protected_transport_supported=False)
    assert "error" in [event["type"] async for event in second.events]
    await web_agent.wait_web_agent_background_tasks()
    manager.process_message.assert_awaited_once()
    assert manager.process_message.await_args.kwargs["message"] == "选择电影"
    receipt = save.await_args.kwargs["messages"][0]
    assert receipt["content"] == "" and receipt["choice_selection"]["selected_value"] == "选择电影"
    assert agent_interaction_manager.claim_web_choice(request.request_id, str(user.id), "another-session") is None


def test_selected_card_survives_old_pending_snapshot_without_mutating_inputs(choice):
    """
    旧客户端替换快照时保留首次已选结果，输入对象保持不变。
    """
    request, _, original = choice
    selected = {"choice_id": request.request_id, "selected_label": "电影", "selected_value": "选择电影"}
    previous = merge_choice_state([], [*original, {"id": "receipt", "choice_selection": selected}])
    before_card = merge_choice_state(previous, [])
    assert before_card == [{
        "id": f"choice-selection-{request.request_id}", "role": "user", "content": "", "choice_selection": selected,
    }]
    result = merge_choice_state(before_card, original)
    assert result[0]["choices"][0]["status"] == "selected"
    assert original[0]["choices"][0]["status"] == "pending"
    assert merge_choice_state(result, [{"choice_selection": selected}])[0]["choice_selection"]["choice_id"] == request.request_id


@pytest.mark.asyncio
async def test_idle_worker_is_not_publicly_processing_but_still_protects_lifecycle():
    """
    空闲 worker 不显示执行中，仍阻止生命周期提前释放会话。
    """
    owner = AgentSessionOwner(memory=Mock())
    session_id = "idle-status"
    worker = asyncio.create_task(asyncio.Event().wait())
    owner._session_workers[session_id] = worker
    owner._session_last_used[session_id] = ("1", datetime.now() - timedelta(days=2))
    queue = asyncio.Queue()
    owner._session_queues[session_id] = queue
    task = _MessageTask(session_id=session_id, user_id="1", message="工作")
    try:
        assert owner.get_session_status(session_id)["is_processing"] is False
        assert owner.is_session_busy(session_id) is False
        assert owner._is_session_busy(session_id) is True
        assert owner._expired_idle_sessions() == []
        queue.put_nowait(task)
        assert owner.is_session_busy(session_id) is True
        queue.get_nowait()
        owner._session_active_tasks[session_id] = task
        assert owner.get_session_status(session_id)["is_processing"] is True
        owner._session_active_tasks.clear()
        assert owner.is_session_busy(session_id) is False
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def test_legacy_tool_hint_does_not_complete_structured_parallel_tool():
    """
    旧文本提示不会提前收口仍在运行的结构化工具。
    """
    message = web_agent.build_web_agent_display_message(role="assistant", status="streaming")
    web_agent.apply_web_agent_display_event({"type": "tool", "tool_id": "active", "status": "running"}, message)
    web_agent.apply_web_agent_display_event({"type": "tool", "message": "旧提示"}, message)
    assert message["tools"][0]["status"] == "running"
    web_agent.apply_web_agent_display_event({"type": "done"}, message)
    assert [tool["status"] for tool in message["tools"]] == ["error", "done"]


def test_database_replacement_cannot_restore_selected_card_to_pending(choice):
    """
    持久化旧完整快照后重新读取，已选卡片仍保留首次选择。
    """
    request, user, original = choice
    with SessionFactory() as db:
        oper = AgentChatOper(db)
        oper.append_display_messages(session_id=request.session_id, user_id=str(user.id), messages=[{
            "id": "receipt", "choice_selection": {
                "choice_id": request.request_id, "selected_label": "电影", "selected_value": "选择电影",
            },
        }])
        db.commit()
    with SessionFactory() as db:
        oper = AgentChatOper(db)
        oper.save_display_messages(session_id=request.session_id, user_id=str(user.id), messages=[])
        db.commit()
    with SessionFactory() as db:
        oper = AgentChatOper(db)
        oper.save_display_messages(session_id=request.session_id, user_id=str(user.id), messages=original)
        db.commit()
    with SessionFactory() as db:
        row = AgentChatOper(db).get(request.session_id)
        assert row.display_messages[0]["choices"][0]["status"] == "selected"
        assert row.display_messages[0]["choices"][0]["selected_value"] == "选择电影"


@pytest.mark.asyncio
async def test_concurrent_choice_write_failure_keeps_first_option_and_successful_receipt(choice, monkeypatch):
    """
    并发回调中一笔写失败不重置另一笔在途选择，首次选项保持不变。
    """
    request, user, _ = choice
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def save(**_kwargs):
        """
        第一笔写入等待后失败，第二笔确认成功。
        """
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("first write failed")

    monkeypatch.setattr(web_agent, "save_web_agent_display_snapshot", save)
    first = asyncio.create_task(web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 1), user))
    await entered.wait()
    second = await web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 2), user)
    release.set()
    with pytest.raises(RuntimeError, match="first write failed"):
        await first
    assert second["message"] == "选择电影" and second["already_selected"] is False
    assert request.selected_index == 1 and request.selection_persisted is True
    assert agent_interaction_manager.claim_web_choice(request.request_id, str(user.id), request.session_id) is not None


@pytest.mark.asyncio
async def test_stream_construction_failure_does_not_consume_choice_execution(choice, monkeypatch):
    """
    回调后断网或流构建失败，原选择仍可提交一次 canonical 执行。
    """
    from app.application.messaging.webagent import stream as stream_module

    request, user, _ = choice
    callback = build_agent_choice_callback(request.request_id, 1)
    result = await web_agent.resolve_and_save_web_agent_choice(callback, user)
    repeated = await web_agent.resolve_and_save_web_agent_choice(callback, user)
    assert repeated["already_selected"] is False
    monkeypatch.setattr(web_agent.agent_application, "get_running_agent_manager", lambda: SimpleNamespace())
    monkeypatch.setattr(web_agent, "get_api_runtime_config_snapshot", lambda: SimpleNamespace(ai_agent_enable=True))
    monkeypatch.setattr(stream_module, "build_agent_web_agent_stream", Mock(side_effect=RuntimeError("stream setup failed")))
    command = web_agent.WebAgentStreamCommand(text=result["message"], session_id=request.session_id,
                                             choice_selection=result["choice_selection"], echo_user=False)
    with pytest.raises(RuntimeError, match="stream setup failed"):
        await web_agent.build_web_agent_stream(command, current_user=user,
            is_disconnected=AsyncMock(return_value=False), protected_transport_supported=False)
    assert request.execution_claimed is False
    assert agent_interaction_manager.claim_web_choice(request.request_id, str(user.id), request.session_id) is not None


@pytest.mark.asyncio
async def test_explicit_agent_admission_rejection_releases_choice_for_retry(choice, monkeypatch):
    """
    Agent 入队前明确拒绝时归还选择，未知执行失败保留领取防止重复业务。
    """
    from app.agent.session import AgentManagerQueueFullError

    request, user, _ = choice
    result = await web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 1), user)
    manager = SimpleNamespace(submit_steering_message=AsyncMock(return_value=None),
                              process_message=AsyncMock(side_effect=AgentManagerQueueFullError(request.session_id, 8)))
    monkeypatch.setattr(web_agent.agent_application, "get_running_agent_manager", lambda: manager)
    monkeypatch.setattr(web_agent.agent_application, "get_web_agent_type", lambda: object)
    monkeypatch.setattr(web_agent, "get_api_runtime_config_snapshot", lambda: SimpleNamespace(ai_agent_enable=True))
    monkeypatch.setattr(web_agent, "bind_web_agent_user_session", Mock())
    command = web_agent.WebAgentStreamCommand(text=result["message"], session_id=request.session_id,
                                             choice_selection=result["choice_selection"], echo_user=False)
    stream = await web_agent.build_web_agent_stream(command, current_user=user,
        is_disconnected=AsyncMock(return_value=False), protected_transport_supported=False)
    assert "error" in [event["type"] async for event in stream.events]
    await web_agent.wait_web_agent_background_tasks()
    assert request.execution_claimed is False
    retry = await web_agent.resolve_and_save_web_agent_choice(build_agent_choice_callback(request.request_id, 2), user)
    assert retry["already_selected"] is False and retry["message"] == "选择电影"
