"""
站点资源代理地址、有限网络重试及暂存文件完整性的离线回归。
"""

import errno
import hashlib
import json
import time
from contextlib import closing
from unittest.mock import Mock

import pytest
from requests import Response
from requests.exceptions import ChunkedEncodingError, ConnectionError, ReadTimeout

from app.adapters.system import resource as resource_module


@pytest.fixture
def resource_settings(monkeypatch):
    """
    隔离部署配置及退避等待，禁止用例访问实际代理或运行目录。
    """
    values = {"GITHUB_PROXY": "", "PROXY": None, "GITHUB_HEADERS": {}, "AUTO_UPDATE_RESOURCE": True}
    monkeypatch.setattr(resource_module, "get_runtime_setting", values.get)
    monkeypatch.setattr(resource_module.SystemUtils, "is_frozen", lambda: False)
    monkeypatch.setattr(time, "sleep", Mock())
    return values


def _response(status=200, content=b"{}"):
    """
    构造可关闭且不访问网络的真实 HTTP 响应。
    """
    response = Response()
    response.status_code = status
    response._content = content
    response._content_consumed = True
    response.url = "https://raw.githubusercontent.com/test"
    response.close = Mock()
    return response


@pytest.mark.parametrize("proxy", ["https://proxy.example", "https://proxy.example/", " https://proxy.example/path "])
def test_manifest_uses_current_normalized_proxy(monkeypatch, resource_settings, proxy):
    """
    导入后改变代理也必须生效，带路径及缺少尾斜杠的前缀均保留目标完整地址。
    """
    request = Mock(return_value=_response())
    monkeypatch.setattr(resource_module.RequestUtils, "get_res", request)
    helper = resource_module.ResourceHelper()
    resource_settings["GITHUB_PROXY"] = proxy

    helper._load_resource_info()

    assert request.call_args.args[0] == f"{proxy.strip().rstrip('/')}/{helper._repo}"
    assert request.call_args.kwargs["raise_exception"] is True


@pytest.mark.parametrize("failure", [ConnectionError("DNS lookup failed"), ReadTimeout("read timed out"), 503])
def test_manifest_retries_transient_failures(monkeypatch, resource_settings, failure):
    """
    清单遭遇 DNS、超时或服务端暂时故障时最多请求三次，成功后停止重试。
    """
    first = _response(failure) if isinstance(failure, int) else failure
    request = Mock(side_effect=[first, ConnectionError("connection reset"), _response()])
    monkeypatch.setattr(resource_module.RequestUtils, "get_res", request)

    assert resource_module.ResourceHelper()._load_resource_info().status_code == 200
    assert request.call_count == 3
    assert [item.args for item in time.sleep.call_args_list] == [(1,), (2,)]
    if isinstance(first, Response):
        first.close.assert_called_once()


@pytest.mark.parametrize("status, attempts", [(404, 1), (503, 3)])
def test_manifest_reports_http_status_with_bounded_attempts(monkeypatch, resource_settings, status, attempts):
    """
    永久 HTTP 错误直接失败，重试耗尽时仍保留阶段及真实状态码。
    """
    request = Mock(side_effect=lambda *args, **kwargs: _response(status))
    monkeypatch.setattr(resource_module.RequestUtils, "get_res", request)

    with pytest.raises(RuntimeError, match=f"清单.*HTTP {status}"):
        resource_module.ResourceHelper().get_update_info(auth_version="1", indexer_version="1")
    assert request.call_count == attempts


def test_manifest_reports_sanitized_connection_reason(monkeypatch, resource_settings):
    """
    连接异常保留 DNS 等诊断线索，同时遮盖异常地址里的凭据。
    """
    request = Mock(side_effect=ConnectionError("DNS failed: https://user:password@proxy.example/?token=private"))
    monkeypatch.setattr(resource_module.RequestUtils, "get_res", request)

    with pytest.raises(RuntimeError, match="ConnectionError.*DNS failed") as caught:
        resource_module.ResourceHelper()._load_resource_info()
    assert "password" not in str(caught.value)
    assert "private" not in str(caught.value)
    assert request.call_count == 3


def test_manifest_parse_error_closes_response_without_retry(monkeypatch, resource_settings):
    """
    镜像返回无法解析的数据时保留解析错误，不把内容错误当作连接抖动。
    """
    response = _response(content=b"<html>proxy error</html>")
    request = Mock(return_value=response)
    monkeypatch.setattr(resource_module.RequestUtils, "get_res", request)

    with pytest.raises(RuntimeError, match="解析失败"):
        resource_module.ResourceHelper().get_update_info(auth_version="1", indexer_version="1")
    request.assert_called_once()
    response.close.assert_called_once()


