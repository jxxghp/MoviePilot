"""
验证存储项缺少路径或名称时不会访问远端、删除本地数据或构造错误目标。
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.modules.filemanager.storages import StorageBase
from app.modules.filemanager.storages.alipan import AliPan
from app.modules.filemanager.storages.alist import Alist
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.storages.rclone import Rclone
from app.modules.filemanager.storages.smb import SMB
from app.schemas.file import FileItem


@pytest.mark.parametrize("storage_class", [Alist, LocalStorage, Rclone, SMB])
@pytest.mark.parametrize("missing_path", [None, ""])
def test_missing_path_operations_stop_before_io(storage_class, missing_path, tmp_path, monkeypatch):
    """
    空路径项不能被当作当前目录或远端根目录执行查询、传输、重命名及删除。
    """
    storage = storage_class()
    item = FileItem(storage=storage.schema.value, type="file", path=missing_path)
    query = Mock(side_effect=AssertionError("空路径不应触发查询"))
    monkeypatch.setattr(storage, "get_item", query)
    if isinstance(storage, SMB):
        monkeypatch.setattr(storage, "_check_connection", Mock())
    assert storage.detail(item) is None
    assert storage.delete(item) is False
    assert storage.rename(item, "renamed.mkv") is False
    assert storage.upload(item, tmp_path / "source.mkv") is None
    if isinstance(storage, LocalStorage):
        with pytest.raises(ValueError, match="缺少路径"):
            storage.download(item, tmp_path)
        assert storage.copy(item, tmp_path, "copy.mkv") is False
        assert storage.move(item, tmp_path, "move.mkv") is False
        assert storage.link(item, tmp_path / "hardlink.mkv") is False
        assert storage.softlink(item, tmp_path / "symlink.mkv") is False
    else:
        assert storage.download(item, tmp_path) is None
    if isinstance(storage, (Alist, Rclone)):
        assert storage.list(item) == []
        assert storage.create_folder(item, "child") is None
        assert storage.copy(item, tmp_path, "copy.mkv") is False
        assert storage.move(item, tmp_path, "move.mkv") is False
    if isinstance(storage, Alist):
        assert storage.get_parent(item) is None
        assert storage.copy_item(item, tmp_path, "copy.mkv") is None
    query.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("storage_class", [AliPan, Alist, Rclone, SMB])
def test_cloud_backends_report_unsupported_links(storage_class, tmp_path):
    """
    不支持链接的存储明确返回失败，调用方不能把空返回值误判为已完成。
    """
    storage = storage_class()
    item = FileItem(storage=storage.schema.value, type="file", path="/source.mkv")
    assert storage.softlink(item, tmp_path / "symlink.mkv") is False
    if not isinstance(storage, SMB):
        assert storage.link(item, tmp_path / "hardlink.mkv") is False
    assert list(tmp_path.iterdir()) == []


def test_alipan_incomplete_metadata_does_not_query_or_create(monkeypatch):
    """
    阿里云盘的目录创建、详情及复制拒绝缺失必要元数据的输入。
    """
    storage = AliPan()
    request = Mock(side_effect=AssertionError("不完整文件项不应触发 API"))
    monkeypatch.setattr(storage, "_request_api", request)
    query = Mock(side_effect=AssertionError("不完整文件项不应查询目标"))
    monkeypatch.setattr(storage, "get_item", query)
    item = FileItem(storage=storage.schema.value, type="file", path=None)
    assert storage.detail(item) is None
    assert storage.create_folder(item, "child") is None
    assert storage.copy(item, Path("/target"), "copy.mkv") is False
    request.assert_not_called()
    query.assert_not_called()


def test_missing_snapshot_path_preserves_previous_files(monkeypatch):
    """
    缺少目录路径的快照输入保留旧文件索引，避免把未查询到的数据清成空目录。
    """
    storage = LocalStorage()
    item = FileItem(storage=storage.schema.value, type="dir", path=None)
    files = {"/keep.mkv": {"size": 123}}
    children = {}
    previous = {}
    query = Mock(side_effect=AssertionError("空路径不应遍历目录"))
    monkeypatch.setattr(storage, "_snapshot_list", query)
    assert StorageBase.get_parent(storage, item) is None
    StorageBase._snapshot_remove_deleted_children(storage, item, [], files, children, previous)
    StorageBase._snapshot_file(storage, item, files, children, previous, None, 5)
    assert files == {"/keep.mkv": {"size": 123}}
    query.assert_not_called()


def test_rclone_config_without_content_writes_empty_file(tmp_path, monkeypatch):
    """
    缺省配置内容保存为空文本，未提供路径时不会尝试打开文件。
    """
    storage = Rclone()
    monkeypatch.setattr(StorageBase, "set_config", Mock())
    storage.set_config({})
    config_path = tmp_path / "nested" / "rclone.conf"
    storage.set_config({"filepath": str(config_path), "content": None})
    assert config_path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("payload", [{}, {"Name": None}, {"Name": 123}])
def test_rclone_listing_rejects_item_without_name(payload):
    """
    远端列表中的无名项不能被投影成当前目录或名称为 None 的文件。
    """
    with pytest.raises(ValueError, match="缺少名称"):
        Rclone()._Rclone__get_rcloneitem(payload)
