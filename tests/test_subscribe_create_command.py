"""订阅新增事务所有权与默认入口集成测试。"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.subscription.write import (
    AsyncCreateSubscriptionCommand,
    CreateSubscriptionCommand,
    add_subscribe,
    async_add_subscribe,
)
from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper, SubscribeStageResult
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType


def _media(media_id: str) -> MediaInfo:
    """构造默认事务写入路径所需的最小媒体信息。"""
    media = MediaInfo()
    media.type = MediaType.MOVIE
    media.title = "事务测试电影"
    media.year = "2026"
    media.media_source = MediaSource.TMDB
    media.media_id = media_id
    media.vote_average = 8.0
    media.overview = "事务切片"
    return media


def test_sync_command_orders_stage_commit_before_caller_effect() -> None:
    """同步新增只有在仓储暂存和提交成功后才把结果交给外部副作用。"""
    calls: list[str] = []
    repository = Mock()
    repository.stage_add.side_effect = lambda *_: (
        calls.append("stage")
        or SubscribeStageResult(10, "新增订阅成功", True)
    )
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    command = CreateSubscriptionCommand(repository, unit_of_work)

    result = command.execute({"media_id": "10"}, {"name": "demo"})
    calls.append("effect")

    assert result == (10, "新增订阅成功")
    assert calls == ["stage", "commit", "effect"]
    unit_of_work.rollback.assert_not_called()


def test_sync_command_rolls_back_commit_failure_and_skips_effect() -> None:
    """提交失败必须回滚并传播原异常，调用方不能误执行提交后副作用。"""
    commit_error = RuntimeError("commit failed")
    repository = Mock()
    repository.stage_add.return_value = SubscribeStageResult(
        11,
        "新增订阅成功",
        True,
    )
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = commit_error
    command = CreateSubscriptionCommand(repository, unit_of_work)
    effects: list[str] = []

    with pytest.raises(RuntimeError) as raised:
        command.execute({"media_id": "11"}, {"name": "demo"})
        effects.append("effect")

    assert raised.value is commit_error
    assert effects == []
    unit_of_work.rollback.assert_called_once_with()


def test_sync_command_does_not_commit_duplicate_request() -> None:
    """查重命中沿用旧 ID 和消息，不开启无意义写事务。"""
    repository = Mock()
    repository.stage_add.return_value = SubscribeStageResult(
        12,
        "订阅已存在",
        False,
    )
    unit_of_work = Mock()
    command = CreateSubscriptionCommand(repository, unit_of_work)

    assert command.execute({}, {}) == (12, "订阅已存在")
    unit_of_work.commit.assert_not_called()
    unit_of_work.rollback.assert_not_called()


def test_sync_event_failure_does_not_roll_back_committed_subscription() -> None:
    """事件属于提交后副作用，失败只向上传播且不能伪装成数据库回滚。"""
    calls: list[str] = []
    repository = Mock()
    repository.stage_add.return_value = SubscribeStageResult(
        13,
        "新增订阅成功",
        True,
    )
    unit_of_work = Mock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    event_error = RuntimeError("event failed")

    def send_event(_subscribe_id: int) -> None:
        """模拟 Chain 在提交后发送订阅事件失败。"""
        calls.append("event")
        raise event_error

    command = CreateSubscriptionCommand(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        command.execute({}, {}, after_commit=send_event)

    assert raised.value is event_error
    assert calls == ["commit", "event"]
    unit_of_work.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_async_command_rolls_back_staging_failure() -> None:
    """异步 flush 或唯一约束失败同样由命令回滚，不留部分写入。"""
    staging_error = RuntimeError("flush failed")
    repository = Mock()
    repository.async_stage_add = AsyncMock(side_effect=staging_error)
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    command = AsyncCreateSubscriptionCommand(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        await command.execute({}, {})

    assert raised.value is staging_error
    unit_of_work.commit.assert_not_awaited()
    unit_of_work.rollback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_async_report_failure_happens_after_event_without_rollback() -> None:
    """异步上报失败保留事件先行顺序，也不回滚已经提交的订阅。"""
    calls: list[str] = []
    repository = Mock()
    repository.async_stage_add = AsyncMock(
        return_value=SubscribeStageResult(14, "新增订阅成功", True)
    )
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    unit_of_work.rollback = AsyncMock()
    report_error = RuntimeError("report failed")

    async def send_event_and_report(_subscribe_id: int) -> None:
        """模拟 Chain 先发事件再执行统计上报。"""
        calls.append("event")
        calls.append("report")
        raise report_error

    command = AsyncCreateSubscriptionCommand(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        await command.execute({}, {}, after_commit=send_event_and_report)

    assert raised.value is report_error
    assert calls == ["commit", "event", "report"]
    unit_of_work.rollback.assert_not_awaited()


def test_default_sync_writer_persists_once_and_reuses_duplicate(db) -> None:
    """Chain 默认入口使用独立事务写入，重复媒体身份返回同一订阅。"""
    db.watermark(Subscribe)
    media = _media("arch-221-sync")
    after_commit = Mock()

    first = add_subscribe(mediainfo=media, after_commit=after_commit)
    second = add_subscribe(mediainfo=media, after_commit=after_commit)

    assert first[0] > 0
    assert first[1] == "新增订阅成功"
    assert second == (first[0], "订阅已存在")
    db.session.expire_all()
    rows = Subscribe.list_by_media_identity(
        db.session,
        media_source=MediaSource.TMDB,
        media_id="arch-221-sync",
    )
    assert [row.id for row in rows] == [first[0]]
    assert after_commit.call_args_list == [
        ((first[0],), {}),
        ((first[0],), {}),
    ]


def test_stage_add_executes_identity_sql_in_oper(db, monkeypatch) -> None:
    """规范新增路径直接由 Oper 查询，不能退回 Model 自动会话装饰器。"""
    db.watermark(Subscribe)
    commit = Mock(wraps=db.session.commit)
    monkeypatch.setattr(db.session, "commit", commit)
    monkeypatch.setattr(
        Subscribe,
        "exists",
        Mock(side_effect=AssertionError("model query must not run")),
    )
    oper = SubscribeOper(db.session)
    identity = {
        "media_source": str(MediaSource.TMDB),
        "media_id": "arch-221-stage",
        "music_type": None,
        "season": None,
        "episode_group": None,
    }

    staged = oper.stage_add(
        identity,
        {
            "name": "Oper SQL",
            "type": MediaType.MOVIE.value,
            "state": "N",
            **identity,
        },
    )

    assert staged.created is True
    assert staged.subscribe_id > 0
    commit.assert_not_called()
    db.session.rollback()


def test_default_async_writer_persists_committed_row(db) -> None:
    """Agent/API 使用的异步 Chain 入口在返回前已完成请求级提交。"""
    db.watermark(Subscribe)
    media = _media("arch-221-async")

    subscribe_id, message = asyncio.run(async_add_subscribe(mediainfo=media))

    assert subscribe_id > 0
    assert message == "新增订阅成功"
    db.session.expire_all()
    persisted = Subscribe.get(db.session, subscribe_id)
    assert persisted is not None
    assert persisted.media_id == "arch-221-async"
