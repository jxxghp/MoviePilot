"""
验证 OpenList 自动分页、目录可见性和文件读写边界。
"""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from app.modules.filemanager.storages import alist as alist_module
from app.modules.filemanager.storages.alist import Alist
from app.schemas.file import FileItem


@pytest.fixture
def storage() -> Alist:
    """
    为每个用例创建独立的 OpenList 存储实例，HTTP 请求由用例自行替换。
    """
    return Alist()


class _FakeResponse:
    """
    提供预设 OpenList JSON 响应与状态码，不执行真实请求。
    """

    def __init__(self, payload: dict, status_code: int = 200):
        """
        保存预设响应载荷和 HTTP 状态码。
        """
        self._payload = payload
        self.status_code = status_code

    def json(self):
        """
        返回预设 JSON 响应正文。
        """
        return self._payload


def _dir_item(path: str = "/"):
    """
    构造指定路径的 OpenList 目录项，默认使用根目录。
    """
    return FileItem(storage="alist", type="dir", path=path)


def _page_payload(start: int, count: int, total: int) -> dict:
    """
    构造连续目录项及总数的分页响应，用于验证自动翻页。
    """
    return {
        "code": 200,
        "message": "success",
        "data": {
            "content": [
                {
                    "name": f"dir-{index}",
                    "size": 0,
                    "is_dir": True,
                    "modified": "2024-05-17T13:47:55.4174917+08:00",
                    "thumb": "",
                }
                for index in range(start, start + count)
            ],
            "total": total,
        },
    }


def test_list_fetches_all_pages_when_per_page_is_default(storage) -> None:
    """
    使用默认分页大小时遍历全部页并合并目录项。
    """
    responses = [
        _FakeResponse(_page_payload(0, 500, 505)),
        _FakeResponse(_page_payload(500, 5, 505)),
    ]
    request_utils = MagicMock()
    request_utils.post_res.side_effect = responses

    with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
        with patch.object(alist_module, "RequestUtils", return_value=request_utils):
            items = storage.list(_dir_item())

    assert 505 == len(items)
    assert "/dir-0/" == items[0].path
    assert "/dir-504/" == items[-1].path
    assert 2 == request_utils.post_res.call_count
    assert 1 == request_utils.post_res.call_args_list[0].kwargs["json"]["page"]
    assert 2 == request_utils.post_res.call_args_list[1].kwargs["json"]["page"]
    assert 500 == request_utils.post_res.call_args_list[0].kwargs["json"]["per_page"]
    assert 500 == request_utils.post_res.call_args_list[1].kwargs["json"]["per_page"]


def test_list_respects_explicit_per_page_without_auto_paging(storage) -> None:
    """
    显式指定每页数量时只请求一页，不触发自动翻页。
    """
    request_utils = MagicMock()
    request_utils.post_res.return_value = _FakeResponse(_page_payload(0, 50, 205))

    with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
        with patch.object(alist_module, "RequestUtils", return_value=request_utils):
            items = storage.list(_dir_item(), per_page=50)

    assert 50 == len(items)
    assert 1 == request_utils.post_res.call_count
    assert 50 == request_utils.post_res.call_args.kwargs["json"]["per_page"]


def test_create_folder_returns_target_when_openlist_metadata_is_delayed(storage) -> None:
    """
    OpenList 创建目录成功但元数据延迟可见时，应返回可用的目标目录项。
    """
    request_utils = MagicMock()
    request_utils.post_res.return_value = _FakeResponse({"code": 200, "message": "success", "data": None})

    with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
        with patch.object(storage, "_Alist__get_header_with_token", return_value={}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                with patch.object(storage, "_delay_get_item", return_value=None):
                    folder = storage.create_folder(
                        _dir_item("/library/Test Show (2026)"),
                        "Season 1",
                    )

    assert folder is not None
    assert "/library/Test Show (2026)/Season 1/" == folder.path
    assert "alist" == folder.storage
    assert "dir" == folder.type


def test_move_item_returns_target_when_openlist_metadata_is_delayed(storage) -> None:
    """
    OpenList 操作成功但目标元数据延迟可见时，应返回可用的目标文件项。
    """
    source = FileItem(
        storage="alist",
        type="file",
        path="/downloads/Test.Show.S01E01.mkv",
        name="Test.Show.S01E01.mkv",
        basename="Test.Show.S01E01",
        extension="mkv",
        size=1024,
        modify_time=1715939275.0,
    )
    request_utils = MagicMock()
    request_utils.post_res.return_value = _FakeResponse({"code": 200, "message": "success", "data": None})

    with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
        with patch.object(storage, "_Alist__get_header_with_token", return_value={}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                with patch.object(storage, "_delay_get_item", return_value=None):
                    target = storage.move_item(
                        source,
                        Path("/library/Test Show (2026)/Season 1"),
                        "Test.Show.S01E01.mkv",
                    )

    assert target is not None
    assert "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv" == target.path
    assert "alist" == target.storage
    assert "file" == target.type
    assert 1024 == target.size


def test_upload_sends_hash_headers_for_rapid_upload(storage) -> None:
    """
    OpenList 上传应附带文件哈希头，供服务端尝试秒传。
    """
    with TemporaryDirectory() as temp_dir:
        local_path = Path(temp_dir) / "rapid.bin"
        content = b"moviepilot-openlist-rapid-upload"
        local_path.write_bytes(content)
        upload_dir = FileItem(
            storage="alist",
            type="dir",
            path="/library/",
            name="library",
            basename="library",
        )
        uploaded_item = FileItem(
            storage="alist",
            type="file",
            path="/library/rapid.bin",
            name="rapid.bin",
            basename="rapid",
            extension="bin",
            size=len(content),
        )
        request_utils = MagicMock()
        request_utils.put_res.return_value = _FakeResponse({"code": 200, "message": "success", "data": None})

        with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils) as request_utils_factory:
                with patch.object(storage, "_delay_get_item", return_value=uploaded_item):
                    result = storage.upload(upload_dir, local_path)

    assert uploaded_item == result
    request_utils.put_res.assert_called_once()
    headers = request_utils_factory.call_args.kwargs["headers"]
    assert hashlib.md5(content).hexdigest() == headers["X-File-Md5"]
    assert hashlib.sha1(content).hexdigest() == headers["X-File-Sha1"]
    assert hashlib.sha256(content).hexdigest() == headers["X-File-Sha256"]
    assert str(len(content)) == headers["Content-Length"]
    assert "application/octet-stream" == headers["Content-Type"]
    assert "/library/rapid.bin" == headers["File-Path"]
