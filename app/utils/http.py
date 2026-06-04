import asyncio
import collections
import re
import sys
import threading
import weakref
from contextlib import AsyncExitStack, contextmanager, asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Tuple, Union

import chardet
import httpx
import requests
import urllib3
from requests import Response, Session
from urllib3.exceptions import InsecureRequestWarning
from urllib.parse import unquote, quote

from app.core.config import settings
from app.log import logger

urllib3.disable_warnings(InsecureRequestWarning)


class _NonClosingTransportProxy(httpx.AsyncBaseTransport):
    """
    包装共享底层 transport，转发请求但吞掉 __aexit__/aclose 调用。
    防止 per-call AsyncClient 在 async with 退出时把底层连接池一并清空。
    底层 transport 的真正关闭由 aclose_shared_async_transports() 统一管理。
    """

    __slots__ = ("_wrapped",)

    def __init__(self, wrapped: httpx.AsyncBaseTransport):
        self._wrapped = wrapped

    async def __aenter__(self):  # pragma: no cover - 简单转发
        return self

    async def __aexit__(self, exc_type=None, exc_value=None, traceback=None) -> None:
        # 故意 no-op：不向底层 transport 传播 __aexit__，避免连接池被清空
        return None

    async def aclose(self) -> None:
        # 故意 no-op：调用方显式 aclose 也不影响共享池
        return None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._wrapped.handle_async_request(request)


_SharedTransportKey = Tuple[
    Optional[str],          # proxy
    Union[bool, str],       # verify
    bool,                   # http2
    int,                    # max_keepalive_connections
    int,                    # max_connections
    int,                    # keepalive_expiry
]

# 共享底层 transport 桶，按事件循环和配置区分，支持 LRU 淘汰
_shared_async_transports: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, collections.OrderedDict[_SharedTransportKey, httpx.AsyncHTTPTransport]] = weakref.WeakKeyDictionary()
# 不同线程各自驱动的事件循环并发首次写入外层弱字典时，需要互斥保护
_shared_async_transports_lock = threading.Lock()
# 每个事件循环允许的最大共享 transport 桶数；超出后按 LRU 淘汰最久未用桶。
_MAX_SHARED_TRANSPORTS_PER_LOOP = 32
# 默认的最大 keep-alive 连接数
_DEFAULT_MAX_KEEPALIVE_CONNECTIONS = 20
# 默认的最大连接数（包括 keep-alive 和非 keep-alive 连接）
_DEFAULT_MAX_CONNECTIONS = 40
# 默认的 keep-alive 连接过期时间（秒）
_DEFAULT_KEEPALIVE_EXPIRY = 30
# 同步 requests.Session 复用连接时，遇到对端或代理关闭 keep-alive 后允许重试的方法
_REQUESTS_RETRY_IDEMPOTENT_METHODS = ("GET", "HEAD", "OPTIONS")
# 持有 LRU 淘汰后正在异步关闭的 transport task，避免 fire-and-forget 被 GC 警告
_pending_eviction_tasks: set[asyncio.Task] = set()


