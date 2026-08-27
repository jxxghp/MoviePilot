"""StorageChain 严格查询的三态传播合同。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.chain.storage import StorageChain
from app.modules.filemanager.module import FileManagerModule
from app.schemas.exception import StorageQueryError


def test_storage_chain_strict_query_distinguishes_absent_and_failure() -> None:
    """严格查询用 None 表示确认不存在，并原样传播 provider 查询失败。"""
    chain = object.__new__(StorageChain)
    chain._module_dispatcher = Mock()
    chain._module_dispatcher.dispatch_strict.return_value = None

    assert chain.get_file_item_strict("plugin", Path("/missing.mkv")) is None

    chain._module_dispatcher.dispatch_strict.side_effect = StorageQueryError(
        "provider lookup failed"
    )
    with pytest.raises(StorageQueryError, match="provider lookup failed"):
        chain.get_file_item_strict("plugin", Path("/unknown.mkv"))


def test_filemanager_storage_query_uses_strict_storage_adapter(monkeypatch) -> None:
    """宿主存储 provider 必须调用 get_item_strict，不能回退会吞错的 get_item。"""
    module = object.__new__(FileManagerModule)
    module._support_storages = ["local"]
    storage = Mock()
    storage.get_item_strict.side_effect = StorageQueryError("local stat failed")
    monkeypatch.setattr(
        module,
        "_FileManagerModule__get_storage_oper",
        lambda _storage: storage,
    )

    with pytest.raises(StorageQueryError, match="local stat failed"):
        module.get_file_item("local", Path("/library/old.mkv"))

    storage.get_item.assert_not_called()
