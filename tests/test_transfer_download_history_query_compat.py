"""Transfer/Download History 查询兼容层的会话与旧插件 ABI 验证。"""

import asyncio

from app.db import decorators
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.session import SessionFactory, async_session_scope


def test_oper_reuses_explicit_sync_session(db, monkeypatch):
    """显式同步会话绑定到 Oper 后，查询不能再创建兼容会话。"""
    row = db.add(TransferHistory(src="/compat/transfer.mkv", src_storage="local"))
    monkeypatch.setattr(
        decorators,
        "ScopedSession",
        lambda: (_ for _ in ()).throw(AssertionError("不应创建额外同步会话")),
    )

    assert TransferHistoryOper(db.session).get_by_src("/compat/transfer.mkv").id == row.id
    assert DownloadHistoryOper(db.session).get_by_hash("missing") is None


def test_model_legacy_sync_calls_preserve_business_arguments(db, monkeypatch):
    """旧插件省略 db 时，第一个位置参数仍须作为业务参数传入。"""
    row = db.add(TransferHistory(src="/compat/legacy.mkv", src_storage="local"))
    created = []
    monkeypatch.setattr(
        decorators,
        "ScopedSession",
        lambda: (created.append(True) or SessionFactory()),
    )

    assert TransferHistory.get_by_src("/compat/legacy.mkv").id == row.id
    assert created == [True]


def test_download_model_legacy_sync_call_preserves_keyword_arguments(db, monkeypatch):
    """旧插件使用关键字查询时，兼容层仍须自动补入 db。"""
    row = db.add(
        DownloadHistory(
            path="/compat/download",
            type="电视剧",
            download_hash="compat-hash",
            title="兼容",
        )
    )
    created = []
    monkeypatch.setattr(
        decorators,
        "ScopedSession",
        lambda: (created.append(True) or SessionFactory()),
    )

    assert DownloadHistory.get_by_hash(download_hash="compat-hash").id == row.id
    assert created == [True]


def test_oper_reuses_explicit_async_session(db, monkeypatch):
    """显式异步会话绑定到 Oper 后，异步查询不能再创建兼容作用域。"""
    db.add(
        DownloadHistory(
            path="/compat/async-download",
            type="电视剧",
            title="异步兼容",
            download_hash="async-compat",
        )
    )

    async def check() -> None:
        async with async_session_scope() as session:
            monkeypatch.setattr(
                decorators,
                "async_session_scope",
                lambda: (_ for _ in ()).throw(AssertionError("不应创建额外异步会话")),
            )
            result = await DownloadHistoryOper(session).async_list_by_page(count=10)
            assert any(item.download_hash == "async-compat" for item in result)

    asyncio.run(check())


def test_model_legacy_async_calls_support_explicit_and_implicit_sessions(db, monkeypatch):
    """异步 Model 查询同时保留显式会话调用与旧插件无会话调用。"""
    db.add(
        DownloadHistory(
            path="/compat/async-legacy",
            type="电视剧",
            title="异步旧 ABI",
            download_hash="async-legacy",
        )
    )
    original_scope = decorators.async_session_scope
    created = []

    def tracked_scope():
        """记录兼容层是否创建了异步会话作用域。"""
        created.append(True)
        return original_scope()

    async def check() -> None:
        async with original_scope() as session:
            assert await DownloadHistory.async_count(session) >= 1
        monkeypatch.setattr(decorators, "async_session_scope", tracked_scope)
        result = await DownloadHistory.async_list_by_title(title="异步旧 ABI")
        assert result[0].download_hash == "async-legacy"

    asyncio.run(check())
    assert created == [True]
