#!/usr/bin/env python3
"""采集并脱敏站点搜索页结构，生成站点适配数据包。"""

import argparse
import copy
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional, Union
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
import websocket
from bs4 import BeautifulSoup, Comment, NavigableString, Tag


COLLECTOR_VERSION = "1.0.1"
FORMAT_VERSION = 1
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_RESULT_ROWS = 25
MIN_RESULT_ROWS = 3
REQUEST_TIMEOUT_SECONDS = 30
CDP_HTTP_TIMEOUT_SECONDS = 5
BROWSER_START_TIMEOUT_SECONDS = 20
CDP_COMMAND_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
ARCHIVE_FILE_NAMES = (
    "manifest.json",
    "request.json",
    "search.html",
    "redaction-report.json",
)
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
SEARCH_PARAMETER_NAMES = {
    "keyword",
    "q",
    "query",
    "search",
    "searchstr",
    "searchtext",
    "search_text",
}
SENSITIVE_NAME_PATTERN = re.compile(
    r"(?:auth(?:orization)?|cookie|csrf|pass(?:key|word)?|secret|session(?:id)?|token)",
    re.IGNORECASE,
)
TORRENT_PATH_PATTERN = re.compile(r"(?:^|/)torrents?/[^/?#]+", re.IGNORECASE)
RESULT_DESCRIPTOR_PATTERN = re.compile(
    r"(?:browse|result|search|torrent)",
    re.IGNORECASE,
)
IDENTITY_DESCRIPTOR_PATTERN = re.compile(
    r"(?:member|profile|uploaded[_-]?by|uploader|user(?:details|name)?|author)",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
IP_ADDRESS_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HIGH_ENTROPY_VALUE_PATTERN = re.compile(
    r"\b[a-fA-F0-9]{24,}\b|"
    r"\b(?=[A-Za-z0-9_-]{32,}\b)(?=[A-Za-z0-9_-]*[a-z])"
    r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]+\b"
)
DATE_TIME_PATTERN = re.compile(
    r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b"
)
SIZE_PATTERN = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:[KMGTPE]i?B|bytes?)\s*$", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?\s*$")
URL_ATTRIBUTE_NAMES = {
    "action",
    "background",
    "cite",
    "data-href",
    "data-lazy-src",
    "data-orig",
    "data-original",
    "data-src",
    "data-url",
    "formaction",
    "href",
    "poster",
    "src",
}
URL_SET_ATTRIBUTE_NAMES = {"data-srcset", "srcset"}
TEXT_ATTRIBUTE_NAMES = {
    "alt",
    "aria-label",
    "data-date",
    "data-original-title",
    "data-time",
    "data-title",
    "datetime",
    "title",
}
ALLOWED_ATTRIBUTE_NAMES = {
    "aria-expanded",
    "aria-hidden",
    "aria-sort",
    "class",
    "colspan",
    "data-capture-kind",
    "data-id",
    "data-leechers",
    "data-seeders",
    "data-size",
    "data-timestamp",
    "decoding",
    "headers",
    "height",
    "id",
    "loading",
    "rel",
    "role",
    "rowspan",
    "scope",
    "target",
    "type",
    "width",
}
STRUCTURAL_PAGE_EXTENSIONS = {".asp", ".aspx", ".htm", ".html", ".php"}
NON_SEARCH_PARAMETER_NAMES = {
    "action",
    "area",
    "category",
    "cat",
    "filter",
    "incldead",
    "page",
    "sort",
    "type",
}


