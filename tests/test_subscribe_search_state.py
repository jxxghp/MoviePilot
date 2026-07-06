from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.chain import subscribe as subscribe_module
from app.chain.subscribe import SubscribeChain
from app.schemas.types import MediaType


class _SubscribeOper:
    """
    最小订阅 Oper 替身，隔离订阅搜索状态流转测试的数据库访问。
    """

    subscribe = None
    updates = []

    def get(self, sid: int):
        """
        按 ID 返回测试订阅对象。
        """
        return self.subscribe if self.subscribe and self.subscribe.id == sid else None

    def list(self, _state: str):
        """
        返回批量搜索需要的测试订阅列表。
        """
        return [self.subscribe] if self.subscribe else []

    def update(self, sid: int, payload: dict) -> None:
        """
        记录订阅状态更新请求。
        """
        self.updates.append((sid, payload))


def _new_subscribe(created_at: datetime) -> SimpleNamespace:
    """
    构造一个新建电影订阅。
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
        date=created_at.strftime("%Y-%m-%d %H:%M:%S"),
        state="N",
        episode_group=None,
    )


def test_new_subscribe_search_keeps_state_when_recently_created(monkeypatch) -> None:
    """
    新增 60 秒保护期内跳过搜索时，应保留 N 状态等待下一轮新增订阅搜索。
    """
    _SubscribeOper.subscribe = _new_subscribe(datetime.now())
    _SubscribeOper.updates = []
    monkeypatch.setattr(subscribe_module, "SubscribeOper", _SubscribeOper)

    with patch.object(SubscribeChain, "recognize_media", return_value=None) as recognize:
        chain = object.__new__(SubscribeChain)
        chain.search(state="N", manual=False)

    recognize.assert_not_called()
    assert _SubscribeOper.updates == []


def test_new_subscribe_search_marks_state_after_attempt(monkeypatch) -> None:
    """
    新增订阅越过保护期并实际尝试搜索后，应从 N 状态收敛为 R。
    """
    _SubscribeOper.subscribe = _new_subscribe(datetime.now() - timedelta(minutes=2))
    _SubscribeOper.updates = []
    monkeypatch.setattr(subscribe_module, "SubscribeOper", _SubscribeOper)

    with patch.object(SubscribeChain, "recognize_media", return_value=None) as recognize:
        chain = object.__new__(SubscribeChain)
        chain.search(state="N", manual=False)

    recognize.assert_called_once()
    assert _SubscribeOper.updates == [(31, {"state": "R"})]
