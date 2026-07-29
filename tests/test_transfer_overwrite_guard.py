from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.modules.filemanager.storages.alipan import AliPan
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.storages.rclone import Rclone
from app.modules.filemanager.storages.u115 import U115Pan
from app.schemas.exception import StorageQueryError


def _local() -> LocalStorage:
    """
    构造本地存储实例（跳过初始化）。
    """
    return object.__new__(LocalStorage)


def _u115() -> U115Pan:
    """
    构造 115 存储实例（跳过初始化）。
    """
    return object.__new__(U115Pan)


def _alipan(monkeypatch) -> AliPan:
    """
    构造阿里云盘存储实例（跳过初始化，_default_drive_id 为只读属性需在类级替换）。
    """
    monkeypatch.setattr(AliPan, "_default_drive_id", "drive-1", raising=False)
    return object.__new__(AliPan)


def test_local_strict_missing_file_returns_none(tmp_path):
    """
    目标文件确实不存在时应确认为不存在，允许正常整理。
    """
    assert _local().get_item_strict(tmp_path / "missing.mkv") is None


def test_local_strict_existing_file_returns_item(tmp_path):
    """
    目标文件存在时应返回文件项。
    """
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"movie")

    item = _local().get_item_strict(target)

    assert item is not None
    assert item.path == target.as_posix()


def test_local_strict_broken_symlink_returns_none(tmp_path):
    """
    失效软链接视为目标不存在，不应阻断整理。
    """
    target = tmp_path / "movie.mkv"
    target.symlink_to(tmp_path / "gone.mkv")

    assert _local().get_item_strict(target) is None


def test_local_strict_raises_on_stat_error(tmp_path, monkeypatch):
    """
    FUSE 挂载抖动导致 stat 失败时应抛出 StorageQueryError，拒绝覆盖。
    """
    target = tmp_path / "movie.mkv"

    def raise_stat_error(self, *args, **kwargs):
        """
        模拟 CloudDrive FUSE 挂载返回 ENOTRECOVERABLE。
        """
        raise OSError(131, "State not recoverable")

    monkeypatch.setattr(Path, "stat", raise_stat_error)

    with pytest.raises(StorageQueryError):
        _local().get_item_strict(target)


def test_u115_strict_transport_failure_raises():
    """
    115 请求失败（网络/限流重试用尽）时应抛出 StorageQueryError。
    """
    storage = _u115()
    storage._request_api = MagicMock(return_value=None)

    with pytest.raises(StorageQueryError):
        storage.get_item_strict(Path("/movie.mkv"))


def test_u115_get_item_keeps_swallowing_transport_failure():
    """
    宽松版 get_item 行为保持兼容：请求失败仍返回 None。
    """
    storage = _u115()
    storage._request_api = MagicMock(return_value=None)

    assert storage.get_item(Path("/movie.mkv")) is None


def test_u115_strict_confirmed_absent_returns_none():
    """
    115 业务码返回记录不存在（data 为空）时应确认为不存在。
    """
    storage = _u115()
    storage._request_api = MagicMock(return_value={"state": True, "code": 20004, "data": {}})

    assert storage.get_item_strict(Path("/movie.mkv")) is None


def test_u115_strict_returns_item():
    """
    115 返回有效文件数据时应构造文件项。
    """
    storage = _u115()
    storage._request_api = MagicMock(return_value={"state": True, "code": 0, "data": {
        "file_id": 123,
        "file_category": "1",
        "file_name": "movie.mkv",
        "pick_code": "abc",
        "size_byte": 1024,
        "utime": 100,
    }})

    item = storage.get_item_strict(Path("/movie.mkv"))

    assert item is not None
    assert item.fileid == "123"
    assert item.size == 1024


def test_alipan_strict_notfound_returns_none(monkeypatch):
    """
    阿里云盘 NotFound 系列错误码应确认为不存在。
    """
    storage = _alipan(monkeypatch)
    storage._request_api = MagicMock(return_value={"code": "NotFound.File", "message": "not found"})

    assert storage.get_item_strict(Path("/movie.mkv")) is None


def test_alipan_strict_other_error_raises(monkeypatch):
    """
    阿里云盘非 NotFound 的业务错误（如限流）应抛出 StorageQueryError。
    """
    storage = _alipan(monkeypatch)
    storage._request_api = MagicMock(return_value={"code": "TooManyRequests", "message": "limit"})

    with pytest.raises(StorageQueryError):
        storage.get_item_strict(Path("/movie.mkv"))


def test_alipan_strict_transport_failure_raises(monkeypatch):
    """
    阿里云盘请求失败时应抛出 StorageQueryError。
    """
    storage = _alipan(monkeypatch)
    storage._request_api = MagicMock(return_value=None)

    with pytest.raises(StorageQueryError):
        storage.get_item_strict(Path("/movie.mkv"))


def test_alipan_strict_returns_item(monkeypatch):
    """
    阿里云盘返回有效数据时应构造文件项。
    """
    storage = _alipan(monkeypatch)
    storage._request_api = MagicMock(return_value={"file_id": "f1", "name": "movie.mkv"})
    setattr(storage, "_AliPan__get_fileitem", MagicMock(return_value="ITEM"))

    assert storage.get_item_strict(Path("/movie.mkv")) == "ITEM"


def test_storage_base_strict_defaults_to_get_item():
    """
    未覆写的存储沿用 get_item 判定，行为不变。
    """
    storage = object.__new__(Rclone)
    storage.get_item = MagicMock(return_value=None)

    assert storage.get_item_strict(Path("/movie.mkv")) is None
    storage.get_item.assert_called_once()