def _get_shared_async_transport(
    proxy: Optional[str],
    verify: Union[bool, str],
    http2: bool,
    max_keepalive_connections: int,
    max_connections: int,
    keepalive_expiry: int,
) -> Optional[httpx.AsyncHTTPTransport]:
    """
    返回与当前事件循环绑定的共享 AsyncHTTPTransport（底层连接池）；首次按需创建。
    没有运行中的事件循环或循环已关闭时返回 None，由调用方走临时客户端兜底。

    Transport 只持有连接池、SSL、代理；cookies/timeout/follow_redirects 等
    会话级状态由调用方在外层 AsyncClient(transport=...) 实例化时单独配置，
    每次调用用完即销毁，因此天然无 jar 累积串扰。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if loop.is_closed():
        return None

    with _shared_async_transports_lock:
        per_loop = _shared_async_transports.get(loop)
        if per_loop is None:
            per_loop = collections.OrderedDict()
            _shared_async_transports[loop] = per_loop

    key: _SharedTransportKey = (
        proxy,
        verify,
        http2,
        max_keepalive_connections,
        max_connections,
        keepalive_expiry,
    )
    transport = per_loop.get(key)
    if transport is not None:
        per_loop.move_to_end(key)  # LRU 触摸
        return transport

    # 首次见到这个配置，创建新的共享 transport 桶
    transport = httpx.AsyncHTTPTransport(
        http2=http2,
        proxy=proxy,
        verify=verify,
        limits=httpx.Limits(
            max_keepalive_connections=max_keepalive_connections,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
        ),
    )
    per_loop[key] = transport

    # LRU 淘汰：超出上限时关闭并移除最久未用桶
    while len(per_loop) > _MAX_SHARED_TRANSPORTS_PER_LOOP:
        evicted_key, evicted_transport = per_loop.popitem(last=False)
        try:
            task = loop.create_task(evicted_transport.aclose())
            # 强引用避免 task 仅被 loop 弱持有而触发 "Task was destroyed but pending"
            _pending_eviction_tasks.add(task)
            task.add_done_callback(_pending_eviction_tasks.discard)
        except Exception as e:  # pragma: no cover - 防御性
            logger.debug(f"LRU 淘汰共享 transport 时调度关闭失败: {e!r}")

    return transport


async def aclose_shared_async_transports() -> None:
    """
    关闭当前事件循环下所有共享 AsyncHTTPTransport，释放底层连接池。
    建议在应用关闭流程（如 FastAPI shutdown 事件）中调用，避免 ResourceWarning。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # 弹出而非 get+clear，避免外层 dict 残留空 OrderedDict 占位
    with _shared_async_transports_lock:
        per_loop = _shared_async_transports.pop(loop, None)
    if not per_loop:
        return
    transports = list(per_loop.values())
    per_loop.clear()
    # 并行关闭：每个 transport 的 TLS close_notify 各占一个 RTT，
    # 顺序等待会线性放大 shutdown 耗时；return_exceptions 让单点失败
    # 不影响其他 transport 的释放
    results = await asyncio.gather(
        *(t.aclose() for t in transports), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.debug(f"关闭共享 AsyncHTTPTransport 失败: {result!r}")


def _url_decode_if_latin(original: str) -> str:
    """
    解码URL编码的字符串，只解码文本，二进制数据保持不变
    :param original: URL编码字符串
    :return: 解码后的字符串或原始二进制数据
    """
    try:
        # 先解码
        decoded = unquote(original, encoding="latin-1")
        # 再完整编码
        fully_encoded = quote(decoded, safe="")
        # 验证
        decoded_again = unquote(fully_encoded, encoding="latin-1")
        if decoded_again == decoded:
            return decoded
    except Exception as e:
        logger.error(f"latin-1解码URL编码失败：{e}")
    return original


def cookie_parse(cookies_str: str, array: bool = False) -> Union[list, dict]:
    """
    解析cookie，转化为字典或者数组
    :param cookies_str: cookie字符串
    :param array: 是否转化为数组
    :return: 字典或者数组
    """
    if not cookies_str:
        return {}

    cookie_dict = {}
    cookies = cookies_str.split(";")
    for cookie in cookies:
        cstr = cookie.split("=", 1)  # 只分割第一个=，因为value可能包含=
        if len(cstr) > 1:
            # URL解码Cookie值（但保留Cookie名不解码）
            cookie_dict[cstr[0].strip()] = _url_decode_if_latin(cstr[1].strip())
    if array:
        return [{"name": k, "value": v} for k, v in cookie_dict.items()]
    return cookie_dict


def get_caller():
    """
    获取调用者的名称，识别是否为插件调用
    """
    # 调用者名称
    caller_name = None

    try:
        frame = sys._getframe(3)  # noqa
    except (AttributeError, ValueError):
        return None

    while frame:
        filepath = Path(frame.f_code.co_filename)
        parts = filepath.parts
        if "app" in parts:
            if not caller_name and "plugins" in parts:
                try:
                    plugins_index = parts.index("plugins")
                    if plugins_index + 1 < len(parts):
                        plugin_candidate = parts[plugins_index + 1]
                        if plugin_candidate != "__init__.py":
                            caller_name = plugin_candidate
                        break
                except ValueError:
                    pass
            if "main.py" in parts:
                break
        elif len(parts) != 1:
            break
        try:
            frame = frame.f_back
        except AttributeError:
            break
    return caller_name


class RequestUtils:
    """
    HTTP请求工具类，提供同步HTTP请求的基本功能
    """

    def __init__(
        self,
        headers: dict = None,
        ua: str = None,
        cookies: Union[str, dict] = None,
        proxies: dict = None,
        session: Session = None,
        timeout: int = None,
        referer: str = None,
        content_type: str = None,
        accept_type: str = None,
    ):
        """
        :param headers: 请求头部信息
        :param ua: User-Agent字符串
        :param cookies: Cookie字符串或字典
        :param proxies: 代理设置
        :param session: requests.Session实例，如果为None则创建新的Session
        :param timeout: 请求超时时间，默认为20秒
        :param referer: Referer头部信息
        :param content_type: 请求的Content-Type，默认为 "application/x-www-form-urlencoded; charset=UTF-8"
        :param accept_type: Accept头部信息，默认为 "application/json"
        """
        self._proxies = proxies
        self._session = session
        self._timeout = timeout or 20
        if not content_type:
            content_type = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            self._headers = headers
        else:
            if ua and ua == settings.USER_AGENT:
                caller_name = get_caller()
                if caller_name:
                    ua = f"{settings.USER_AGENT} Plugin/{caller_name}"
            self._headers = {
                "User-Agent": ua,
                "Content-Type": content_type,
                "Accept": accept_type,
                "referer": referer,
            }
        if cookies:
            if isinstance(cookies, str):
                self._cookies = cookie_parse(cookies)
            else:
                self._cookies = cookies
        else:
            self._cookies = None

    @contextmanager
    def response_manager(self, method: str, url: str, **kwargs):
        """
        响应管理器上下文管理器，确保响应对象被正确关闭
        :param method: HTTP方法
        :param url: 请求的URL
        :param kwargs: 其他请求参数
        """
        response = None
        try:
            response = self.request(method=method, url=url, **kwargs)
            yield response
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception as e:
                    logger.debug(f"关闭响应失败: {e}")

    def request(
        self, method: str, url: str, raise_exception: bool = False, **kwargs
    ) -> Optional[Response]:
        """
        发起HTTP请求
        :param method: HTTP方法，如 get, post, put 等
        :param url: 请求的URL
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象
        :raises: requests.exceptions.RequestException 仅raise_exception为True时会抛出
        """
        if self._session is None:
            req_method = requests.request
        else:
            req_method = self._session.request
        kwargs.setdefault("headers", self._headers)
        kwargs.setdefault("cookies", self._cookies)
        kwargs.setdefault("proxies", self._proxies)
        kwargs.setdefault("timeout", self._timeout)
        kwargs.setdefault("verify", False)
        kwargs.setdefault("stream", False)
        method_upper = method.upper()
        try:
            return req_method(method, url, **kwargs)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout,
        ) as e:
            if (
                self._session is not None
                and method_upper in _REQUESTS_RETRY_IDEMPOTENT_METHODS
            ):
                logger.debug(f"keep-alive 连接已失效，同步幂等请求重试一次: {e!r}")
                try:
                    self._session.close()
                    return req_method(method, url, **kwargs)
                except requests.exceptions.RequestException as retry_error:
                    error_msg = (
                        str(retry_error)
                        if str(retry_error)
                        else f"未知网络错误 (URL: {url}, Method: {method_upper})"
                    )
                    logger.debug(f"重试后同步请求仍失败: {error_msg}")
                    if raise_exception:
                        raise
                    return None
            error_msg = (
                str(e)
                if str(e)
                else f"未知网络错误 (URL: {url}, Method: {method_upper})"
            )
            logger.debug(f"同步请求失败(不重试): {error_msg}")
            if raise_exception:
                raise
            return None
        except requests.exceptions.RequestException as e:
            # 获取更详细的错误信息
            error_msg = (
                str(e)
                if str(e)
                else f"未知网络错误 (URL: {url}, Method: {method_upper})"
            )
            logger.debug(f"请求失败: {error_msg}")
            if raise_exception:
                raise
            return None

    def get(self, url: str, params: dict = None, **kwargs) -> Optional[str]:
        """
        发送GET请求
        :param url: 请求的URL
        :param params: 请求的参数
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: 响应的内容，若发生RequestException则返回None
        """
        response = self.request(method="get", url=url, params=params, **kwargs)
        try:
            if response:
                try:
                    content = str(response.content, "utf-8")
                    return content
                except Exception as e:
                    logger.debug(f"处理响应内容失败: {e}")
                    return None
            return None
        finally:
            if response is not None:
                response.close()

    def post(
        self, url: str, data: Any = None, json: dict = None, **kwargs
    ) -> Optional[Response]:
        """
        发送POST请求
        :param url: 请求的URL
        :param data: 请求的数据
        :param json: 请求的JSON数据
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestException则返回None
        """
        return self.request(method="post", url=url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs) -> Optional[Response]:
        """
        发送PUT请求
        :param url: 请求的URL
        :param data: 请求的数据
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestException则返回None
        """
        return self.request(method="put", url=url, data=data, **kwargs)

    def get_res(
        self,
        url: str,
        params: dict = None,
        data: Any = None,
        json: dict = None,
        allow_redirects: bool = True,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[Response]:
        """
        发送GET请求并返回响应对象
        :param url: 请求的URL
        :param params: 请求的参数
        :param data: 请求的数据
        :param json: 请求的JSON数据
        :param allow_redirects: 是否允许重定向
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestException则返回None
        :raises: requests.exceptions.RequestException 仅raise_exception为True时会抛出
        """
        return self.request(
            method="get",
            url=url,
            params=params,
            data=data,
            json=json,
            allow_redirects=allow_redirects,
            raise_exception=raise_exception,
            **kwargs,
        )

    @contextmanager
    def get_stream(self, url: str, params: dict = None, **kwargs):
        """
        获取流式响应的上下文管理器，适用于大文件下载
        :param url: 请求的URL
        :param params: 请求的参数
        :param kwargs: 其他请求参数
        """
        kwargs["stream"] = True
        response = self.request(method="get", url=url, params=params, **kwargs)
        try:
            yield response
        finally:
            if response is not None:
                response.close()

    def post_res(
        self,
        url: str,
        data: Any = None,
        params: dict = None,
        allow_redirects: bool = True,
        files: Any = None,
        json: dict = None,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[Response]:
        """
        发送POST请求并返回响应对象
        :param url: 请求的URL
        :param data: 请求的数据
        :param params: 请求的参数
        :param allow_redirects: 是否允许重定向
        :param files: 请求的文件
        :param json: 请求的JSON数据
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestException则返回None
        :raises: requests.exceptions.RequestException 仅raise_exception为True时会抛出
        """
        return self.request(
            method="post",
            url=url,
            data=data,
            params=params,
            allow_redirects=allow_redirects,
            files=files,
            json=json,
            raise_exception=raise_exception,
            **kwargs,
        )

    def put_res(
        self,
        url: str,
        data: Any = None,
        params: dict = None,
        allow_redirects: bool = True,
        files: Any = None,
        json: dict = None,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[Response]:
        """
        发送PUT请求并返回响应对象
        :param url: 请求的URL
        :param data: 请求的数据
        :param params: 请求的参数
        :param allow_redirects: 是否允许重定向
        :param files: 请求的文件
        :param json: 请求的JSON数据
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestException则返回None
        :raises: requests.exceptions.RequestException 仅raise_exception为True时会抛出
        """
        return self.request(
            method="put",
            url=url,
            data=data,
            params=params,
            allow_redirects=allow_redirects,
            files=files,
            json=json,
            raise_exception=raise_exception,
            **kwargs,
        )

    def delete_res(
        self,
        url: str,
        data: Any = None,
        params: dict = None,
        allow_redirects: bool = True,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[Response]:
        """
        发送DELETE请求并返回响应对象
        :param url: 请求的URL
        :param data: 请求的数据
        :param params: 请求的参数
        :param allow_redirects: 是否允许重定向
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestException则返回None
        :raises: requests.exceptions.RequestException 仅raise_exception为True时会抛出
        """
        return self.request(
            method="delete",
            url=url,
            data=data,
            params=params,
            allow_redirects=allow_redirects,
            raise_exception=raise_exception,
            **kwargs,
        )

    def get_json(self, url: str, params: dict = None, **kwargs) -> Optional[dict]:
        """
        发送GET请求并返回JSON数据，自动关闭连接
        :param url: 请求的URL
        :param params: 请求的参数
        :param kwargs: 其他请求参数
        :return: JSON数据，若发生异常则返回None
        """
        response = self.request(method="get", url=url, params=params, **kwargs)
        try:
            if response:
                try:
                    data = response.json()
                    return data
                except Exception as e:
                    logger.debug(f"解析JSON失败: {e}")
                    return None
            return None
        finally:
            if response is not None:
                response.close()

    def post_json(
        self, url: str, data: Any = None, json: dict = None, **kwargs
    ) -> Optional[dict]:
        """
        发送POST请求并返回JSON数据，自动关闭连接
        :param url: 请求的URL
        :param data: 请求的数据
        :param json: 请求的JSON数据
        :param kwargs: 其他请求参数
        :return: JSON数据，若发生异常则返回None
        """
        if json is None:
            json = {}
        response = self.request(method="post", url=url, data=data, json=json, **kwargs)
        try:
            if response:
                try:
                    data = response.json()
                    return data
                except Exception as e:
                    logger.debug(f"解析JSON失败: {e}")
                    return None
            return None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def parse_cache_control(header: str) -> Tuple[str, Optional[int]]:
        """
        解析 Cache-Control 头，返回 cache_directive 和 max_age
        :param header: Cache-Control 头部的字符串
        :return: cache_directive 和 max_age
        """
        cache_directive = ""
        max_age = None

        if not header:
            return cache_directive, max_age

        directives = [directive.strip() for directive in header.split(",")]
        for directive in directives:
            if directive.startswith("max-age"):
                try:
                    max_age = int(directive.split("=")[1])
                except Exception as e:
                    logger.debug(
                        f"Invalid max-age directive in Cache-Control header: {directive}, {e}"
                    )
            elif directive in {
                "no-cache",
                "private",
                "public",
                "no-store",
                "must-revalidate",
            }:
                cache_directive = directive

        return cache_directive, max_age

    @staticmethod
    def generate_cache_headers(
        etag: Optional[str],
        cache_control: Optional[str] = "public",
        max_age: Optional[int] = 86400,
    ) -> dict:
        """
        生成 HTTP 响应的 ETag 和 Cache-Control 头
        :param etag: 响应的 ETag 值。如果为 None，则不添加 ETag 头部。
        :param cache_control: Cache-Control 指令，例如 "public"、"private" 等。默认为 "public"
        :param max_age: Cache-Control 的 max-age 值（秒）。默认为 86400 秒（1天）
        :return: HTTP 头部的字典
        """
        cache_headers = {}

        if etag:
            cache_headers["ETag"] = etag

        if cache_control and max_age is not None:
            cache_headers["Cache-Control"] = f"{cache_control}, max-age={max_age}"
        elif cache_control:
            cache_headers["Cache-Control"] = cache_control
        elif max_age is not None:
            cache_headers["Cache-Control"] = f"max-age={max_age}"

        return cache_headers

    @staticmethod
    def detect_encoding_from_html_response(
        response: Response,
        performance_mode: bool = False,
        confidence_threshold: float = 0.8,
    ):
        """
        根据HTML响应内容探测编码信息

        :param response: HTTP 响应对象
        :param performance_mode: 是否使用性能模式，默认为 False (兼容模式)
        :param confidence_threshold: chardet 检测置信度阈值，默认为 0.8
        :return: 解析得到的字符编码
        """
        fallback_encoding = None
        try:
            if not performance_mode:
                # 兼容模式：使用chardet分析后，再处理 BOM 和 meta 信息
                # 1. 使用 chardet 库进一步分析内容
                detection = chardet.detect(response.content)
                if detection["confidence"] > confidence_threshold:
                    return detection.get("encoding")
                # 保存 chardet 的结果备用
                fallback_encoding = detection.get("encoding")

                # 2. 检查响应体中的 BOM 标记（例如 UTF-8 BOM）
                if response.content[:3] == b"\xef\xbb\xbf":  # UTF-8 BOM
                    return "utf-8"

                # 3. 如果是 HTML 响应体，检查其中的 <meta charset="..."> 标签
                if re.search(
                    r"charset=[\"']?utf-8[\"']?", response.text, re.IGNORECASE
                ):
                    return "utf-8"

                # 4. 尝试从 response headers 中获取编码信息
                content_type = response.headers.get("Content-Type", "")
                if re.search(r"charset=[\"']?utf-8[\"']?", content_type, re.IGNORECASE):
                    return "utf-8"

            else:
                # 性能模式：优先从 headers 和 BOM 标记获取，最后使用 chardet 分析
                # 1. 尝试从 response headers 中获取编码信息
                content_type = response.headers.get("Content-Type", "")
                if re.search(r"charset=[\"']?utf-8[\"']?", content_type, re.IGNORECASE):
                    return "utf-8"
                # 2. 检查响应体中的 BOM 标记（例如 UTF-8 BOM）
                if response.content[:3] == b"\xef\xbb\xbf":
                    return "utf-8"

                # 3. 如果是 HTML 响应体，检查其中的 <meta charset="..."> 标签
                if re.search(
                    r"charset=[\"']?utf-8[\"']?", response.text, re.IGNORECASE
                ):
                    return "utf-8"
                # 4. 使用 chardet 库进一步分析内容
                detection = chardet.detect(response.content)
                if detection.get("confidence", 0) > confidence_threshold:
                    return detection.get("encoding")
                # 保存 chardet 的结果备用
                fallback_encoding = detection.get("encoding")

            # 5. 如果上述方法都无法确定，信任 chardet 的结果（即使置信度较低），否则返回默认字符集
            return fallback_encoding or "utf-8"
        except Exception as e:
            logger.debug(f"Error when detect_encoding_from_response: {str(e)}")
            return fallback_encoding or "utf-8"

    @staticmethod
    def get_decoded_html_content(
        response: Response,
        performance_mode: bool = False,
        confidence_threshold: float = 0.8,
    ) -> str:
        """
        获取HTML响应的解码文本内容

        :param response: HTTP 响应对象
        :param performance_mode: 是否使用性能模式，默认为 False (兼容模式)
        :param confidence_threshold: chardet 检测置信度阈值，默认为 0.8
        :return: 解码后的响应文本内容
        """
        try:
            if not response:
                return ""
            if response.content:
                # 1. 获取编码信息
                encoding = (
                    RequestUtils.detect_encoding_from_html_response(
                        response, performance_mode, confidence_threshold
                    )
                    or response.apparent_encoding
                )
                # 2. 根据解析得到的编码进行解码
                try:
                    # 尝试用推测的编码解码
                    return response.content.decode(encoding)
                except Exception as e:
                    logger.debug(f"Decoding failed, error message: {str(e)}")
                    # 如果解码失败，尝试 fallback 使用 apparent_encoding
                    response.encoding = response.apparent_encoding
                    return response.text
            else:
                return response.text
        except Exception as e:
            logger.debug(f"Error when getting decoded content: {str(e)}")
            return response.text


class AsyncRequestUtils:
    """
    异步HTTP请求工具类，提供异步HTTP请求的基本功能
    """

    def __init__(
        self,
        headers: dict = None,
        ua: str = None,
        cookies: Union[str, dict] = None,
        proxies: dict = None,
        client: httpx.AsyncClient = None,
        timeout: int = None,
        referer: str = None,
        content_type: str = None,
        accept_type: str = None,
        verify: Union[bool, str] = False,
        follow_redirects: bool = True,
        http2: bool = True,
        max_keepalive_connections: int = _DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
        max_connections: int = _DEFAULT_MAX_CONNECTIONS,
        keepalive_expiry: int = _DEFAULT_KEEPALIVE_EXPIRY,
    ):
        """
        :param headers: 请求头部信息
        :param ua: User-Agent字符串
        :param cookies: Cookie字符串或字典
        :param proxies: 代理设置
        :param client: httpx.AsyncClient实例，如果为None则创建新的客户端
        :param timeout: 请求超时时间，默认为20秒
        :param referer: Referer头部信息
        :param content_type: 请求的Content-Type，默认为 "application/x-www-form-urlencoded; charset=UTF-8"
        :param accept_type: Accept头部信息，默认为 "application/json"
        :param verify: 是否校验证书
        :param follow_redirects: 客户端默认是否跟随重定向
        :param http2: 是否启用 HTTP/2（默认 True）。基于 TLS ALPN 协商：服务端
            支持 h2 时复用流多路复用，不支持（含明文 HTTP、老 nginx/Apache）
            自动透明回落 HTTP/1.1。如遇个别站点 h2 实现异常，可显式传
            http2=False 单独关闭。
        :param max_keepalive_connections: 共享 AsyncHTTPTransport 的最大 keep-alive 连接数
        :param max_connections: 共享 AsyncHTTPTransport 的最大连接数
        :param keepalive_expiry: 共享 AsyncHTTPTransport 的 keep-alive 连接过期时间（秒）
        """
        self._proxies = self._convert_proxies_for_httpx(proxies)
        self._client = client
        self._timeout = timeout or 20
        self._verify = verify
        self._follow_redirects = follow_redirects
        self._http2 = http2
        self._max_keepalive_connections = max_keepalive_connections
        self._max_connections = max_connections
        self._keepalive_expiry = keepalive_expiry
        if not content_type:
            content_type = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            # 过滤掉None值的headers
            self._headers = {k: v for k, v in headers.items() if v is not None}
        else:
            if ua and ua == settings.USER_AGENT:
                caller_name = get_caller()
                if caller_name:
                    ua = f"{settings.USER_AGENT} Plugin/{caller_name}"
            self._headers = {}
            if ua:
                self._headers["User-Agent"] = ua
            if content_type:
                self._headers["Content-Type"] = content_type
            if accept_type:
                self._headers["Accept"] = accept_type
            if referer:
                self._headers["referer"] = referer
        if cookies:
            if isinstance(cookies, str):
                self._cookies = cookie_parse(cookies)
            else:
                self._cookies = cookies
        else:
            self._cookies = None

    @staticmethod
    def _convert_proxies_for_httpx(proxies: dict) -> Optional[str]:
        """
        将requests格式的代理配置转换为httpx兼容的格式

        :param proxies: requests格式的代理配置 {"http": "http://proxy:port", "https": "http://proxy:port"}
        :return: httpx兼容的代理字符串或None
        """
        if not proxies:
            return None

        # 如果已经是字符串格式，直接返回
        if isinstance(proxies, str):
            return proxies

        # 如果是字典格式，提取http或https代理
        if isinstance(proxies, dict):
            # 优先使用https代理，如果没有则使用http代理
            proxy_url = proxies.get("https") or proxies.get("http")
            if proxy_url:
                return proxy_url

        return None

    @asynccontextmanager
    async def response_manager(self, method: str, url: str, **kwargs):
        """
        异步响应管理器上下文管理器，确保响应对象被正确关闭
        :param method: HTTP方法
        :param url: 请求的URL
        :param kwargs: 其他请求参数
        """
        response = None
        try:
            response = await self.request(method=method, url=url, **kwargs)
            yield response
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except Exception as e:
                    logger.debug(f"关闭异步响应失败: {e}")

    async def request(
        self, method: str, url: str, raise_exception: bool = False, **kwargs
    ) -> Optional[httpx.Response]:
        """
        发起异步HTTP请求
        :param method: HTTP方法，如 get, post, put 等
        :param url: 请求的URL
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象
        :raises: httpx.RequestError 仅raise_exception为True时会抛出
        """
        # 运行时 self._cookies 只能是 dict | None（cookie_parse 默认 array=False 返回 dict）
        cookies_dict: Optional[dict] = self._cookies if isinstance(self._cookies, dict) else None

        if self._client is not None:
            # 用户自管 client 时，把实例级 cookies 注入到本次 per-request kwargs，
            # 既能复用用户 client，又不让 instance cookies 被静默丢弃。
            # 调用方若显式传 kwargs["cookies"]，则以其为准（setdefault 不覆盖）。
            if cookies_dict is not None:
                kwargs.setdefault("cookies", cookies_dict)
            return await self._make_request(
                self._client, method, url, raise_exception, **kwargs
            )

        # 共享底层 transport（连接池+TLS 复用），每次请求创建轻量 AsyncClient。
        # AsyncClient 持有的 cookie jar 仅存活于本次请求 lifecycle，
        # 既复用握手又彻底避免 jar 跨调用累积。
        transport = _get_shared_async_transport(
            proxy=self._proxies,
            verify=self._verify,
            http2=self._http2,
            max_keepalive_connections=self._max_keepalive_connections,
            max_connections=self._max_connections,
            keepalive_expiry=self._keepalive_expiry,
        )
        if transport is not None:
            # 用 _NonClosingTransportProxy 包装共享 transport，吞掉 AsyncClient.__aexit__
            # 传播下来的 transport.__aexit__，避免每次 async with 退出都把共享连接池清空。
            async with httpx.AsyncClient(
                transport=_NonClosingTransportProxy(transport),
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=self._follow_redirects,
                cookies=cookies_dict,
            ) as client:
                return await self._make_request(
                    client, method, url, raise_exception, **kwargs
                )

        # 兜底：没有运行中的事件循环时，临时客户端走完即关
        async with httpx.AsyncClient(
            http2=self._http2,
            proxy=self._proxies,
            timeout=self._timeout,
            verify=self._verify,
            follow_redirects=self._follow_redirects,
            cookies=cookies_dict,
        ) as client:
            return await self._make_request(
                client, method, url, raise_exception, **kwargs
            )

    async def _make_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """
        执行实际的异步请求
        """
        kwargs.setdefault("headers", self._headers)
        # 共享池下 client 自带默认 timeout，这里用每请求 timeout 覆盖以尊重实例配置
        kwargs.setdefault("timeout", self._timeout)
        # Cookie 在 request() 入口已按 path 处理：
        # - path A（用户自管 client）：kwargs["cookies"] 已注入
        # - path B/C（新建 AsyncClient）：构造时已绑定 cookies
        # 这里不重复 setdefault，避免覆盖各 path 的设定

        method_upper = method.upper()
        # 仅对幂等方法做 stale-pool 竞态重试：复用了刚被对端 FIN 的 keep-alive 连接时，
        # 实际请求通常未到服务端，httpx 自身不重试，这里兜底一次。
        is_idempotent = method_upper in ("GET", "HEAD", "OPTIONS")
        stale_conn_errs = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)

        try:
            return await client.request(method, url, **kwargs)
        except stale_conn_errs as e:
            if is_idempotent:
                logger.debug(f"keep-alive 连接已失效，幂等方法重试一次: {e!r}")
                try:
                    return await client.request(method, url, **kwargs)
                except httpx.RequestError as e2:
                    error_msg = (
                        str(e2) or f"未知网络错误 (URL: {url}, Method: {method_upper})"
                    )
                    logger.debug(f"重试后异步请求仍失败: {error_msg}")
                    if raise_exception:
                        raise
                    return None
            # 非幂等方法（POST/PUT/PATCH/DELETE 等）不重试以避免重复副作用，
            # 但仍记录调试日志，避免静默失败掩盖问题
            error_msg = str(e) or f"未知网络错误 (URL: {url}, Method: {method_upper})"
            logger.debug(f"异步请求失败(非幂等不重试): {error_msg}")
            if raise_exception:
                raise
            return None
        except httpx.RequestError as e:
            # 获取更详细的错误信息
            error_msg = str(e) or f"未知网络错误 (URL: {url}, Method: {method_upper})"
            logger.debug(f"异步请求失败: {error_msg}")
            if raise_exception:
                raise
            return None

    async def get(self, url: str, params: dict = None, **kwargs) -> Optional[str]:
        """
        发送异步GET请求
        :param url: 请求的URL
        :param params: 请求的参数
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: 响应的内容，若发生RequestError则返回None
        """
        response = await self.request(method="get", url=url, params=params, **kwargs)
        try:
            if response:
                try:
                    content = response.text
                    return content
                except Exception as e:
                    logger.debug(f"处理异步响应内容失败: {e}")
                    return None
            return None
        finally:
            if response is not None:
                await response.aclose()

    async def post(
        self, url: str, data: Any = None, json: dict = None, **kwargs
    ) -> Optional[httpx.Response]:
        """
        发送异步POST请求
        :param url: 请求的URL
        :param data: 请求的数据
        :param json: 请求的JSON数据
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestError则返回None
        """
        return await self.request(
            method="post", url=url, data=data, json=json, **kwargs
        )

    async def put(
        self, url: str, data: Any = None, **kwargs
    ) -> Optional[httpx.Response]:
        """
        发送异步PUT请求
        :param url: 请求的URL
        :param data: 请求的数据
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestError则返回None
        """
        return await self.request(method="put", url=url, data=data, **kwargs)

    async def get_res(
        self,
        url: str,
        params: dict = None,
        data: Any = None,
        json: dict = None,
        allow_redirects: bool = True,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """
        发送异步GET请求并返回响应对象
        :param url: 请求的URL
        :param params: 请求的参数
        :param data: 请求的数据
        :param json: 请求的JSON数据
        :param allow_redirects: 是否允许重定向
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestError则返回None
        :raises: httpx.RequestError 仅raise_exception为True时会抛出
        """
        return await self.request(
            method="get",
            url=url,
            params=params,
            data=data,
            json=json,
            follow_redirects=allow_redirects,
            raise_exception=raise_exception,
            **kwargs,
        )

    @asynccontextmanager
    async def get_stream(
        self,
        url: str,
        params: dict = None,
        raise_exception: bool = False,
        **kwargs,
    ):
        """
        获取异步流式响应的上下文管理器，适用于大文件下载。
        使用 httpx.AsyncClient.stream() 标准流式 API，避免把响应体一次性读入内存。

        :param url: 请求的URL
        :param params: 请求的参数
        :param raise_exception: 是否在发生异常时抛出，否则吞掉并 yield None
        :param kwargs: 其他请求参数（headers, cookies 等）
        :return: 上下文管理器，进入后 yield httpx.Response（出错时 yield None）
        """
        cookies_dict: Optional[dict] = self._cookies if isinstance(self._cookies, dict) else None
        kwargs.setdefault("headers", self._headers)

        # 与 _make_request 保持一致：复用 keep-alive 时偶遇对端 FIN 的连接，
        # 流式 GET 是幂等的，单次重试即可
        stale_conn_errs = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)

        async with AsyncExitStack() as stack:
            # 选 client：复用与 request() 相同的三条 path 逻辑
            if self._client is not None:
                client = self._client
                if cookies_dict is not None:
                    kwargs.setdefault("cookies", cookies_dict)
            else:
                transport = _get_shared_async_transport(
                    proxy=self._proxies,
                    verify=self._verify,
                    http2=self._http2,
                    max_keepalive_connections=self._max_keepalive_connections,
                    max_connections=self._max_connections,
                    keepalive_expiry=self._keepalive_expiry,
                )
                if transport is not None:
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            transport=_NonClosingTransportProxy(transport),
                            timeout=httpx.Timeout(self._timeout),
                            follow_redirects=self._follow_redirects,
                            cookies=cookies_dict,
                        )
                    )
                else:
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            http2=self._http2,
                            proxy=self._proxies,
                            timeout=self._timeout,
                            verify=self._verify,
                            follow_redirects=self._follow_redirects,
                            cookies=cookies_dict,
                        )
                    )

            try:
                response = await stack.enter_async_context(
                    client.stream("GET", url, params=params, **kwargs)
                )
            except stale_conn_errs as e:
                logger.debug(f"流式 keep-alive 连接已失效，重试一次: {e!r}")
                try:
                    response = await stack.enter_async_context(
                        client.stream("GET", url, params=params, **kwargs)
                    )
                except httpx.RequestError as e2:
                    logger.debug(f"重试后异步流式请求仍失败: {e2!r}")
                    if raise_exception:
                        raise
                    yield None
                    return
            except httpx.RequestError as e:
                logger.debug(f"异步流式请求失败: {e!r}")
                if raise_exception:
                    raise
                yield None
                return

            # AsyncExitStack 反向 unwind：先关 stream，再关 owned client；
            # yield 体内的异常由标准 async with 协议透传给各 __aexit__
            yield response

    async def post_res(
        self,
        url: str,
        data: Any = None,
        params: dict = None,
        allow_redirects: bool = True,
        files: Any = None,
        json: dict = None,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """
        发送异步POST请求并返回响应对象
        :param url: 请求的URL
        :param data: 请求的数据
        :param params: 请求的参数
        :param allow_redirects: 是否允许重定向
        :param files: 请求的文件
        :param json: 请求的JSON数据
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestError则返回None
        :raises: httpx.RequestError 仅raise_exception为True时会抛出
        """
        return await self.request(
            method="post",
            url=url,
            data=data,
            params=params,
            follow_redirects=allow_redirects,
            files=files,
            json=json,
            raise_exception=raise_exception,
            **kwargs,
        )

    async def put_res(
        self,
        url: str,
        data: Any = None,
        params: dict = None,
        allow_redirects: bool = True,
        files: Any = None,
        json: dict = None,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """
        发送异步PUT请求并返回响应对象
        :param url: 请求的URL
        :param data: 请求的数据
        :param params: 请求的参数
        :param allow_redirects: 是否允许重定向
        :param files: 请求的文件
        :param json: 请求的JSON数据
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestError则返回None
        :raises: httpx.RequestError 仅raise_exception为True时会抛出
        """
        return await self.request(
            method="put",
            url=url,
            data=data,
            params=params,
            follow_redirects=allow_redirects,
            files=files,
            json=json,
            raise_exception=raise_exception,
            **kwargs,
        )

    async def delete_res(
        self,
        url: str,
        data: Any = None,
        params: dict = None,
        allow_redirects: bool = True,
        raise_exception: bool = False,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """
        发送异步DELETE请求并返回响应对象
        :param url: 请求的URL
        :param data: 请求的数据
        :param params: 请求的参数
        :param allow_redirects: 是否允许重定向
        :param raise_exception: 是否在发生异常时抛出异常，否则默认拦截异常返回None
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        :return: HTTP响应对象，若发生RequestError则返回None
        :raises: httpx.RequestError 仅raise_exception为True时会抛出
        """
        return await self.request(
            method="delete",
            url=url,
            data=data,
            params=params,
            follow_redirects=allow_redirects,
            raise_exception=raise_exception,
            **kwargs,
        )

    async def get_json(self, url: str, params: dict = None, **kwargs) -> Optional[dict]:
        """
        发送异步GET请求并返回JSON数据，自动关闭连接
        :param url: 请求的URL
        :param params: 请求的参数
        :param kwargs: 其他请求参数
        :return: JSON数据，若发生异常则返回None
        """
        response = await self.request(method="get", url=url, params=params, **kwargs)
        try:
            if response:
                try:
                    data = response.json()
                    return data
                except Exception as e:
                    logger.debug(f"解析异步JSON失败: {e}")
                    return None
            return None
        finally:
            if response is not None:
                await response.aclose()

    async def post_json(
        self, url: str, data: Any = None, json: dict = None, **kwargs
    ) -> Optional[dict]:
        """
        发送异步POST请求并返回JSON数据，自动关闭连接
        :param url: 请求的URL
        :param data: 请求的数据
        :param json: 请求的JSON数据
        :param kwargs: 其他请求参数
        :return: JSON数据，若发生异常则返回None
        """
        if json is None:
            json = {}
        response = await self.request(
            method="post", url=url, data=data, json=json, **kwargs
        )
        try:
            if response:
                try:
                    data = response.json()
                    return data
                except Exception as e:
                    logger.debug(f"解析异步JSON失败: {e}")
                    return None
            return None
        finally:
            if response is not None:
                await response.aclose()
