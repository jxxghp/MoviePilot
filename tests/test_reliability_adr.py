"""后台动作可靠性 ADR 与事件登记一致性测试。"""

from pathlib import Path

from app.schemas.types import ChainEventType, EventType


ADR_PATH = Path(__file__).parents[1] / "docs" / "adr" / "0007-background-action-reliability.md"


def test_reliability_adr_names_every_event() -> None:
    """ADR 必须逐个提及全部广播和链式事件，不能只写笼统分组。"""
    content = ADR_PATH.read_text(encoding="utf-8")

    missing = [
        event_type.name
        for event_type in (*EventType, *ChainEventType)
        if f"`{event_type.name}`" not in content
    ]
    assert missing == []


def test_reliability_adr_covers_all_background_mechanisms() -> None:
    """ADR 必须覆盖方案要求的四类非 Event 后台机制和完成语义。"""
    content = ADR_PATH.read_text(encoding="utf-8")

    for phrase in (
        "FastAPI BackgroundTasks",
        "Scheduler jobs",
        "Agent tasks",
        "Transfer pending",
        "完成点",
        "dead letter",
        "idempotency key",
    ):
        assert phrase in content
