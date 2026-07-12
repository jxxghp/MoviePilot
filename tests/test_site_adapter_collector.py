import hashlib
import json
import os
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import site_adapter_collector as collector


SEARCH_HTML = """
<!doctype html>
<html>
  <head><script>window.token = "embedded-secret-value";</script></head>
  <body>
    <nav id="account">alice@example.com <span>192.168.1.20</span></nav>
    <form><input type="hidden" name="csrf_token" value="embedded-secret-value"></form>
    <table id="torrent-table" class="torrents">
      <thead>
        <tr><th>标题</th><th>大小</th><th>做种</th><th>发布者</th></tr>
      </thead>
      <tbody>
        <tr id="uploader-alice"
            class="torrent-row torrent-session-token-cell profile-alice contact-alice@example.com peer-192.168.1.20"
            data-id="987" data-private-user="alice" unknown="alice@example.com">
          <td>
            <a class="torrent-title" title="私密电影标题.2026.1080p" aria-label="私密电影标题"
               href="/details.php?id=123&passkey=embedded-secret-value">私密电影标题</a>
            <img data-orig="/covers/私密电影标题.jpg" data-original="/covers/private-title.jpg"
                 data-lazy-src="https://images.example.net/private-title.jpg">
          </td>
          <td>18.45 GiB</td>
          <td>42</td>
          <td><a class="username" href="/users/alice">alice</a></td>
          <td><span class="date-added" title="2026-07-12 12:34:56">刚刚</span></td>
        </tr>
        <tr class="torrent-listings-global-freeleech">
          <td><a class="torrent-title" href="/details.php?id=124">另一部私密电影</a></td>
          <td>8.00 GiB</td><td>12</td><td>bob</td>
        </tr>
        <tr class="torrent-row">
          <td><a class="torrent-title" href="/details.php?id=125">第三部私密电影</a></td>
          <td>2.00 GiB</td><td>8</td><td>carol</td>
        </tr>
      </tbody>
    </table>
    <footer>unrelated footer</footer>
  </body>
</html>
"""


class _FakeResponse:
    """提供采集器测试所需的最小响应接口。"""

    def __init__(
        self,
        status_code: int,
        url: str,
        body: bytes = b"",
        headers: dict = None,
    ):
        """初始化状态、地址、响应体和响应头。"""
        self.status_code = status_code
        self.url = url
        self._body = body
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size: int):
        """按给定块大小返回内存响应体。"""
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self) -> None:
        """记录响应已关闭。"""
        self.closed = True


class _FakeClient:
    """按顺序返回预设响应，避免测试产生真实外网请求。"""

    def __init__(self, responses: list[_FakeResponse]):
        """保存待返回响应和请求记录。"""
        self.responses = responses
        self.calls: list[dict] = []

    def get_res(self, **kwargs) -> _FakeResponse:
        """记录请求参数并返回下一条响应。"""
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_prepare_capture_request_uses_safe_slug_and_keyword_placeholder():
    """搜索请求应生成安全站点标识和严格关键词占位符。"""
    request = collector._prepare_capture_request(
        url="https://Tracker.Example.com/torrents.php?search={keyword}&category=1",
        keyword="Movie 2026",
    )

    assert request.site_id == "tracker-example-com"
    assert request.origin == "https://tracker.example.com"
    assert request.path == "/torrents.php"
    assert request.params == {"search": "Movie 2026", "category": "1"}
    assert request.public_params == {"search": "{keyword}", "category": "1"}


@pytest.mark.parametrize(
    "url",
    [
        "ftp://tracker.example.com/torrents.php?search={keyword}",
        "http://tracker.example.com/torrents.php?search={keyword}",
        "https://tracker.example.com/passkey/value/torrents.php?search={keyword}",
        "https://tracker.example.com/torrents.php?token=value&search={keyword}",
        "https://tracker.example.com/torrents.php?search={keyword}&q={keyword}",
    ],
)
def test_prepare_capture_request_rejects_unsafe_urls(url: str):
    """采集器应拒绝非 HTTPS 地址及疑似内嵌凭据的 URL。"""
    with pytest.raises(ValueError):
        collector._prepare_capture_request(url=url, keyword="Movie")


