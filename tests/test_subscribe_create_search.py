import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, patch

from app.api.endpoints.subscribe import create_subscribe
from app.chain.subscribe import SubscribeChain
from app.schemas.subscribe import Subscribe
from app.schemas.types import MediaType


class _BackgroundTasks:
    """
    最小后台任务替身，记录订阅创建接口入队的任务。
    """

    def __init__(self) -> None:
        self.tasks = []

    def add_task(self, func: Callable[..., Any], **kwargs: Any) -> None:
        """
        记录后台任务函数和参数。
        """
        self.tasks.append({"func": func, "kwargs": kwargs})


class _User(SimpleNamespace):
    """
    最小用户替身，模拟订阅创建接口依赖的用户字段。
    """

    def __init__(self, name: str, is_superuser: bool) -> None:
        super().__init__(name=name, is_superuser=is_superuser)


class _SubscribeOper:
    """
    最小订阅 Oper 替身，隔离 SubscribeChain.search 的数据库访问。
    """

    subscribe: Optional[SimpleNamespace] = None
    updates: list[tuple[int, dict[str, Any]]] = []

    def get(self, sid: int) -> Optional[SimpleNamespace]:
        """
        按 ID 返回测试订阅对象。
        """
        return self.subscribe if self.subscribe and self.subscribe.id == sid else None

    def list(self, _state: str) -> list[SimpleNamespace]:
        """
        返回批量搜索需要的测试订阅列表。
        """
        return [self.subscribe] if self.subscribe else []

    def update(self, sid: int, payload: dict[str, Any]) -> None:
        """
        记录订阅状态更新请求。
        """
        self.updates.append((sid, payload))


def _new_subscribe() -> SimpleNamespace:
    """
    构造一个刚创建的电影订阅。
    """
    return SimpleNamespace(
        id=31,
        name="测试电影",
        year="2026",
        type=MediaType.MOVIE.value,
        tmdbid=12345,
        doubanid=None,
        season=None,
        custom_words=None,
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        state="N",
        episode_group=None,
    )


def test_create_subscribe_schedules_first_search_for_new_subscribe() -> None:
    """
    API 新增订阅成功后应立即入队该订阅的首次后台搜索。
    """
    background_tasks = _BackgroundTasks()
    subscribe_in = Subscribe(
        name="测试电影",
        year="2026",
        type=MediaType.MOVIE.value,
    )

    with patch(
        "app.api.endpoints.subscribe.SubscribeChain.async_add",
        new=AsyncMock(return_value=(31, "新增订阅成功")),
    ), patch("app.api.endpoints.subscribe.Scheduler") as scheduler:
        response = asyncio.run(
            create_subscribe(
                subscribe_in=subscribe_in,
                background_tasks=background_tasks,
                current_user=_User(name="alice", is_superuser=False),
            )
        )

    assert response.success
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0]["func"] == scheduler.return_value.start
    assert background_tasks.tasks[0]["kwargs"] == {
        "job_id": "new_subscribe_search",
        "sid": 31,
        "state": None,
        "manual": False,
    }


def test_create_subscribe_does_not_search_existing_subscribe() -> None:
    """
    重复订阅返回已存在时不应额外触发首次搜索。
    """
    background_tasks = _BackgroundTasks()
    subscribe_in = Subscribe(
        name="测试电影",
        year="2026",
        type=MediaType.MOVIE.value,
    )

    with patch(
        "app.api.endpoints.subscribe.SubscribeChain.async_add",
        new=AsyncMock(return_value=(31, "订阅已存在")),
    ), patch("app.api.endpoints.subscribe.Scheduler") as scheduler:
        response = asyncio.run(
            create_subscribe(
                subscribe_in=subscribe_in,
                background_tasks=background_tasks,
                current_user=_User(name="alice", is_superuser=False),
            )
        )

    assert response.success
    assert background_tasks.tasks == []
    scheduler.assert_not_called()


def test_subscribe_search_runs_single_new_subscribe_immediately(monkeypatch) -> None:
    """
    指定订阅 ID 的首次搜索不应被新建 60 秒保护挡住。
    """
    subscribe = _new_subscribe()
    _SubscribeOper.subscribe = subscribe
    _SubscribeOper.updates = []
    monkeypatch.setattr("app.chain.subscribe.SubscribeOper", _SubscribeOper)

    with patch.object(SubscribeChain, "recognize_media", return_value=None) as recognize:
        chain = object.__new__(SubscribeChain)
        chain.search(sid=subscribe.id, state=None, manual=False)

    recognize.assert_called_once()
    assert _SubscribeOper.updates == [(subscribe.id, {"state": "R"})]


def test_subscribe_search_keeps_batch_new_subscribe_delay(monkeypatch) -> None:
    """
    定时批量扫描新订阅时仍保留 60 秒编辑窗口。
    """
    subscribe = _new_subscribe()
    _SubscribeOper.subscribe = subscribe
    _SubscribeOper.updates = []
    monkeypatch.setattr("app.chain.subscribe.SubscribeOper", _SubscribeOper)

    with patch.object(SubscribeChain, "recognize_media", return_value=None) as recognize:
        chain = object.__new__(SubscribeChain)
        chain.search(state="N", manual=False)

    recognize.assert_not_called()
    assert _SubscribeOper.updates == []
