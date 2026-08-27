import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.application.server.share import ServerSharingService
from app.application.workflow import WorkflowSnapshot


def _workflow(*, actions=(), flows=()) -> WorkflowSnapshot:
    """构造中心服务分享使用的真实工作流快照。"""
    return WorkflowSnapshot(
        id=1,
        name="Demo Workflow",
        description="demo",
        timer=None,
        trigger_type="manual",
        event_type=None,
        event_conditions={},
        state="W",
        current_action=None,
        result=None,
        run_count=0,
        actions=actions,
        flows=flows,
        context={"private": True},
        execution_config={},
        execution_state={},
        add_time=None,
        last_time=None,
    )


def _service(**overrides) -> ServerSharingService:
    """构造不依赖数据库和网络的中心服务分享用例。"""
    def handle_response(response, clear):
        """模拟旧 Helper 的成功响应和缓存失效顺序。"""
        clear()
        return response.status_code == 200, ""

    defaults = {
        "subscribe_provider": Mock(return_value=None),
        "async_subscribe_provider": AsyncMock(return_value=None),
        "workflow_provider": Mock(return_value=None),
        "async_workflow_provider": AsyncMock(return_value=None),
        "user_uuid_provider": Mock(return_value="user-1"),
        "subscribe_sender": Mock(),
        "async_subscribe_sender": AsyncMock(),
        "workflow_sender": Mock(),
        "async_workflow_sender": AsyncMock(),
        "response_handler": handle_response,
        "subscribe_cache_clearer": Mock(),
        "workflow_cache_clearer": Mock(),
    }
    defaults.update(overrides)
    return ServerSharingService(**defaults)


def test_subscribe_share_builds_public_payload_and_clears_cache_after_success():
    """订阅分享隐藏本地字段，并在成功响应后触发缓存失效。"""
    sender = Mock(return_value=SimpleNamespace(status_code=200))
    clear = Mock()
    subscribe = SimpleNamespace(to_dict=lambda: {
        "name": "Demo",
        "type": "电影",
        "media_source": "themoviedb",
        "media_id": "123",
        "username": "private",
    })
    service = _service(
        subscribe_provider=Mock(return_value=subscribe),
        subscribe_sender=sender,
        subscribe_cache_clearer=clear,
    )

    result = service.share_subscribe(
        enabled=True,
        subscribe_id=1,
        share_title="Title",
        share_comment="Comment",
        share_user="User",
    )

    assert result == (True, "")
    payload = sender.call_args.args[0]
    assert payload["share_uid"] == "user-1"
    assert payload["media_source"] == "themoviedb"
    assert "username" not in payload
    clear.assert_called_once_with()


def test_workflow_validation_stops_before_transport():
    """缺少动作或流程的工作流不会进入中心服务传输。"""
    sender = Mock()
    workflow = _workflow(flows=({"id": 1},))
    service = _service(
        workflow_provider=Mock(return_value=workflow),
        workflow_sender=sender,
    )

    result = service.share_workflow(
        enabled=True,
        workflow_id=1,
        share_title="Title",
        share_comment="Comment",
        share_user="User",
    )

    assert result == (False, "请分享有动作和流程的工作流")
    sender.assert_not_called()


def test_workflow_share_serializes_snapshot_without_local_fields():
    """同步工作流分享从冻结快照生成兼容载荷并剔除本地上下文。"""
    sender = Mock(return_value=SimpleNamespace(status_code=200))
    workflow = _workflow(
        actions=({"id": "action-1"},),
        flows=({"source": "action-1", "target": "end"},),
    )
    service = _service(
        workflow_provider=Mock(return_value=workflow),
        workflow_sender=sender,
    )

    result = service.share_workflow(
        enabled=True,
        workflow_id=1,
        share_title="Title",
        share_comment="Comment",
        share_user="User",
    )

    assert result == (True, "")
    payload = sender.call_args.args[0]
    assert "id" not in payload
    assert "context" not in payload
    assert json.loads(payload["actions"]) == [{"id": "action-1"}]
    assert json.loads(payload["flows"]) == [
        {"source": "action-1", "target": "end"}
    ]


def test_async_subscribe_share_uses_async_reader_and_transport():
    """异步分享路径不会回退到同步数据库或网络端口。"""
    subscribe = SimpleNamespace(to_dict=lambda: {
        "name": "Demo",
        "type": "电影",
        "media_source": "themoviedb",
        "media_id": "123",
    })
    reader = AsyncMock(return_value=subscribe)
    sender = AsyncMock(return_value=SimpleNamespace(status_code=200))
    service = _service(
        async_subscribe_provider=reader,
        async_subscribe_sender=sender,
    )

    result = asyncio.run(service.async_share_subscribe(
        enabled=True,
        subscribe_id=1,
        share_title="Title",
        share_comment="Comment",
        share_user="User",
    ))

    assert result == (True, "")
    reader.assert_awaited_once_with(1)
    sender.assert_awaited_once()


def test_async_workflow_share_uses_snapshot_reader_and_transport():
    """异步工作流分享复用同一快照契约且不回退同步端口。"""
    workflow = _workflow(
        actions=({"id": "action-1"},),
        flows=({"source": "action-1", "target": "end"},),
    )
    reader = AsyncMock(return_value=workflow)
    sender = AsyncMock(return_value=SimpleNamespace(status_code=200))
    service = _service(
        async_workflow_provider=reader,
        async_workflow_sender=sender,
    )

    result = asyncio.run(service.async_share_workflow(
        enabled=True,
        workflow_id=1,
        share_title="Title",
        share_comment="Comment",
        share_user="User",
    ))

    assert result == (True, "")
    reader.assert_awaited_once_with(1)
    sender.assert_awaited_once()