def test_user_details_link_is_not_a_torrent_result():
    """用户详情链接不得被误判为种子详情链接。"""
    soup = collector.BeautifulSoup(
        '<a href="/userdetails.php?id=9">user</a>',
        "html.parser",
    )

    assert collector._is_torrent_link(soup.a) is False


def test_sanitize_search_html_crops_and_redacts_result_structure():
    """脱敏结果应仅保留种子列表结构，不包含身份、标题和凭据。"""
    sanitized_html, row_count, report = collector._sanitize_search_html(
        html=SEARCH_HTML,
        origin="https://tracker.example.com",
        keyword="Movie",
    )
    lowered = sanitized_html.lower()

    assert row_count == 3
    assert "torrent-table" in sanitized_html
    assert "torrent-row" in sanitized_html
    assert "torrent-listings-global-freeleech" in sanitized_html
    assert "torrent-session-token-cell" in sanitized_html
    assert "unrelated footer" not in sanitized_html
    assert "alice" not in lowered
    assert "私密电影标题" not in sanitized_html
    assert "private-title" not in sanitized_html
    assert "embedded-secret-value" not in sanitized_html
    assert "passkey" not in lowered
    assert "csrf" not in lowered
    assert "window.token" not in lowered
    sanitized_soup = collector.BeautifulSoup(sanitized_html, "html.parser")
    result_row = sanitized_soup.select_one("tr.torrent-row")
    assert result_row["id"] == "uploader-redacted"
    assert "profile-redacted" in result_row["class"]
    assert "redacted" in result_row["class"]
    assert "peer-redacted" in result_row["class"]
    assert result_row["data-id"] == "0"
    assert not result_row.has_attr("data-private-user")
    assert not result_row.has_attr("unknown")
    assert sanitized_soup.select_one("a.torrent-title")["href"] == "/details.php?id=1"
    assert sanitized_soup.select_one("a.username")["href"] == "#redacted-identity"
    assert sanitized_soup.select_one("a.torrent-title")["title"] == "[REDACTED]"
    assert sanitized_soup.select_one("span.date-added")["title"] == "2000-01-01 00:00"
    assert report["redacted"] is True
    assert report["contains_credentials"] is False
    assert report["captured_rows"] == 3


def test_nested_nexus_table_counts_only_outer_result_rows():
    """NexusPHP 资源名内嵌表格不得重复计数或被当成外层结果裁剪。"""
    result_rows = "".join(
        f"""
        <tr class="outer-result-row">
          <td>
            <table class="torrentname">
              <tbody><tr><td><a href="/details.php?id={index}">资源 {index}</a></td></tr></tbody>
            </table>
          </td>
          <td>{index}</td>
        </tr>
        """
        for index in range(1, 31)
    )
    html = f"""
    <html><body>
      <table class="torrents">
        <thead><tr><th>名称</th><th>做种</th></tr></thead>
        <tbody>{result_rows}</tbody>
      </table>
    </body></html>
    """

    sanitized_html, row_count, report = collector._sanitize_search_html(
        html=html,
        origin="https://tracker.example.com",
        keyword="Movie",
    )
    soup = collector.BeautifulSoup(sanitized_html, "html.parser")

    assert row_count == collector.MAX_RESULT_ROWS
    assert len(soup.select("tr.outer-result-row")) == collector.MAX_RESULT_ROWS
    assert len(soup.select("table.torrentname")) == collector.MAX_RESULT_ROWS
    assert report["captured_rows"] == collector.MAX_RESULT_ROWS


def test_fetch_search_page_blocks_cross_origin_redirect(monkeypatch):
    """携带 Cookie 的采集请求遇到跨 origin 重定向时必须立即停止。"""
    response = _FakeResponse(
        status_code=302,
        url="https://tracker.example.com/torrents.php?search=Movie",
        headers={"Location": "https://login.example.net/sign-in"},
    )
    client = _FakeClient([response])
    monkeypatch.setattr(collector, "RequestUtils", lambda **_: client)
    request = collector._prepare_capture_request(
        url="https://tracker.example.com/torrents.php?search={keyword}",
        keyword="Movie",
    )

    with pytest.raises(RuntimeError, match="跨域重定向"):
        collector._fetch_search_page(request, "session=very-secret-cookie", "Browser UA")

    assert len(client.calls) == 1
    assert client.calls[0]["allow_redirects"] is False
    assert client.calls[0]["verify"] is True
    assert response.closed is True