@pytest.mark.parametrize("proxy", ["", "  "])
def test_empty_mirror_preserves_global_proxy(monkeypatch, resource_settings, proxy):
    """
    未配置镜像前缀时清单保持原地址，并继续使用全局代理。
    """
    resource_settings.update(GITHUB_PROXY=proxy, PROXY={"https": "http://global.example"})
    request = Mock(return_value=_response())
    factory = Mock(return_value=Mock(get_res=request))
    monkeypatch.setattr(resource_module, "RequestUtils", factory)

    helper = resource_module.ResourceHelper()
    helper._load_resource_info()

    assert request.call_args.args[0] == helper._repo
    assert factory.call_args.kwargs["proxies"] == resource_settings["PROXY"]


def test_directory_request_retries_without_mirror_prefix(monkeypatch, resource_settings):
    """
    目录 API 沿用全局代理并支持重试，不把 GitHub API 改成镜像地址。
    """
    resource_settings.update(GITHUB_PROXY="https://proxy.example", PROXY={"https": "http://global.example"})
    response = _response(content=json.dumps([{"name": "sites.bin", "download_url": "https://raw.example/sites.bin", "size": 6}]).encode())
    request = Mock(side_effect=[ReadTimeout("read timed out"), response])
    factory = Mock(return_value=Mock(get_res=request))
    monkeypatch.setattr(resource_module, "RequestUtils", factory)

    files = resource_module.ResourceHelper().get_download_files({"files": [{"name": "sites.bin"}]})

    assert files[0]["size"] == 6
    assert request.call_args.args[0].startswith("https://api.github.com/")
    assert factory.call_args.kwargs["proxies"] == resource_settings["PROXY"]
    response.close.assert_called_once()


@pytest.mark.parametrize("failure", [ChunkedEncodingError("reset during body"), None])
def test_file_retry_discards_partial_content_and_rolls_back_progress(monkeypatch, tmp_path, resource_settings, failure):
    """
    传输中断及长度不足均从头下载，失败字节不累计到进度或最终摘要。
    """
    resource_settings["GITHUB_PROXY"] = "https://proxy.example"
    first = _response()
    first.iter_content = Mock(return_value=iter([b"bad"]))
    if failure:
        def interrupted_body(**_kwargs):
            """
            在写入部分内容后模拟网络中断。
            """
            yield b"bad"
            raise failure

        first.iter_content.side_effect = interrupted_body
    second = _response(content=b"abcdef")
    request = Mock(side_effect=[closing(first), closing(second)])
    monkeypatch.setattr(resource_module.RequestUtils, "get_stream", request)
    progress = Mock()

    files = resource_module.ResourceHelper().download_files(
        [{"name": "sites.bin", "download_url": "https://raw.example/sites.bin", "size": 6}], tmp_path, progress,
    )

    assert (tmp_path / "sites.bin").read_bytes() == b"abcdef"
    assert not (tmp_path / "sites.bin.part").exists()
    assert files[0]["sha256"] == hashlib.sha256(b"abcdef").hexdigest()
    assert [item.args[0] for item in progress.call_args_list] == [3, -3, 6]
    assert request.call_count == 2
    assert request.call_args.args[0] == "https://proxy.example/https://raw.example/sites.bin"
    first.close.assert_called_once()
    second.close.assert_called_once()


def test_failed_file_download_preserves_previous_file(monkeypatch, tmp_path, resource_settings):
    """
    重试耗尽不替换旧文件，也不留下可被误当作完整资源的临时文件。
    """
    (tmp_path / "sites.bin").write_bytes(b"old")
    request = Mock(side_effect=ConnectionError("connection reset"))
    monkeypatch.setattr(resource_module.RequestUtils, "get_stream", request)

    with pytest.raises(RuntimeError, match="sites.bin.*ConnectionError"):
        resource_module.ResourceHelper().download_files(
            [{"name": "sites.bin", "download_url": "https://raw.example/sites.bin", "size": 6}], tmp_path,
        )
    assert (tmp_path / "sites.bin").read_bytes() == b"old"
    assert not (tmp_path / "sites.bin.part").exists()
    assert request.call_count == 3


def test_file_disk_error_is_not_retried(monkeypatch, tmp_path, resource_settings):
    """
    磁盘写入故障直接反馈，不追加网络请求且仍释放响应、清理临时文件。
    """
    response = _response(content=b"abcdef")
    request = Mock(return_value=closing(response))
    monkeypatch.setattr(resource_module.RequestUtils, "get_stream", request)
    monkeypatch.setattr(type(tmp_path), "open", Mock(side_effect=OSError(errno.ENOSPC, "disk full")))

    with pytest.raises(OSError, match="disk full"):
        resource_module.ResourceHelper().download_files(
            [{"name": "sites.bin", "download_url": "https://raw.example/sites.bin", "size": 6}], tmp_path,
        )
    request.assert_called_once()
    response.close.assert_called_once()
    assert not (tmp_path / "sites.bin.part").exists()
