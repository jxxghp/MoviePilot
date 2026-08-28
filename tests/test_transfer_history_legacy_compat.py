"""旧整理历史 Oper 的精确插件 ABI 门禁。"""

import importlib
import inspect

from app.db.oper.transferhistory import TransferHistoryOper as CanonicalTransferHistoryOper
from app.runtime.compat.manifest import MODULE_ALIASES


def test_legacy_transfer_history_import_targets_private_sdk_facade() -> None:
    """旧 DB 路径必须精确路由到私有 SDK 门面。"""
    alias = MODULE_ALIASES["app.db.transferhistory_oper"]
    legacy = importlib.import_module("app.db.transferhistory_oper")

    assert alias.target == "app.sdk._legacy.history"
    assert alias.owner == "sdk"
    assert alias.replacement == "app.application.history"
    assert legacy is importlib.import_module(alias.target)
    assert legacy.__all__ == ["TransferHistoryOper"]
    assert issubclass(legacy.TransferHistoryOper, CanonicalTransferHistoryOper)
    assert legacy.TransferHistoryOper is not CanonicalTransferHistoryOper

    oper_package = importlib.import_module("app.db.oper")
    assert oper_package.TransferHistoryOper is legacy.TransferHistoryOper
    assert "TransferHistoryOper" not in oper_package.__all__


def test_legacy_transfer_history_oper_preserves_exact_write_abi() -> None:
    """兼容类冻结旧方法参数，并以脱离 Session 的快照替代 ORM 注解。"""
    legacy = importlib.import_module("app.db.transferhistory_oper")
    oper_type = legacy.TransferHistoryOper
    public_methods = {
        name
        for name, value in oper_type.__dict__.items()
        if not name.startswith("_") and callable(value)
    }

    assert public_methods == {"add_success", "add_fail", "add_force"}
    assert str(inspect.signature(oper_type.add_force)) == (
        "(self, **kwargs) -> app.application.history.TransferHistorySnapshot | None"
    )
    assert str(inspect.signature(oper_type.add_success)) == (
        "(self, fileitem: app.schemas.file.FileItem, mode: str, "
        "meta: app.domain.meta.metabase.MetaBase, "
        "mediainfo: app.domain.context.MediaInfo | app.domain.context.MusicInfo, "
        "transferinfo: app.schemas.transfer.TransferInfo, "
        "downloader: str | None = None, download_hash: str | None = None) -> "
        "Any | None"
    )
    assert str(inspect.signature(oper_type.add_fail)) == (
        "(self, fileitem: app.schemas.file.FileItem, mode: str, "
        "meta: app.domain.meta.metabase.MetaBase, "
        "mediainfo: app.domain.context.MediaInfo | app.domain.context.MusicInfo | "
        "None = None, transferinfo: app.schemas.transfer.TransferInfo | None = None, "
        "downloader: str | None = None, download_hash: str | None = None) -> "
        "Any | None"
    )


def test_legacy_add_force_preserves_replace_behavior(db) -> None:
    """旧 add_force 继续按同源存储替换并返回可读记录。"""
    legacy = importlib.import_module("app.db.transferhistory_oper")
    oper = legacy.TransferHistoryOper(db.session)

    first = oper.add_force(
        src="/downloads/legacy-replace.mkv",
        src_storage=None,
        title="旧标题",
        status=False,
    )
    second = oper.add_force(
        src="/downloads/legacy-replace.mkv",
        src_storage="local",
        title="新标题",
        status=True,
    )

    assert first is not None
    assert second is not None
    assert second.src == "/downloads/legacy-replace.mkv"
    assert second.src_storage == "local"
    assert second.title == "新标题"
    assert second.status is True
    records = oper.list_success_by_src(
        "/downloads/legacy-replace.mkv",
        "local",
    )
    assert [record.id for record in records] == [second.id]


def test_legacy_transfer_history_helper_keeps_exact_application_route() -> None:
    """旧 Helper 路径继续复用应用层实现，不创建第二份兼容模块。"""
    alias = MODULE_ALIASES["app.helper.transferhistory"]

    assert alias.target == "app.application.history"
    assert alias.owner == "application"
    assert importlib.import_module("app.helper.transferhistory") is importlib.import_module(
        alias.target
    )