def test_read_limited_response_rejects_oversized_content_length():
    """响应头声明超过容量上限时不应读取响应体。"""
    response = _FakeResponse(
        status_code=200,
        url="https://tracker.example.com/torrents.php",
        headers={"Content-Length": str(collector.MAX_RESPONSE_BYTES + 1)},
    )

    with pytest.raises(ValueError, match="超过 5 MiB"):
        collector._read_limited_response(response)


def test_collect_site_capture_writes_fixed_protocol(monkeypatch, tmp_path: Path):
    """采集结果应使用固定四文件协议，并通过摘要和脱敏声明自校验。"""
    cookie = "session=very-secret-cookie-value"
    monkeypatch.setattr(
        collector,
        "_fetch_search_page",
        lambda request, cookie, user_agent: SEARCH_HTML,
    )

    archive_path = collector.collect_site_capture(
        url="https://tracker.example.com/torrents.php?search={keyword}&category=1",
        keyword="Movie",
        cookie=cookie,
        output_dir=tmp_path,
        user_agent="Browser UA",
        site_name="示例站点",
    )

    with zipfile.ZipFile(archive_path) as archive:
        assert tuple(archive.namelist()) == collector.ARCHIVE_FILE_NAMES
        contents = {name: archive.read(name) for name in archive.namelist()}

    manifest = json.loads(contents["manifest.json"])
    request = json.loads(contents["request.json"])
    report = json.loads(contents["redaction-report.json"])
    assert manifest["format_version"] == 1
    assert manifest["site"] == {
        "id": "tracker-example-com",
        "name": "示例站点",
        "domain": "https://tracker.example.com",
        "public": False,
    }
    assert manifest["capture"]["kind"] == "search"
    assert manifest["capture"]["row_count"] == 3
    assert manifest["privacy"] == {
        "redacted": True,
        "contains_credentials": False,
    }
    assert manifest["files"]["search.html"] == hashlib.sha256(
        contents["search.html"]
    ).hexdigest()
    assert request == {
        "method": "get",
        "path": "/torrents.php",
        "params": {"category": "1", "search": "{keyword}"},
        "origin": "https://tracker.example.com",
    }
    assert report["redacted"] is True
    assert report["contains_credentials"] is False
    assert b"very-secret-cookie-value" not in b"\n".join(contents.values())
    assert b"Browser UA" not in b"\n".join(contents.values())


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "alice@example.com",
        "192.168.1.20",
        "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    ],
)
def test_verify_payload_rejects_residual_private_values(unsafe_value: str):
    """最终序列化检查应阻止残留身份信息和疑似高熵凭据写入。"""
    payload = {
        "manifest.json": b'{"files": {}}',
        "request.json": b"{}",
        "search.html": unsafe_value.encode("utf-8"),
        "redaction-report.json": b"{}",
    }

    with pytest.raises(ValueError):
        collector._verify_payload(payload, "session=safe-cookie-value")


def test_verify_payload_checks_complete_short_cookie():
    """即使 Cookie 很短，最终序列化检查也不得忽略其完整原值。"""
    payload = {
        "manifest.json": b'{"files": {}}',
        "request.json": b"{}",
        "search.html": b"id=1",
        "redaction-report.json": b"{}",
    }

    with pytest.raises(ValueError, match="凭据值"):
        collector._verify_payload(payload, "id=1")


def test_collect_does_not_write_archive_when_final_scan_fails(monkeypatch, tmp_path: Path):
    """最终序列化检查失败时不得在输出目录留下 ZIP。"""
    monkeypatch.setattr(
        collector,
        "_fetch_search_page",
        lambda request, cookie, user_agent: SEARCH_HTML,
    )

    with pytest.raises(ValueError, match="身份信息"):
        collector.collect_site_capture(
            url="https://tracker.example.com/torrents.php?search={keyword}",
            keyword="Movie",
            cookie="session=safe-cookie-value",
            output_dir=tmp_path,
            site_name="alice@example.com",
        )

    assert list(tmp_path.iterdir()) == []