class RequestUtils:
    """为独立采集器提供与原调用方式兼容的最小 HTTP 客户端。"""

    def __init__(
        self,
        headers: Optional[dict[str, str]] = None,
        cookies: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """
        初始化仅用于当前采集任务的 requests 会话。

        :param headers: 请求头
        :param cookies: 原始 Cookie 请求头，仅驻留内存
        :param timeout: 默认请求超时秒数
        """
        self._session = requests.Session()
        self._session.headers.update(headers or {})
        if cookies:
            self._session.headers["Cookie"] = cookies
        self._timeout = timeout

    def get_res(self, url: str, **kwargs) -> requests.Response:
        """
        发起 GET 请求并返回 requests 响应对象。

        :param url: 请求地址
        :param kwargs: 传递给 requests 的请求参数
        :return: HTTP 响应对象
        :raises RuntimeError: 网络请求失败时抛出
        """
        kwargs.setdefault("timeout", self._timeout)
        try:
            return self._session.get(url, **kwargs)
        except requests.RequestException as error:
            raise RuntimeError(f"搜索页请求失败：{str(error)}") from error

    def close(self) -> None:
        """关闭当前采集任务使用的 HTTP 会话。"""
        self._session.close()


@dataclass(frozen=True)
class _BrowserCapture:
    """保存从当前浏览器标签页读取的临时采集数据。"""

    url: str
    html: str
    cookie: str
    user_agent: str
    search_inputs: list[dict[str, str]]


@dataclass
class _BrowserSession:
    """管理临时浏览器进程、CDP 地址和一次性用户目录。"""

    process: subprocess.Popen
    profile_guard: tempfile.TemporaryDirectory
    port: int
    browser_websocket_url: str

    def close(self) -> None:
        """关闭浏览器进程并删除包含登录状态的一次性用户目录。"""
        try:
            with _CdpClient(self.browser_websocket_url) as client:
                client.call("Browser.close")
        except (RuntimeError, websocket.WebSocketException):
            pass

        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self.profile_guard.cleanup()


class _CdpClient:
    """通过本机 WebSocket 执行最小 Chrome DevTools Protocol 命令。"""

    def __init__(self, websocket_url: str) -> None:
        """
        连接已校验为本机回环地址的 CDP WebSocket。

        :param websocket_url: CDP WebSocket 地址
        """
        parsed = urlsplit(websocket_url)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise RuntimeError("浏览器调试地址不是本机回环地址，已停止采集")
        try:
            self._socket = websocket.create_connection(
                websocket_url,
                timeout=CDP_COMMAND_TIMEOUT_SECONDS,
                origin="http://127.0.0.1",
                http_proxy_host=None,
            )
        except websocket.WebSocketException as error:
            raise RuntimeError(f"无法连接本机浏览器调试接口：{str(error)}") from error
        self._message_id = 0

    def __enter__(self) -> "_CdpClient":
        """返回已连接的 CDP 客户端。"""
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """离开上下文时关闭 WebSocket。"""
        self.close()

    def close(self) -> None:
        """关闭 CDP WebSocket。"""
        self._socket.close()

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        """
        发送一条 CDP 命令并等待对应响应。

        :param method: CDP 方法名
        :param params: 可选命令参数
        :return: CDP 结果字典
        :raises RuntimeError: CDP 返回错误、超时或连接中断时抛出
        """
        self._message_id += 1
        message_id = self._message_id
        command = {"id": message_id, "method": method}
        if params:
            command["params"] = params
        try:
            self._socket.send(json.dumps(command))
            deadline = time.monotonic() + CDP_COMMAND_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                message = json.loads(self._socket.recv())
                if message.get("id") != message_id:
                    continue
                if message.get("error"):
                    error_message = message["error"].get("message", "未知错误")
                    raise RuntimeError(f"浏览器调试命令失败：{error_message}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        except (json.JSONDecodeError, OSError, websocket.WebSocketException) as error:
            raise RuntimeError(f"读取浏览器页面失败：{str(error)}") from error
        raise RuntimeError("读取浏览器页面超时，请保持浏览器窗口打开后重试")

    def evaluate(self, expression: str):
        """
        在当前页面执行只读 JavaScript 并返回可序列化值。

        :param expression: JavaScript 表达式
        :return: 表达式结果
        :raises RuntimeError: 页面脚本执行失败时抛出
        """
        response = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if response.get("exceptionDetails"):
            raise RuntimeError("浏览器无法读取当前页面，请刷新页面后重试")
        remote_result = response.get("result", {})
        return remote_result.get("value")


@dataclass(frozen=True)
class _CaptureRequest:
    """保存规范化后的搜索请求及可公开元数据。"""

    url: str
    origin: str
    path: str
    params: dict[str, str]
    public_params: dict[str, str]
    domain: str
    site_id: str
    site_name: str


@dataclass
class _RedactionStats:
    """记录本地裁剪和脱敏动作的数量。"""

    discarded_nodes: int = 0
    removed_attributes: int = 0
    redacted_urls: int = 0
    redacted_identity_values: int = 0
    redacted_result_values: int = 0
    redacted_sensitive_values: int = 0


def _is_sensitive_name(value: str) -> bool:
    """判断字段名或路径是否带有凭据语义。"""
    return bool(SENSITIVE_NAME_PATTERN.search(value))


def _normalize_origin(url: str) -> tuple[str, str, str]:
    """
    校验 HTTPS 地址并返回 origin、主机名和规范路径。

    :param url: 用户输入的搜索页地址
    :return: origin、域名和以斜杠开头的相对路径
    :raises ValueError: 地址不是安全的 HTTPS 地址时抛出
    """
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("搜索页 URL 仅支持 https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("搜索页 URL 必须包含有效域名，且不能内嵌账号或密码")

    host = parsed.hostname.encode("idna").decode("ascii").lower()
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port = parsed.port
    authority = host if not port or port == default_port else f"{host}:{port}"
    origin = f"{parsed.scheme.lower()}://{authority}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    if _is_sensitive_name(path):
        raise ValueError("搜索页 URL 路径疑似包含凭据，请改用不带凭据的搜索地址")
    return origin, host, path


def _normalize_user_start_url(value: str) -> str:
    """允许普通用户只输入域名，并统一补成可访问的 HTTPS 地址。"""
    normalized = value.strip()
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    _normalize_origin(normalized)
    return normalized


def _make_site_id(domain: str) -> str:
    """根据域名生成仅含小写字母、数字和连字符的安全站点标识。"""
    normalized = domain.lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    slug = re.sub(r"-+", "-", slug)[:63].rstrip("-")
    if not slug:
        raise ValueError("无法根据域名生成安全的站点标识")
    if not slug[0].isalnum():
        slug = f"site-{slug}"
    return slug


def _prepare_capture_request(
    url: str,
    keyword: str,
    site_name: Optional[str] = None,
) -> _CaptureRequest:
    """
    规范化搜索请求，并将公开参数中的关键词替换为占位符。

    :param url: 包含搜索参数或 ``{keyword}`` 占位符的搜索页 URL
    :param keyword: 用于本次采集的搜索关键词
    :param site_name: 可选站点显示名称，留空时使用域名
    :return: 可用于请求与写入采集包的结构化参数
    :raises ValueError: URL 不含可识别的搜索参数或包含敏感参数时抛出
    """
    if not keyword.strip():
        raise ValueError("搜索关键词不能为空")

    origin, domain, path = _normalize_origin(url)
    parsed = urlsplit(url.strip())
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    request_params: dict[str, str] = {}
    public_params: dict[str, str] = {}
    keyword_assigned = False

    for name, value in pairs:
        if _is_sensitive_name(name):
            raise ValueError(f"搜索页 URL 参数 {name!r} 疑似包含凭据，请先移除")
        normalized_name = name.lower()
        has_keyword_value = "{keyword}" in value or value == keyword
        is_keyword_parameter = has_keyword_value or (
            normalized_name in SEARCH_PARAMETER_NAMES and not keyword_assigned
        )
        if is_keyword_parameter:
            if keyword_assigned:
                raise ValueError("URL 中存在多个关键词参数，请只保留一个搜索参数")
            request_params[name] = keyword
            public_params[name] = "{keyword}"
            keyword_assigned = True
        else:
            request_params[name] = value
            public_params[name] = value

    if not keyword_assigned:
        raise ValueError(
            "URL 中未找到搜索参数，请粘贴搜索后的 URL，或将关键词值写成 {keyword}"
        )

    site_id = _make_site_id(domain)
    request_url = urlunsplit((urlsplit(origin).scheme, urlsplit(origin).netloc, path, "", ""))
    return _CaptureRequest(
        url=request_url,
        origin=origin,
        path=path,
        params=request_params,
        public_params=public_params,
        domain=domain,
        site_id=site_id,
        site_name=site_name.strip() if site_name and site_name.strip() else domain,
    )


def _same_origin(url: str, expected_origin: str) -> bool:
    """判断地址是否与预期 origin 完全同源。"""
    try:
        origin, _, _ = _normalize_origin(url)
    except (TypeError, ValueError):
        return False
    return origin == expected_origin


def _find_browser_executable() -> Path:
    """查找本机已安装的 Chrome、Edge 或 Chromium 可执行文件。"""
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.extend(
            Path(path)
            for path in (
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                str(Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            )
        )
    elif os.name == "nt":
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        relative_paths = (
            "Google/Chrome/Application/chrome.exe",
            "Microsoft/Edge/Application/msedge.exe",
            "Chromium/Application/chrome.exe",
        )
        candidates.extend(
            Path(root) / relative_path
            for root in roots
            if root
            for relative_path in relative_paths
        )
    else:
        for command in (
            "google-chrome",
            "google-chrome-stable",
            "microsoft-edge",
            "microsoft-edge-stable",
            "chromium",
            "chromium-browser",
        ):
            executable = shutil.which(command)
            if executable:
                candidates.append(Path(executable))

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到 Chrome、Edge 或 Chromium，请先安装其中一个浏览器")


def _reserve_loopback_port() -> int:
    """让操作系统分配一个仅供本机临时浏览器使用的空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _load_cdp_json(port: int, path: str) -> object:
    """从本机 Chrome DevTools HTTP 接口读取 JSON。"""
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            f"http://127.0.0.1:{port}{path}",
            timeout=CDP_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(f"无法读取本机浏览器状态：{str(error)}") from error
    finally:
        session.close()


def _wait_for_browser_session(process: subprocess.Popen, port: int) -> str:
    """等待临时浏览器启动并返回浏览器级 CDP WebSocket 地址。"""
    deadline = time.monotonic() + BROWSER_START_TIMEOUT_SECONDS
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("浏览器启动后立即退出，请关闭已有异常进程后重试")
        try:
            version = _load_cdp_json(port, "/json/version")
            if isinstance(version, dict):
                websocket_url = version.get("webSocketDebuggerUrl")
                if isinstance(websocket_url, str) and websocket_url:
                    return websocket_url
        except RuntimeError as error:
            last_error = error
        time.sleep(0.2)
    detail = f"：{str(last_error)}" if last_error else ""
    raise RuntimeError(f"等待浏览器启动超时{detail}")


@contextmanager
def _launch_browser_session(start_url: str) -> Generator[_BrowserSession, None, None]:
    """使用一次性用户目录启动可视浏览器，并在结束时清理全部登录状态。"""
    _normalize_origin(start_url)
    browser_path = _find_browser_executable()
    port = _reserve_loopback_port()
    profile_guard = tempfile.TemporaryDirectory(
        prefix="moviepilot-site-collector-",
        ignore_cleanup_errors=True,
    )
    command = [
        str(browser_path),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=http://127.0.0.1",
        f"--user-data-dir={profile_guard.name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--new-window",
        start_url,
    ]
    process: Optional[subprocess.Popen] = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser_websocket_url = _wait_for_browser_session(process, port)
        session = _BrowserSession(
            process=process,
            profile_guard=profile_guard,
            port=port,
            browser_websocket_url=browser_websocket_url,
        )
    except Exception:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        profile_guard.cleanup()
        raise

    try:
        yield session
    finally:
        session.close()


def _select_search_page_target(port: int) -> dict:
    """从临时浏览器标签页中选择当前 HTTPS 搜索结果页。"""
    targets = _load_cdp_json(port, "/json/list")
    if not isinstance(targets, list):
        raise RuntimeError("浏览器标签页列表格式异常")
    pages = [
        target
        for target in targets
        if isinstance(target, dict)
        and target.get("type") == "page"
        and isinstance(target.get("url"), str)
        and target["url"].startswith("https://")
        and isinstance(target.get("webSocketDebuggerUrl"), str)
    ]
    if not pages:
        raise RuntimeError("未找到 HTTPS 页面，请在弹出的浏览器中打开站点搜索结果页")

    def target_priority(target: dict) -> tuple[int, int]:
        """优先选择 URL 中带明显搜索参数的页面。"""
        query_names = {
            name.lower()
            for name, value in parse_qsl(urlsplit(target["url"]).query, keep_blank_values=True)
            if value
        }
        return (len(query_names & SEARCH_PARAMETER_NAMES), len(urlsplit(target["url"]).query))

    return max(pages, key=target_priority)


def _read_browser_capture(session: _BrowserSession) -> _BrowserCapture:
    """通过本机 CDP 读取当前页面 URL、渲染后 HTML、Cookie 和 User-Agent。"""
    target = _select_search_page_target(session.port)
    with _CdpClient(target["webSocketDebuggerUrl"]) as client:
        current_url = client.evaluate("window.location.href")
        html_text = client.evaluate("document.documentElement.outerHTML")
        user_agent = client.evaluate("window.navigator.userAgent")
        search_inputs = client.evaluate(
            """
            Array.from(document.querySelectorAll('input, textarea'))
              .filter((element) => element.value && !['hidden', 'password'].includes(element.type))
              .map((element) => ({
                name: String(element.name || ''),
                id: String(element.id || ''),
                value: String(element.value || '')
              }))
            """
        )
        try:
            cookie_result = client.call("Network.getAllCookies")
        except RuntimeError:
            cookie_result = client.call("Storage.getCookies")

    if not isinstance(current_url, str) or not current_url.startswith("https://"):
        raise RuntimeError("当前标签页不是 HTTPS 搜索结果页")
    if not isinstance(html_text, str) or not html_text.strip():
        raise RuntimeError("无法读取当前搜索结果页内容")
    if len(html_text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise RuntimeError("当前搜索结果页超过 5 MiB，已停止采集")
    if not isinstance(user_agent, str):
        user_agent = ""
    if not isinstance(search_inputs, list):
        search_inputs = []

    hostname = urlsplit(current_url).hostname or ""
    cookie_pairs: list[str] = []
    cookies = cookie_result.get("cookies", [])
    if isinstance(cookies, list):
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            domain = str(cookie.get("domain", "")).lstrip(".").lower()
            name = cookie.get("name")
            value = cookie.get("value")
            if (
                isinstance(name, str)
                and isinstance(value, str)
                and domain
                and (hostname == domain or hostname.endswith(f".{domain}"))
            ):
                cookie_pairs.append(f"{name}={value}")
    return _BrowserCapture(
        url=current_url,
        html=html_text,
        cookie="; ".join(cookie_pairs),
        user_agent=user_agent,
        search_inputs=[item for item in search_inputs if isinstance(item, dict)],
    )


def _infer_search_keyword(capture: _BrowserCapture) -> str:
    """从当前 URL 与搜索输入框中自动识别本次搜索关键词。"""
    pairs = parse_qsl(urlsplit(capture.url).query, keep_blank_values=True)
    named_inputs = {
        str(item.get("name") or item.get("id") or "").lower(): str(item.get("value") or "")
        for item in capture.search_inputs
        if item.get("name") or item.get("id")
    }
    candidates: list[tuple[int, str]] = []
    for name, value in pairs:
        normalized_name = name.lower()
        if not value or _is_sensitive_name(name) or normalized_name in NON_SEARCH_PARAMETER_NAMES:
            continue
        priority = 0
        if normalized_name in SEARCH_PARAMETER_NAMES:
            priority = 3
        elif named_inputs.get(normalized_name) == value:
            priority = 2
        elif any(input_value == value for input_value in named_inputs.values()):
            priority = 1
        if priority:
            candidates.append((priority, value))
    if not candidates:
        raise ValueError(
            "无法从当前地址识别搜索关键词；请确认搜索后地址栏包含关键词，再重新采集"
        )
    best_priority = max(priority for priority, _ in candidates)
    values = {value for priority, value in candidates if priority == best_priority}
    if len(values) != 1:
        raise ValueError("当前地址包含多个搜索关键词，请只保留一次普通搜索后重试")
    return values.pop()


def _read_limited_response(response) -> bytes:
    """
    读取流式响应并限制最大字节数。

    :param response: ``RequestUtils`` 返回的响应对象
    :return: 不超过上限的响应字节
    :raises ValueError: 响应超过上限时抛出
    """
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = 0
        if declared_size > MAX_RESPONSE_BYTES:
            raise ValueError("搜索页响应超过 5 MiB，已停止采集")

    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("搜索页响应超过 5 MiB，已停止采集")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_html(content: bytes, response) -> str:
    """依据响应编码解码 HTML，无法确定时回退 UTF-8。"""
    encoding = response.encoding or getattr(response, "apparent_encoding", None) or "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _fetch_search_page(request: _CaptureRequest, cookie: str, user_agent: str) -> str:
    """
    通过 ``RequestUtils`` 获取搜索页，并仅跟随同源重定向。

    :param request: 已规范化的搜索请求
    :param cookie: 仅驻留内存的登录 Cookie
    :param user_agent: 仅用于本次请求且不写入采集包的浏览器 User-Agent
    :return: 搜索页 HTML 文本
    :raises RuntimeError: 请求失败、响应异常或发生跨域重定向时抛出
    """
    client = RequestUtils(
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{request.origin}/",
            "User-Agent": user_agent,
        },
        cookies=cookie,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    current_url = request.url
    params: Optional[dict[str, str]] = request.params

    for redirect_count in range(MAX_REDIRECTS + 1):
        response = client.get_res(
            url=current_url,
            params=params,
            allow_redirects=False,
            stream=True,
            verify=True,
        )
        params = None
        if response is None:
            raise RuntimeError("搜索页请求失败，请检查 URL、网络和 Cookie")
        try:
            status_code = response.status_code
            if status_code in REDIRECT_STATUS_CODES:
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError("站点返回了没有 Location 的重定向")
                next_url = urljoin(str(response.url or current_url), location)
                if not _same_origin(next_url, request.origin):
                    raise RuntimeError("站点尝试跨域重定向，为避免泄露 Cookie 已停止采集")
                if redirect_count >= MAX_REDIRECTS:
                    raise RuntimeError("站点重定向次数过多，已停止采集")
                current_url = next_url
                continue

            if status_code != 200:
                raise RuntimeError(f"搜索页返回 HTTP {status_code}")
            final_url = str(response.url or current_url)
            if not _same_origin(final_url, request.origin):
                raise RuntimeError("最终响应与输入 URL 不同源，已停止采集")
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type:
                raise RuntimeError(f"搜索页返回了非 HTML 内容：{content_type}")
            return _decode_html(_read_limited_response(response), response)
        finally:
            response.close()

    raise RuntimeError("站点重定向次数过多，已停止采集")


def _is_torrent_link(tag: Tag) -> bool:
    """判断链接是否指向种子详情或下载资源。"""
    if tag.name != "a":
        return False
    href = str(tag.get("href", ""))
    parsed = urlsplit(href)
    path = parsed.path.lower()
    basename = path.rsplit("/", 1)[-1]
    if basename in {"details.php", "download.php", "torrent.php", "viewtorrent.php"}:
        return True
    query_names = {name.lower() for name, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if basename == "torrents.php" and ({"torrentid", "id"} & query_names):
        return True
    return bool(TORRENT_PATH_PATTERN.search(path))


def _descriptor(tag: Tag) -> str:
    """合并标签的 id、class 和角色属性，供结构启发式判断使用。"""
    classes = tag.get("class", [])
    if isinstance(classes, str):
        classes = [classes]
    return " ".join(
        [str(tag.get("id", "")), *[str(item) for item in classes], str(tag.get("role", ""))]
    )


def _deduplicate_nodes(nodes: list[Tag]) -> list[Tag]:
    """移除重复或已被父级候选覆盖的 DOM 节点。"""
    unique: list[Tag] = []
    seen: set[int] = set()
    for node in nodes:
        if id(node) in seen:
            continue
        seen.add(id(node))
        if any(parent is existing for parent in node.parents for existing in unique):
            continue
        unique = [existing for existing in unique if node not in existing.parents]
        unique.append(node)
    return unique


def _find_candidate_nodes(soup: BeautifulSoup) -> list[Tag]:
    """
    从完整页面中定位最小的种子列表相关 DOM 节点。

    :param soup: 完整搜索页 DOM
    :return: 表格或独立结果项节点，最多保留约定数量
    """
    links = [tag for tag in soup.find_all("a") if _is_torrent_link(tag)]
    tables = _deduplicate_nodes(
        [table for link in links if (table := link.find_parent("table")) is not None]
    )
    descriptor_tables = [
        table
        for table in soup.find_all("table")
        if RESULT_DESCRIPTOR_PATTERN.search(_descriptor(table))
        and len(table.find_all("tr")) > 1
    ]
    tables = _deduplicate_nodes([*tables, *descriptor_tables])
    if tables:
        return tables

    items: list[Tag] = []
    for link in links:
        item = link.find_parent(["article", "li"])
        if item is None:
            for parent in link.parents:
                if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
                    break
                if RESULT_DESCRIPTOR_PATTERN.search(_descriptor(parent)):
                    item = parent
                    break
        if item is None:
            item = link.find_parent("div")
        if item is not None:
            items.append(item)
    return _deduplicate_nodes(items)[:MAX_RESULT_ROWS]


def _trim_table(table: Tag, max_rows: int) -> tuple[Tag, int, int]:
    """
    裁剪表格中的非结果内容并限制种子结果行数。

    :param table: 待裁剪的种子表格
    :param max_rows: 本表格最多保留的结果行数
    :return: 裁剪后的表格、保留行数和丢弃行数
    """
    cloned = copy.deepcopy(table)
    rows = [
        row
        for row in cloned.find_all("tr")
        if row.find_parent("table") is cloned
    ]
    result_rows = [row for row in rows if any(_is_torrent_link(link) for link in row.find_all("a"))]
    if not result_rows:
        result_rows = [row for row in rows if not row.find("th")]

    discarded = 0
    kept_result_ids = {id(row) for row in result_rows[:max_rows]}
    for row in rows:
        is_header = row.find("th") is not None
        if not is_header and id(row) not in kept_result_ids:
            row.decompose()
            discarded += 1
    return cloned, min(len(result_rows), max_rows), discarded


def _sanitize_url(value: str, origin: str, stats: _RedactionStats) -> str:
    """把 DOM URL 转为不含站点凭据和资源标识的相对结构地址。"""
    stripped = value.strip()
    if not stripped or stripped.startswith(("#", "data:")):
        return stripped
    if stripped.lower().startswith(("javascript:", "mailto:", "tel:")):
        stats.redacted_urls += 1
        return "#"

    absolute_url = urljoin(f"{origin}/", stripped)
    parsed = urlsplit(absolute_url)
    if not _same_origin(absolute_url, origin):
        stats.redacted_urls += 1
        return "#external-resource"

    path_segments = []
    redact_next_segment = False
    for segment in parsed.path.split("/"):
        if redact_next_segment and segment:
            path_segments.append("{identity}")
            redact_next_segment = False
            continue
        path_segments.append(_sanitize_path_segment(segment))
        if segment.lower() in {"author", "member", "members", "profile", "profiles", "user", "users"}:
            redact_next_segment = True
    safe_pairs = []
    for name, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if _is_sensitive_name(name):
            continue
        safe_pairs.append((name, _query_value_placeholder(name)))
    stats.redacted_urls += 1
    query = urlencode(safe_pairs)
    return urlunsplit(("", "", "/".join(path_segments) or "/", query, ""))


def _query_value_placeholder(name: str) -> str:
    """为 URL 查询参数生成不含原值但保留解析语义的合成值。"""
    normalized_name = name.lower()
    if (
        normalized_name in {"cat", "category", "id", "page", "tid", "torrentid", "type"}
        or normalized_name.endswith("id")
    ):
        return "1"
    return "{value}"


def _sanitize_path_segment(segment: str) -> str:
    """保留路由语义，并替换 URL 路径中的资源名和对象标识。"""
    if not segment:
        return segment
    if _is_sensitive_name(segment):
        return "{redacted}"
    if segment.isdigit():
        return "{id}"
    if HIGH_ENTROPY_VALUE_PATTERN.fullmatch(segment) or not segment.isascii():
        return "{value}"

    dot_index = segment.rfind(".")
    suffix = segment[dot_index:].lower() if dot_index > 0 else ""
    if suffix and suffix not in STRUCTURAL_PAGE_EXTENSIONS:
        safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,6}", suffix) else ""
        return f"{{asset}}{safe_suffix}"
    if len(segment) >= 16 and re.search(r"[-_\d]", segment):
        return "{value}"
    return segment


def _placeholder_for_text(value: str) -> str:
    """根据结果文本形态生成不携带原始内容的占位值。"""
    if not value.strip():
        return value
    if SIZE_PATTERN.fullmatch(value):
        return "1.00 GiB"
    if DATE_TIME_PATTERN.search(value):
        return "2000-01-01 00:00"
    if NUMBER_PATTERN.fullmatch(value):
        return "0"
    return "[REDACTED]"


def _sanitize_selector_token(value: str) -> str:
    """脱敏 class/id 中的身份后缀，同时保留可用于选择器的结构前缀。"""
    redacted = EMAIL_PATTERN.sub("redacted", value)
    redacted = IP_ADDRESS_PATTERN.sub("redacted", redacted)
    if HIGH_ENTROPY_VALUE_PATTERN.search(redacted):
        return "redacted"
    identity_match = re.match(
        r"^(profile|uploader|uploaded[-_]by|user|username|member|author)([-_]).+$",
        redacted,
        flags=re.IGNORECASE,
    )
    if identity_match:
        return f"{identity_match.group(1)}{identity_match.group(2)}redacted"
    return redacted


def _sanitize_selector_attribute(value: object) -> Union[list[str], str]:
    """逐项脱敏 BeautifulSoup 的 class 列表或普通 id 字符串。"""
    if isinstance(value, list):
        return [_sanitize_selector_token(str(item)) for item in value]
    return _sanitize_selector_token(str(value))


def _redact_result_text(node: Tag, stats: _RedactionStats) -> None:
    """替换种子结果项中的标题、用户、时间和统计值。"""
    for text_node in list(node.find_all(string=True)):
        if not isinstance(text_node, NavigableString) or not text_node.strip():
            continue
        parent = text_node.parent
        if parent and parent.name in {"th", "thead"}:
            continue
        replacement = _placeholder_for_text(str(text_node))
        if replacement != str(text_node):
            text_node.replace_with(replacement)
            stats.redacted_result_values += 1


def _sanitize_dom(root: Tag, origin: str, stats: _RedactionStats) -> None:
    """移除活动内容、隐藏字段、身份信息和可疑属性。"""
    for comment in list(root.find_all(string=lambda value: isinstance(value, Comment))):
        comment.extract()
        stats.discarded_nodes += 1

    for node in list(root.find_all(["script", "style", "iframe", "object", "embed", "noscript", "template"])):
        node.decompose()
        stats.discarded_nodes += 1

    for hidden_input in list(root.find_all("input", attrs={"type": re.compile("hidden", re.IGNORECASE)})):
        hidden_input.decompose()
        stats.discarded_nodes += 1

    for tag in root.find_all(True):
        descriptor = " ".join(
            [
                _descriptor(tag),
                str(tag.get("href", "")),
                str(tag.get("name", "")),
            ]
        )
        is_identity_element = (
            tag.name in {"a", "span", "div"}
            and bool(IDENTITY_DESCRIPTOR_PATTERN.search(descriptor))
        )
        if is_identity_element:
            tag.clear()
            tag.append("[REDACTED_USER]")
            stats.redacted_identity_values += 1

        for attribute_name in list(tag.attrs):
            lowered_name = attribute_name.lower()
            is_allowed = (
                lowered_name in ALLOWED_ATTRIBUTE_NAMES
                or lowered_name in URL_ATTRIBUTE_NAMES
                or lowered_name in URL_SET_ATTRIBUTE_NAMES
                or lowered_name in TEXT_ATTRIBUTE_NAMES
            )
            if not is_allowed:
                del tag.attrs[attribute_name]
                stats.removed_attributes += 1
                continue
            attribute_value = tag.attrs[attribute_name]
            if lowered_name in {"class", "id"}:
                tag.attrs[attribute_name] = _sanitize_selector_attribute(attribute_value)
                continue
            if lowered_name in URL_ATTRIBUTE_NAMES:
                if is_identity_element:
                    tag.attrs[attribute_name] = "#redacted-identity"
                    stats.redacted_urls += 1
                else:
                    tag.attrs[attribute_name] = _sanitize_url(str(attribute_value), origin, stats)
                continue
            if lowered_name in URL_SET_ATTRIBUTE_NAMES:
                tag.attrs[attribute_name] = "[REDACTED_URL_SET]"
                stats.redacted_urls += 1
                continue
            if lowered_name in TEXT_ATTRIBUTE_NAMES:
                tag.attrs[attribute_name] = _placeholder_for_text(str(attribute_value))
                stats.redacted_result_values += 1
                continue
            if lowered_name.startswith("data-") and lowered_name != "data-capture-kind":
                tag.attrs[attribute_name] = _placeholder_for_text(str(attribute_value))
                stats.redacted_sensitive_values += 1
                continue
            serialized_value = " ".join(attribute_value) if isinstance(attribute_value, list) else str(attribute_value)
            if lowered_name in {"content", "value"}:
                tag.attrs[attribute_name] = "[REDACTED]"
                stats.redacted_sensitive_values += 1
            elif HIGH_ENTROPY_VALUE_PATTERN.search(serialized_value):
                tag.attrs[attribute_name] = "redacted"
                stats.redacted_sensitive_values += 1

    for text_node in list(root.find_all(string=True)):
        value = str(text_node)
        redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
        redacted = IP_ADDRESS_PATTERN.sub("[REDACTED_IP]", redacted)
        redacted = HIGH_ENTROPY_VALUE_PATTERN.sub("[REDACTED_VALUE]", redacted)
        if redacted != value:
            text_node.replace_with(redacted)
            stats.redacted_sensitive_values += 1


def _sanitize_search_html(
    html: str,
    origin: str,
    keyword: str,
) -> tuple[str, int, dict]:
    """
    本地裁剪并脱敏种子列表 DOM，不保留原始页面。

    :param html: 原始搜索页 HTML，仅在内存中存在
    :param origin: 搜索站点 origin
    :param keyword: 本次搜索关键词
    :return: 脱敏 HTML、采集行数和脱敏报告
    :raises ValueError: 页面中无法定位种子结果结构时抛出
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = _find_candidate_nodes(soup)
    if not candidates:
        raise ValueError("页面中未找到种子列表，请确认 Cookie 有效且 URL 指向搜索结果页")

    output = BeautifulSoup(
        '<!doctype html><html><head><meta charset="utf-8"><title>Site search structure</title></head><body></body></html>',
        "html.parser",
    )
    container = output.new_tag("main", attrs={"data-capture-kind": "torrent-search"})
    output.body.append(container)
    stats = _RedactionStats()
    row_count = 0

    for candidate in candidates:
        remaining_rows = MAX_RESULT_ROWS - row_count
        if remaining_rows <= 0:
            break
        if candidate.name == "table":
            cloned, captured_rows, discarded = _trim_table(candidate, remaining_rows)
            row_count += captured_rows
            stats.discarded_nodes += discarded
        else:
            cloned = copy.deepcopy(candidate)
            row_count += 1
        _redact_result_text(cloned, stats)
        container.append(cloned)

    _sanitize_dom(output, origin, stats)
    if row_count < MIN_RESULT_ROWS:
        raise ValueError(
            f"仅定位到 {row_count} 条种子结果，请换用更宽泛的关键词，至少采集 {MIN_RESULT_ROWS} 条"
        )
    sanitized_html = str(output)
    if keyword:
        sanitized_html, count = re.subn(re.escape(keyword), "{keyword}", sanitized_html, flags=re.IGNORECASE)
        stats.redacted_sensitive_values += count
    report = {
        "redacted": True,
        "contains_credentials": False,
        "strategy": "local-dom-cropping-and-allowlist",
        "captured_rows": row_count,
        "changes": {
            "discarded_nodes": stats.discarded_nodes,
            "removed_attributes": stats.removed_attributes,
            "redacted_urls": stats.redacted_urls,
            "redacted_identity_values": stats.redacted_identity_values,
            "redacted_result_values": stats.redacted_result_values,
            "redacted_sensitive_values": stats.redacted_sensitive_values,
        },
    }
    return sanitized_html, row_count, report


def _sha256(value: bytes) -> str:
    """计算字节内容的 SHA-256 摘要。"""
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: dict) -> bytes:
    """把字典序列化为稳定、可读的 UTF-8 JSON。"""
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _credential_values(cookie: str) -> list[str]:
    """提取需要在最终采集包中执行二次检查的较长凭据值。"""
    raw_values = [cookie.strip()]
    for part in cookie.split(";"):
        _, separator, value = part.partition("=")
        if separator:
            raw_values.append(value.strip())
    values = {cookie.strip()} if cookie.strip() else set()
    values.update({
        variant
        for raw_value in raw_values
        for variant in (raw_value, unquote(raw_value), quote(raw_value, safe=""))
        if len(variant) >= 8
    })
    return sorted(values)


def _verify_payload(payload: dict[str, bytes], cookie: str) -> None:
    """
    验证采集包文件集合及内容中不包含已知凭据。

    :param payload: 待写入 ZIP 的根目录文件
    :param cookie: 用户本次输入的 Cookie，仅用于内存比对
    :raises ValueError: 协议不完整或发现凭据值时抛出
    """
    if tuple(payload) != ARCHIVE_FILE_NAMES:
        raise ValueError("采集包文件协议不完整")
    combined = b"\n".join(payload.values())
    for value in _credential_values(cookie):
        if value.encode("utf-8") in combined:
            raise ValueError("脱敏检查发现采集包仍包含凭据值，已停止写入")

    combined_text = combined.decode("utf-8", errors="replace")
    if EMAIL_PATTERN.search(combined_text) or IP_ADDRESS_PATTERN.search(combined_text):
        raise ValueError("脱敏检查发现采集包仍包含身份信息，已停止写入")

    for name, content in payload.items():
        scan_text = content.decode("utf-8", errors="replace")
        if name == "manifest.json":
            manifest = json.loads(scan_text)
            manifest["files"] = {}
            scan_text = json.dumps(manifest, ensure_ascii=False)
        if HIGH_ENTROPY_VALUE_PATTERN.search(scan_text):
            raise ValueError("脱敏检查发现采集包仍包含疑似凭据，已停止写入")


def _build_payload(request: _CaptureRequest, html: str, cookie: str) -> dict[str, bytes]:
    """构造符合站点适配协议的四个根目录文件。"""
    sanitized_html, row_count, report = _sanitize_search_html(
        html=html,
        origin=request.origin,
        keyword=next(
            request.params[name]
            for name, value in request.public_params.items()
            if value == "{keyword}"
        ),
    )
    html_bytes = sanitized_html.encode("utf-8")
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "format_version": FORMAT_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "site": {
            "id": request.site_id,
            "name": request.site_name,
            "domain": request.origin,
            "public": False,
        },
        "capture": {
            "kind": "search",
            "captured_at": captured_at,
            "row_count": row_count,
        },
        "files": {"search.html": _sha256(html_bytes)},
        "privacy": {
            "redacted": True,
            "contains_credentials": False,
        },
    }
    public_request = {
        "method": "get",
        "path": request.path,
        "params": request.public_params,
        "origin": request.origin,
    }
    payload = {
        "manifest.json": _json_bytes(manifest),
        "request.json": _json_bytes(public_request),
        "search.html": html_bytes,
        "redaction-report.json": _json_bytes(report),
    }
    _verify_payload(payload, cookie)
    return payload


def _write_archive(payload: dict[str, bytes], output_dir: Path, site_id: str) -> Path:
    """把固定文件集合写入 ZIP 根目录并返回文件路径。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = output_dir / f"moviepilot-site-capture-{site_id}-{timestamp}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in payload.items():
            archive.writestr(name, content)
    return archive_path


def collect_site_capture(
    url: str,
    keyword: str,
    cookie: str,
    output_dir: Path,
    user_agent: str = DEFAULT_USER_AGENT,
    site_name: Optional[str] = None,
) -> Path:
    """
    获取、脱敏并打包一个站点搜索页。

    :param url: 搜索页 URL，需含搜索参数或 ``{keyword}`` 占位符
    :param keyword: 本次请求使用的搜索关键词
    :param cookie: 登录 Cookie，仅在采集期间驻留内存
    :param output_dir: ZIP 输出目录
    :param user_agent: 可选浏览器 User-Agent，仅用于请求且不会写入采集包
    :param site_name: 可选站点显示名称，留空时使用域名
    :return: 生成的 ZIP 文件路径
    """
    request = _prepare_capture_request(url=url, keyword=keyword, site_name=site_name)
    html = _fetch_search_page(request=request, cookie=cookie, user_agent=user_agent)
    payload = _build_payload(request=request, html=html, cookie=cookie)
    return _write_archive(payload=payload, output_dir=output_dir, site_id=request.site_id)


def collect_site_capture_with_browser(start_url: str, output_dir: Path) -> Path:
    """
    启动临时浏览器，让用户完成登录和搜索后生成脱敏采集包。

    :param start_url: 用户要访问的站点 HTTPS 地址
    :param output_dir: ZIP 输出目录
    :return: 生成的 ZIP 文件路径
    """
    normalized_start_url = _normalize_user_start_url(start_url)
    origin, _, _ = _normalize_origin(normalized_start_url)
    with _launch_browser_session(normalized_start_url) as browser_session:
        print("\n浏览器已打开，请在临时窗口中完成以下操作：")
        print("1. 登录站点。")
        print("2. 搜索一个能返回至少 3 条结果的常见关键词。")
        print("3. 保持搜索结果页打开，然后回到这里按回车。")
        input("\n完成后按回车开始脱敏采集：")
        capture = _read_browser_capture(browser_session)

    capture_origin, _, _ = _normalize_origin(capture.url)
    if capture_origin != origin:
        print(f"提示：浏览器最终进入了 {capture_origin}，将按该地址生成站点配置。")
    keyword = _infer_search_keyword(capture)
    request = _prepare_capture_request(
        url=capture.url,
        keyword=keyword,
    )
    payload = _build_payload(request=request, html=capture.html, cookie=capture.cookie)
    return _write_archive(payload=payload, output_dir=output_dir, site_id=request.site_id)


def _parse_args() -> argparse.Namespace:
    """解析普通浏览器模式和高级手动 Cookie 模式的命令行参数。"""
    parser = argparse.ArgumentParser(description="采集并脱敏 MoviePilot 站点适配所需的搜索页结构")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="采集 ZIP 输出目录，默认当前目录",
    )
    parser.add_argument(
        "--manual-cookie",
        action="store_true",
        help="高级模式：手工输入搜索 URL、关键词和 Cookie，不启动临时浏览器",
    )
    return parser.parse_args()


def _configure_standard_streams() -> None:
    """将可重配置的标准输出流统一为 UTF-8，避免 Windows 本地代码页无法输出中文。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (LookupError, OSError):
            continue


def main() -> int:
    """交互式读取采集参数并生成站点适配 ZIP。"""
    _configure_standard_streams()
    args = _parse_args()
    print("MoviePilot 站点适配采集器")
    print("原始页面不会落盘；临时浏览器关闭后会清理登录状态，输出前会裁剪并脱敏。")
    try:
        if args.manual_cookie:
            url = input("搜索结果页 URL：").strip()
            keyword = input("刚才使用的搜索关键词：").strip()
            cookie = getpass.getpass("Cookie（输入不会回显）：").strip()
            if not cookie:
                raise ValueError("Cookie 不能为空")
            archive_path = collect_site_capture(
                url=url,
                keyword=keyword,
                cookie=cookie,
                output_dir=args.output_dir,
            )
        else:
            start_url = input("请输入站点地址（例如 https://tracker.example.com）：").strip()
            archive_path = collect_site_capture_with_browser(
                start_url=start_url,
                output_dir=args.output_dir,
            )
    except (EOFError, KeyboardInterrupt):
        print("\n已取消采集。", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError) as error:
        print(f"采集失败：{str(error)}", file=sys.stderr)
        return 1

    print(f"采集完成：{archive_path}")
    print("请在站点适配 Feature Request 中附加此 ZIP；不要上传 Cookie 或原始 HTML。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
