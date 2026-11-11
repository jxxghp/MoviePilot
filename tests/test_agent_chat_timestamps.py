"""
选择回执的时间戳保留及历史会话读取兼容回归。
"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete

from app.api.endpoints import agent as agent_endpoint
from app.application.messaging.chat import get_configured_agent_chat_service
from app.db.models.agentchat import AgentChat
from app.db.oper.agentchat import AgentChatOper
from app.db.session import SessionFactory
from app.domain.chat import merge_choice_state, normalize_display_timestamps
from app.schemas.agent import AgentChatMessage


@pytest.fixture
def history_session_id():
    """
    为持久化路由测试分配唯一会话，并在结束后精确清理本次提交的记录。

    :yields: 仅供当前测试使用的临时会话标识
    """
    session_id = f"web-agent:timestamps-{uuid4().hex}"
    yield session_id
    with SessionFactory() as db:
        db.execute(delete(AgentChat).where(AgentChat.session_id == session_id))
        db.commit()


def _receipt(**fields: object) -> dict[str, object]:
    """
    构造带稳定标识的选择回执，允许省略时间戳以重放旧数据。

    :param fields: 回执需要补充或覆盖的展示字段
    :return: 用户选择电影的展示回执
    """
    return {
        "id": "choice-selection-test", "role": "user", "content": "",
        "choice_selection": {"choice_id": "test", "selected_label": "电影", "selected_value": "选择电影"},
        **fields,
    }


def test_replacement_keeps_original_receipt_timestamp_across_stale_snapshots() -> None:
    """
    旧快照省略回执或重复提交回执时，首次选择和原始时间都保持稳定。
    """
    card = {"id": "card", "role": "assistant", "content": "", "createdAt": 1000,
            "choices": [{"id": "test", "status": "pending"}]}
    original = [card, _receipt(createdAt=1010)]
    before = deepcopy(original)
    saved = original
    for incoming in ([card], [], [card, _receipt(createdAt=9000)], [card, _receipt()], [card]):
        saved = merge_choice_state(saved, incoming)
        receipt = next(message for message in saved if message.get("choice_selection"))
        assert AgentChatMessage.model_validate(receipt).createdAt == 1010
        assert receipt["choice_selection"] == original[1]["choice_selection"]
    assert original == before


def test_selected_card_recovery_supplies_receipt_timestamp() -> None:
    """
    旧记录只有已选卡片时，重建回执使用卡片时间并保留选择内容。
    """
    original = [{"id": "card", "role": "assistant", "content": "", "createdAt": 1234.5,
                 "choices": [{"id": "test", "status": "selected", "selected_value": "选择电影"}]}]
    result = merge_choice_state(original, [])
    assert len(result) == 1
    receipt = AgentChatMessage.model_validate(result[0])
    assert receipt.id == "choice-selection-test"
    assert receipt.createdAt == 1234.5
    assert receipt.choice_selection.selected_value == "选择电影"


@pytest.mark.parametrize("position", [0, 1, 2])
def test_legacy_receipt_is_repaired_on_save_without_mutating_input(position: int) -> None:
    """
    历史回执缺少时间时借用相邻消息时间，保存后可读取且不修改旧内存快照。

    :param position: 缺字段回执位于完整快照中的位置
    """
    original = [
        {"id": "first", "role": "user", "createdAt": 1000},
        {"id": "last", "role": "assistant", "createdAt": 2000},
    ]
    original.insert(position, _receipt())
    before = deepcopy(original)
    result = merge_choice_state([], original)
    assert result[position]["createdAt"] == (2000 if position == 2 else 1000)
    assert all(AgentChatMessage.model_validate(message) for message in result)
    assert original == before


def test_legacy_receipt_without_any_known_time_uses_stable_unknown_timestamp() -> None:
    """
    完全缺少时间的旧快照返回固定未知值，重复保存不会伪造不断变化的创建时间。
    """
    result = merge_choice_state([], [_receipt()])
    assert AgentChatMessage.model_validate(result[0]).createdAt == 0
    assert merge_choice_state(result, []) == result


@pytest.mark.parametrize("timestamp", [None, "invalid"])
def test_timestamp_compatibility_preserves_explicit_invalid_values(timestamp: object) -> None:
    """
    仅兼容缺失字段，显式非法值仍由原有模型验证报告。

    :param timestamp: 已提供但不符合毫秒数值约定的字段值
    """
    result = normalize_display_timestamps([_receipt(createdAt=timestamp)])
    with pytest.raises(ValidationError, match="createdAt"):
        AgentChatMessage.model_validate(result[0])


@pytest.mark.parametrize("replace_snapshot", [False, True])
def test_history_endpoint_reads_legacy_receipt_and_survives_old_snapshot_save(
    monkeypatch: pytest.MonkeyPatch, replace_snapshot: bool, history_session_id: str,
) -> None:
    """
    实际详情路由能读取脏历史，且旧内存快照或缺少回执的快照再次写回后仍返回 200。

    :param monkeypatch: 隔离运行中 Agent 管理器的 pytest 替换工具
    :param replace_snapshot: 后续保存完整旧快照或省略回执的快照
    :param history_session_id: 测试结束后会精确清理的独立会话标识
    """
    session_id = history_session_id
    legacy = [
        {"id": "first", "role": "user", "content": "选择电影", "createdAt": 1000},
        _receipt(),
        {"id": "last", "role": "assistant", "content": "继续", "createdAt": 2000},
    ]
    with SessionFactory() as db:
        db.add(AgentChat(session_id=session_id, user_id="501", username="alice",
                         display_messages=legacy, message_count=len(legacy)))
        db.commit()
    monkeypatch.setattr(agent_endpoint.agent_application, "get_running_agent_manager", lambda: None)
    app = FastAPI()
    app.include_router(agent_endpoint.router)
    app.dependency_overrides[agent_endpoint.get_agent_chat_service] = get_configured_agent_chat_service
    app.dependency_overrides[agent_endpoint.get_current_active_user] = lambda: SimpleNamespace(
        id=501, name="alice", is_superuser=False,
    )
    with TestClient(app) as client:
        for attempt in range(3):
            response = client.get(f"/sessions/{session_id}")
            assert response.status_code == 200, response.text
            messages = response.json()["data"]["messages"]
            assert len(messages) == 3
            assert next(message for message in messages if message.get("choice_selection"))["createdAt"] == 1000
            with SessionFactory() as db:
                row = AgentChatOper(db).get(session_id)
                if attempt == 0:
                    assert row.display_messages == legacy
                AgentChatOper(db).save_display_messages(
                    session_id=session_id, user_id="501",
                    messages=legacy if not replace_snapshot else [legacy[0], legacy[2]],
                )
                db.commit()
    with SessionFactory() as db:
        row = AgentChatOper(db).get(session_id)
        assert all(AgentChatMessage.model_validate(message) for message in row.display_messages)


def test_history_timestamp_compatibility_does_not_weaken_save_request_validation() -> None:
    """
    历史兼容只发生在读取和合并边界，客户端展示消息仍必须提供时间戳。
    """
    with pytest.raises(ValidationError, match="createdAt"):
        AgentChatMessage.model_validate(_receipt())
