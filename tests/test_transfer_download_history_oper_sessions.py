"""Transfer/Download History Oper 的显式会话复用验证。"""

import asyncio

from app.db import base as db_base
from app.db.models.transferhistory import TransferHistory
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.session import async_session_scope


def test_oper_reuses_explicit_sync_session(db, monkeypatch):
    """显式同步会话绑定到 Oper 后，查询不能再创建兼容会话。"""
    row = db.add(TransferHistory(src="/compat/transfer.mkv", src_storage="local"))
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(
            AssertionError("不应创建额外同步事务")
        ),
    )

    assert TransferHistoryOper(db.session).get_by_src("/compat/transfer.mkv").id == row.id
    assert DownloadHistoryOper(db.session).get_by_hash("missing") is None


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
                db_base,
                "run_async_transaction",
                lambda _operation: (_ for _ in ()).throw(
                    AssertionError("不应创建额外异步事务")
                ),
            )
            result = await DownloadHistoryOper(session).async_list_by_page(count=10)
            assert any(item.download_hash == "async-compat" for item in result)

    asyncio.run(check())
