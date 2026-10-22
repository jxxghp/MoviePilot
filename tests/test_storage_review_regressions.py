"""
验证存储审查发现的分页、凭据和中断下载回归。
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.filemanager.storages import alist as alist_module
from app.modules.filemanager.storages.alipan import AliPan
from app.modules.filemanager.storages.alist import Alist
from app.schemas.file import FileItem


def test_alipan_token_refresh_preserves_drive_configuration(monkeypatch):
    """
    自动刷新只替换令牌字段，保留已授权空间和用户配置。
    """
    storage = object.__new__(AliPan)
    configuration = {"refresh_token": "old", "access_token": "expired", "expires_in": 1,
                     "refresh_time": 1, "resource_drive_id": "drive", "custom": "preserved"}
    storage._request_utils = Mock()
    monkeypatch.setattr(storage, "get_conf", lambda: dict(configuration))
    monkeypatch.setattr(storage, "set_config", lambda value: (configuration.clear(), configuration.update(value)))
    monkeypatch.setattr(storage, "_AliPan__refresh_access_token", lambda _token: {
        "access_token": "fresh", "refresh_token": "next", "expires_in": 7200,
    })
    assert storage.access_token == "fresh"
    assert configuration["resource_drive_id"] == "drive"
    assert configuration["custom"] == "preserved"
    assert configuration["refresh_token"] == "next"


@pytest.mark.parametrize("response, expected", [(None, False), ({"code": "AccessDenied"}, False), ({}, True)])
def test_alipan_delete_requires_success_response(monkeypatch, response, expected):
    """
    删除拒绝和无响应不能落账为成功，空 JSON 是开放接口正常成功响应。
    """
    storage = object.__new__(AliPan)
    monkeypatch.setattr(storage, "_request_api", lambda *_args, **_kwargs: response)
    assert storage.delete(FileItem(storage="alipan", fileid="file", drive_id="drive")) is expected


def test_alipan_full_last_page_stops_at_empty_marker(monkeypatch):
    """
    满页但没有下一页标记时只发送一次目录请求。
    """
    storage = object.__new__(AliPan)
    monkeypatch.setattr(storage, "get_conf", lambda: {"resource_drive_id": "drive"})
    request = Mock(side_effect=[{"items": [{"file_id": str(i)} for i in range(100)], "next_marker": ""},
                               AssertionError("末页不应重取第一页")])
    monkeypatch.setattr(storage, "_request_api", request)
    monkeypatch.setattr(storage, "_AliPan__get_fileitem", lambda info, **_kwargs: FileItem(fileid=info["file_id"]))
    assert len(storage.list(FileItem(storage="alipan", path="/", type="dir"))) == 100
    assert request.call_count == 1


@pytest.mark.parametrize("failure", ["stream", "short", "cancel"])
def test_openlist_incomplete_download_is_not_a_transfer_source(tmp_path, monkeypatch, failure):
    """
    流中断、正常提前结束和取消都必须返回失败并删除未完成文件。
    """
    storage = object.__new__(Alist)
    monkeypatch.setattr(storage, "get_conf", lambda: {"url": "https://openlist.invalid", "token": "secret"})
    response = Mock(status_code=200)
    response.json.return_value = {"code": 200, "data": {"raw_url": "https://cdn.invalid/file", "sign": ""}}
    stream = Mock(headers={"Content-Length": "12"})

    def chunks(**_kwargs):
        """
        只提供部分内容，并可模拟连接中断。
        """
        yield b"part"
        if failure == "stream":
            raise ConnectionError("stream interrupted")

    stream.iter_content = chunks

    @contextmanager
    def get_stream(*_args, **_kwargs):
        """
        在网络边界回放下载流。
        """
        yield stream

    request = Mock()
    request.post_res.return_value = response
    request.get_stream = get_stream
    monkeypatch.setattr(alist_module, "RequestUtils", lambda **_kwargs: request)
    monkeypatch.setattr(alist_module.runtime_stop_state, "consume_transfer_stop", lambda _path: failure == "cancel")
    item = FileItem(storage="alist", path="/movie.mkv", name="movie.mkv", type="file", size=12)
    assert storage.download(item, tmp_path) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("raw_url", ["https://cdn.invalid/file", "https://openlist.invalid/d/file"])
def test_openlist_download_never_forwards_access_token(tmp_path, monkeypatch, raw_url):
    """
    文件直链和签名下载地址不携带 OpenList API 令牌，跨源重定向也没有令牌可泄漏。
    """
    storage = object.__new__(Alist)
    monkeypatch.setattr(storage, "get_conf", lambda: {"url": "https://openlist.invalid", "token": "secret"})
    response = Mock(status_code=200)
    response.json.return_value = {"code": 200, "data": {"raw_url": raw_url, "sign": ""}}
    configurations = []
    stream = Mock(headers={"Content-Length": "4"})
    stream.iter_content.return_value = [b"data"]

    @contextmanager
    def get_stream(*_args, **_kwargs):
        """
        返回完整的离线内容响应。
        """
        yield stream

    def request_factory(**kwargs):
        """
        记录 API 和下载各自的请求头。
        """
        configurations.append(kwargs)
        return SimpleNamespace(post_res=lambda *_args, **_kwargs: response, get_stream=get_stream)

    monkeypatch.setattr(alist_module, "RequestUtils", request_factory)
    item = FileItem(storage="alist", path="/file.mkv", name="file.mkv", type="file", size=4)
    result = storage.download(item, tmp_path)
    assert result.read_bytes() == b"data"
    assert "Authorization" not in configurations[-1].get("headers", {})


def test_openlist_detail_size_prevents_legacy_move_deleting_truncated_source(tmp_path, monkeypatch):
    """
    旧文件项及无长度 HTTP 响应仍须使用详情大小阻止截断移动删除源。
    """
    from app.modules.filemanager.transhandler import TransHandler
    storage = object.__new__(Alist)
    monkeypatch.setattr(storage, "get_conf", lambda: {"url": "https://openlist.invalid", "token": "secret"})
    response = Mock(status_code=200)
    response.json.return_value = {"code": 200, "data": {
        "raw_url": "https://cdn.invalid/file", "sign": "", "size": 100,
    }}
    stream = Mock(headers={})
    stream.iter_content.return_value = [b"PART"]

    @contextmanager
    def get_stream(*_args, **_kwargs):
        """
        模拟正常结束但短于详情大小的无长度下载。
        """
        yield stream

    request = Mock()
    request.post_res.return_value = response
    request.get_stream = get_stream
    monkeypatch.setattr(alist_module, "RequestUtils", lambda **_kwargs: request)
    deleted = Mock(return_value=True)
    monkeypatch.setattr(storage, "delete", deleted)
    source = FileItem(storage="alist", path="/Movie.mkv", name="Movie.mkv", type="file")
    target = tmp_path / "Movie.mkv"
    result, _ = TransHandler._TransHandler__download_to_local(source, storage, target, "move")
    assert result is False
    deleted.assert_not_called()
    assert not target.exists()
    assert not list(tmp_path.glob(".mp-download-*"))
