"""
StorageChain 严格查询的三态传播合同。
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.chain.storage import StorageChain
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.storages.local import LocalStorage
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem


def test_storage_chain_strict_query_distinguishes_absent_and_failure() -> None:
    """
    严格查询用 None 表示确认不存在，并原样传播 provider 查询失败。
    """
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
    """
    宿主存储 provider 必须调用 get_item_strict，不能回退会吞错的 get_item。
    """
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


def test_storage_image_link_uses_local_storage_adapter(monkeypatch, tmp_path) -> None:
    """
    图片复用经存储链、模块派发和存储适配器建立真实硬链接，避免重复上传。
    """
    module = object.__new__(FileManagerModule)
    module._support_storages = ["local"]
    storage = object.__new__(LocalStorage)
    lookup = Mock(return_value=storage)
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", lookup)
    chain = object.__new__(StorageChain)
    chain._module_dispatcher = Mock()
    chain._module_dispatcher.dispatch.side_effect = lambda method, **kwargs: getattr(module, method)(**kwargs)
    source = tmp_path / "backdrop.jpg"
    source.write_bytes(b"artwork")
    target = tmp_path / "fanart.jpg"
    fileitem = FileItem(storage="local", path=str(source), name=source.name, type="file")

    assert chain.link_file(fileitem, target) is True

    lookup.assert_called_once_with("local", "link")
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_ino == source.stat().st_ino


def test_filemanager_image_link_rejects_unavailable_storage(monkeypatch) -> None:
    """
    存储没有注册硬链接能力时返回 False，让刮削链从下载副本回退上传。
    """
    module = object.__new__(FileManagerModule)
    module._support_storages = ["local"]
    lookup = Mock(return_value=None)
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", lookup)
    target = Path("/library/fanart.jpg")
    source = FileItem(storage="unregistered", path="/library/backdrop.jpg", type="file")

    assert module.link_file(source, target) is False
    lookup.assert_not_called()

    assert module.link_file(source.model_copy(update={"storage": "local"}), target) is False
    lookup.assert_called_once_with("local", "link")