def test_infer_search_keyword_from_url_and_visible_input():
    """普通浏览器模式应自动识别 URL 中与搜索框一致的关键词。"""
    capture = collector._BrowserCapture(
        url="https://tracker.example.com/browse.php?term=Movie%202026&category=1",
        html=SEARCH_HTML,
        cookie="session=safe-cookie-value",
        user_agent="Browser UA",
        search_inputs=[{"name": "term", "id": "search", "value": "Movie 2026"}],
    )

    assert collector._infer_search_keyword(capture) == "Movie 2026"


def test_infer_search_keyword_rejects_post_only_page():
    """地址栏不包含搜索参数时应友好拒绝，避免生成不可复用的 GET 配置。"""
    capture = collector._BrowserCapture(
        url="https://tracker.example.com/browse.php",
        html=SEARCH_HTML,
        cookie="session=safe-cookie-value",
        user_agent="Browser UA",
        search_inputs=[{"name": "search", "id": "search", "value": "Movie"}],
    )

    with pytest.raises(ValueError, match="地址栏包含关键词"):
        collector._infer_search_keyword(capture)


def test_read_browser_capture_filters_cookies_to_current_site(monkeypatch):
    """浏览器采集只应保留当前站点域名的 Cookie 用于本地泄露检查。"""
    class _FakeCdpClient:
        """模拟只读 CDP 页面与 Cookie 返回。"""

        def __init__(self, websocket_url: str):
            """保存测试传入的 WebSocket 地址。"""
            self.websocket_url = websocket_url

        def __enter__(self):
            """返回模拟 CDP 客户端。"""
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            """结束模拟 CDP 上下文。"""

        def evaluate(self, expression: str):
            """按表达式返回当前页面模拟数据。"""
            if "location.href" in expression:
                return "https://tracker.example.com/browse.php?search=Movie"
            if "outerHTML" in expression:
                return SEARCH_HTML
            if "userAgent" in expression:
                return "Browser UA"
            return [{"name": "search", "id": "search", "value": "Movie"}]

        def call(self, method: str):
            """返回当前站点与外部站点的模拟 Cookie。"""
            assert method == "Network.getAllCookies"
            return {
                "cookies": [
                    {"domain": ".example.com", "name": "session", "value": "site-secret"},
                    {"domain": ".external.test", "name": "other", "value": "external-secret"},
                ]
            }

    monkeypatch.setattr(
        collector,
        "_select_search_page_target",
        lambda port: {"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"},
    )
    monkeypatch.setattr(collector, "_CdpClient", _FakeCdpClient)
    session = collector._BrowserSession(
        process=None,
        profile_guard=None,
        port=9222,
        browser_websocket_url="ws://127.0.0.1/devtools/browser/1",
    )

    capture = collector._read_browser_capture(session)

    assert capture.cookie == "session=site-secret"
    assert "external-secret" not in capture.cookie
    assert capture.user_agent == "Browser UA"


def test_browser_capture_builds_archive_without_manual_cookie_input(monkeypatch, tmp_path: Path):
    """普通模式应从临时浏览器数据直接生成四文件 ZIP。"""
    @contextmanager
    def fake_browser_session(start_url: str):
        """提供无需启动真实浏览器的模拟会话。"""
        assert start_url == "https://tracker.example.com"
        yield object()

    browser_capture = collector._BrowserCapture(
        url="https://tracker.example.com/torrents.php?search=Movie&category=1",
        html=SEARCH_HTML,
        cookie="session=very-secret-cookie-value",
        user_agent="Browser UA",
        search_inputs=[{"name": "search", "id": "search", "value": "Movie"}],
    )
    monkeypatch.setattr(collector, "_launch_browser_session", fake_browser_session)
    monkeypatch.setattr(collector, "_read_browser_capture", lambda session: browser_capture)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    archive_path = collector.collect_site_capture_with_browser(
        start_url="https://tracker.example.com",
        output_dir=tmp_path,
    )

    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        request = json.loads(archive.read("request.json"))
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
    assert manifest["collector_version"] == "1.0.1"
    assert request["params"]["search"] == "{keyword}"
    assert b"very-secret-cookie-value" not in combined


def test_help_uses_utf8_when_parent_forces_legacy_code_page() -> None:
    """父进程强制 cp1252 时，中文 argparse 帮助仍应以 UTF-8 正常输出。"""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [sys.executable, str(Path(collector.__file__)), "--help"],
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "站点适配" in result.stdout.decode("utf-8")
