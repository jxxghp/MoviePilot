from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.orchestration import subscribe as subscribe_module
from app.application.orchestration.subscribe import SubscribeChain
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


class _TimedOutLock:
    """模拟订阅搜索锁在等待窗口内始终无法取得。"""

    def acquire(self, **_kwargs):
        """返回未取得锁，验证调用方不会越过互斥边界继续执行。"""
        return False

    def release(self):
        """超时路径不应释放未持有的锁。"""
        raise AssertionError("未持有的订阅锁不应被释放")


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
        bangumiid=None,
        anilistid=None,
        media_source="themoviedb",
        media_id="12345",
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

    media_chain_class = Mock()
    with patch.object(subscribe_module, "MediaChain", media_chain_class):
        chain = object.__new__(SubscribeChain)
        chain.search(state="N", manual=False)

    media_chain_class.assert_not_called()
    assert _SubscribeOper.updates == []


def test_new_subscribe_search_marks_state_after_attempt(monkeypatch) -> None:
    """
    新增订阅越过保护期并实际尝试搜索后，应从 N 状态收敛为 R。
    """
    _SubscribeOper.subscribe = _new_subscribe(datetime.now() - timedelta(minutes=2))
    _SubscribeOper.updates = []
    monkeypatch.setattr(subscribe_module, "SubscribeOper", _SubscribeOper)

    media_chain = Mock()
    media_chain.recognize_media.return_value = None
    with patch.object(subscribe_module, "MediaChain", return_value=media_chain):
        chain = object.__new__(SubscribeChain)
        chain.search(state="N", manual=False)

    media_chain.recognize_media.assert_called_once()
    assert _SubscribeOper.updates == [(31, {"state": "R"})]


def test_subscribe_search_aborts_when_lock_times_out(monkeypatch) -> None:
    """订阅搜索锁超时后必须中止，不能在无锁状态下继续访问订阅。"""
    monkeypatch.setattr(SubscribeChain, "_rlock", _TimedOutLock())
    subscribe_oper = Mock()
    monkeypatch.setattr(subscribe_module, "SubscribeOper", subscribe_oper)
    progress = Mock()

    chain = object.__new__(SubscribeChain)
    chain.search(state="N", progress_callback=progress)

    subscribe_oper.assert_not_called()
    progress.assert_called_once_with(
        value=100,
        text="订阅搜索锁等待超时，已跳过本轮",
    )


def test_subscribe_match_aborts_when_lock_times_out(monkeypatch) -> None:
    """订阅匹配锁超时后必须中止，不能绕过防重复下载边界。"""
    monkeypatch.setattr(SubscribeChain, "_rlock", _TimedOutLock())
    progress = Mock()

    chain = object.__new__(SubscribeChain)
    chain.match({"example.org": []}, progress_callback=progress)

    progress.assert_any_call(value=100, text="订阅匹配锁等待超时，已跳过本轮")
