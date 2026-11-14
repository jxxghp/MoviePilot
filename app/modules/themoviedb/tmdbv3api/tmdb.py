"""
TMDB 同步与异步传输、响应快照缓存、分页状态和有限重试策略。
"""

# -*- coding: utf-8 -*-

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime
from typing import Protocol, TypeAlias, TypedDict, TypeVar, Union, cast

import requests
import requests.exceptions

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.runtime.cache import async_fresh, cached, fresh
from app.runtime.settings import get_runtime_setting

from .exceptions import TMDbConnectionError, TMDbException

TMDbJson: TypeAlias = Union[dict[str, "TMDbJson"], list["TMDbJson"], str, int, float, bool, None]
_RequestFunction = TypeVar("_RequestFunction", bound=Callable[..., object])


class _HttpResponse(Protocol):
    """
    requests 与 httpx 响应在 TMDb 解析层使用的最小接口。
    """

    @property
    def headers(self) -> Mapping[str, str]:
        """
        读取客户端提供的响应头映射。

        :return: 响应头名称和值的映射；大小写行为沿用底层客户端
        """
        ...

    def json(self) -> TMDbJson:
        """
        解析 HTTP 响应中的 JSON 数据。

        :return: 响应 JSON 原值；此接口不保证顶层一定是对象或数组
        :raises ValueError: 响应不是有效 JSON
        :raises UnicodeDecodeError: 响应编码无法解码
        """
        ...


class _ResponseSnapshot(TypedDict):
    """
    可序列化响应快照，另带运行期快照标记。

    :ivar headers: 从原始响应复制的响应头，用于读取限流信息
    :ivar json: 已解码的响应内容，仅接受对象或数组作为顶层结构
    """

    headers: dict[str, str]
    json: TMDbJson


class _ClearableRequest(Protocol):
    """
    缓存装饰器为同步请求附加的清理入口。
    """

    def cache_clear(self) -> None:
        """
        清空当前请求函数绑定的缓存区。
        """
        ...


logger = logging.getLogger(__name__)

# 单次重试前的退避等待时间（秒）。NAS+FUSE网盘等环境下TMDB连接的失败大多是数秒内
# 可自愈的瞬时抖动，零间隔重试（或异步完全不重试）基本无法穿越这类抖动窗口；
# 识别链路是同步阻塞调用，1-3秒的等待可接受，超出3秒则会让识别耗时明显变长，
# 故取区间内的经验值。
RETRY_BACKOFF_SECONDS = 2


def _is_business_failure_snapshot(snapshot: object) -> bool:
    """
    判断响应快照是否为TMDB业务失败（success=false，如404/限流/服务端错误的合法JSON）。

    这类响应若入缓存会把瞬时失败固化整个TTL周期（如12小时），期间同key请求
    直接命中失败快照；跳过缓存让下次请求重新确认。真正的负缓存（条目确认
    不存在）由上层 TmdbCache 负责，request 层不做失败记忆。
    """
    if not isinstance(snapshot, dict):
        return False
    json_data = snapshot.get("json")
    return isinstance(json_data, dict) and json_data.get("success") is False


def _is_empty_result_snapshot(snapshot: object) -> bool:
    """
    判断响应快照是否为空结果（列表/搜索类接口的 results 为空列表）。

    这类快照结构合法但无业务内容，常由代理瞬时故障产生；不能靠 skip_none/skip_empty
    识别（快照本身是非空字典），需单独谓词判定后按 get_runtime_setting('EMPTY_RESULT_CACHE_TTL')
    短 TTL 缓存。详情类接口无 results 字段，不属于空结果。
    """
    if not isinstance(snapshot, dict):
        return False
    json_data = snapshot.get("json")
    if not isinstance(json_data, dict):
        return False
    results = json_data.get("results")
    return isinstance(results, list) and not results


def _cache_request(function: _RequestFunction) -> _RequestFunction:
    """
    保留请求的调用类型；cached 仅附加缓存管理入口而不改变返回值。
    """
    return cached(
        maxsize=get_runtime_setting('CONF').tmdb,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
        skip_if=_is_business_failure_snapshot,
        empty_ttl=get_runtime_setting('EMPTY_RESULT_CACHE_TTL'),
        empty_if=_is_empty_result_snapshot,
    )(function)


class TMDb(object):
    """
    封装 TMDB 请求、响应缓存及有界的连接和限流重试。
    """

    _RESPONSE_SNAPSHOT_MARKER = "__mp_tmdb_response_snapshot__"

    def __init__(self, session: requests.Session | None = None, language: str | None = None) -> None:
        """
        读取运行配置并创建同步与异步请求客户端。

        同步与异步客户端共用 15 秒请求超时，不随调用方会话传入。

        未提供会话时创建自有会话。close 沿用历史行为，会关闭当前同步会话，
        包括调用方传入的会话；异步请求客户端不由该方法关闭。

        :param session: 可选同步会话；提供时连接失败不重建或重试该会话
        :param language: 请求语言；空值回退到运行配置，再回退为 en-US
        """
        self._api_key: str | None = get_runtime_setting('TMDB_API_KEY')
        self._language: str | None = language or get_runtime_setting('TMDB_LOCALE') or "en-US"
        self._session_id: str | None = None
        self._session = session
        self._wait_on_rate_limit = True
        self._proxies: dict[str, str] | None = get_runtime_setting('PROXY')
        self._domain: str | None = get_runtime_setting('TMDB_API_DOMAIN')
        self._page: TMDbJson = None
        self._total_results: TMDbJson = None
        self._total_pages: TMDbJson = None

        self._owns_session = session is None
        # 超时必须在创建请求客户端之前赋值：_init_session 会读取它，
        # 放在客户端之后会让 timeout 静默回退到 RequestUtils 的默认值。
        self._timeout = 15
        self._init_session(session=session)

        # TMDB 在部分代理和运营商链路下的 HTTP/2 长连接偶发卡死，识别路径优先保证稳定性。
        self._async_req = AsyncRequestUtils(
            ua=get_runtime_setting('NORMAL_USER_AGENT'),
            proxies=self.proxies,
            http2=False,
            timeout=self._timeout,
        )

        self._remaining = 40
        self._reset: int | None = None

    def _init_session(self, session: requests.Session | None = None) -> None:
        """
        初始化同步请求会话。

        同步客户端沿用实例声明的请求超时。

        :param session: 调用方提供的会话；为空时创建自有会话
        """
        self._session = session or requests.Session()
        self._req = RequestUtils(
            ua=get_runtime_setting('NORMAL_USER_AGENT'),
            session=self._session,
            proxies=self.proxies,
            timeout=self._timeout,
        )

    def _reset_owned_session(self) -> None:
        """
        重建当前实例自持有的同步请求会话。
        """
        if not self._owns_session:
            return
        if self._session:
            try:
                self._session.close()
            except Exception as err:
                logger.debug(f"关闭TMDB同步会话失败：{err!r}")
        self._init_session()

    @property
    def page(self) -> TMDbJson:
        """
        返回响应携带的页码，不转换上游字段。

        :return: 最近一次带分页信息响应中的当前页码；尚未取得时为 None
        """
        return self._page

    @property
    def total_results(self) -> TMDbJson:
        """
        返回响应携带的结果总数。

        :return: 最近一次带该字段响应中的结果总数；尚未取得时为 None
        """
        return self._total_results

    @property
    def total_pages(self) -> TMDbJson:
        """
        返回响应携带的总页数。

        :return: 最近一次带该字段响应中的总页数；尚未取得时为 None
        """
        return self._total_pages

    @property
    def api_key(self) -> str | None:
        """
        读取当前 API Key。

        :return: 当前 API 密钥，可能为 None
        """
        return self._api_key

    @api_key.setter
    def api_key(self, api_key: object) -> None:
        """
        沿用旧入口的字符串转换规则保存 API Key。
        """
        self._api_key = str(api_key)

    @property
    def domain(self) -> str | None:
        """
        读取当前 API 域名。

        :return: 当前请求域名，可能为 None
        """
        return self._domain

    @domain.setter
    def domain(self, domain: object) -> None:
        """
        沿用旧入口的字符串转换规则保存 API 域名。
        """
        self._domain = str(domain)

    @property
    def proxies(self) -> dict[str, str] | None:
        """
        读取当前 HTTP 代理映射。

        :return: 当前代理配置映射或 None
        """
        return self._proxies

    @proxies.setter
    def proxies(self, proxies: dict[str, str] | None) -> None:
        """
        保留调用方传入的代理映射。
        """
        self._proxies = proxies

    @property
    def language(self) -> str | None:
        """
        读取请求使用的语言。

        :return: 当前请求语言代码或 None
        """
        return self._language

    @language.setter
    def language(self, language: str | None) -> None:
        """
        保存调用方指定的语言。
        """
        self._language = language

    @property
    def has_session(self) -> bool:
        """
        判断是否已保存认证会话标识。

        :return: 已保存非空会话标识时返回 True
        """
        return True if self._session_id else False

    @property
    def session_id(self) -> str:
        """
        读取认证会话；未认证时保持原异常。

        :return: 当前非空认证会话标识；未认证时抛出 TMDbException
        """
        if not self._session_id:
            raise TMDbException("Must Authenticate to create a session run Authentication(username, password)")
        return self._session_id

    @session_id.setter
    def session_id(self, session_id: str | None) -> None:
        """
        保存或清空认证会话标识。
        """
        self._session_id = session_id

    @property
    def wait_on_rate_limit(self) -> bool:
        """
        读取是否等待服务端限流重置。

        :return: 是否允许等待服务端限流重置
        """
        return self._wait_on_rate_limit

    @wait_on_rate_limit.setter
    def wait_on_rate_limit(self, wait_on_rate_limit: object) -> None:
        """
        按原有真值规则设置限流等待开关。
        """
        self._wait_on_rate_limit = bool(wait_on_rate_limit)

    @_cache_request
    def request(self, method: str, url: str, data: dict[str, TMDbJson] | None, json: dict[str, TMDbJson] | None, **kwargs: object) -> _ResponseSnapshot:
        """
        执行同步请求并缓存可序列化响应快照。

        仅自有会话的 GET 请求在未取得响应时等待 2 秒、重建会话并重试一次。
        业务失败快照不缓存，空结果使用较短缓存期限。

        :param method: GET 使用查询请求，其他值均走 POST 传输
        :param url: 已包含认证与查询参数的完整请求地址
        :param data: GET 查询参数或 POST 表单载荷
        :param json: 可选 JSON 请求载荷
        :param kwargs: 参与缓存键的附加参数，不直接传递给 HTTP 客户端
        :return: 含响应头和 JSON 数据的快照
        :raises TMDbConnectionError: 请求及适用重试均未取得响应
        :raises TMDbException: 响应 JSON 或顶层结构无效
        """
        req = self._request_once(method, url, data, json)
        if req is None and method == "GET" and self._owns_session:
            logger.debug(f"TMDB同步请求失败，等待{RETRY_BACKOFF_SECONDS}秒后重建会话重试一次")
            # 同步阻塞识别链线程；1-3秒的退避等待可接受，能显著提升对瞬时抖动的容错，
            # 详见模块级常量 RETRY_BACKOFF_SECONDS 的说明。
            time.sleep(RETRY_BACKOFF_SECONDS)
            self._reset_owned_session()
            req = self._request_once(method, url, data, json)
        if req is None:
            # 抛出更具体的连接异常子类，供上层（如TMDB详情查询）区分"网络故障"
            # 与"TMDB业务层明确返回的错误"（如404条目不存在），两者不能混为一谈。
            raise TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
        return self._snapshot_response(req)

    def _request_once(self, method: str, url: str, data: dict[str, TMDbJson] | None, json: dict[str, TMDbJson] | None) -> _HttpResponse | None:
        """
        执行一次同步传输，GET 以外的方法沿用 POST 路径。

        :return: 原始 HTTP 响应；未取得响应时返回 None
        """
        if method == "GET":
            return cast(_HttpResponse | None, self._req.get_res(url, params=data, json=json))
        return cast(_HttpResponse | None, self._req.post_res(url, data=data, json=json))

    @_cache_request
    async def async_request(self, method: str, url: str, data: dict[str, TMDbJson] | None, json: dict[str, TMDbJson] | None, **kwargs: object) -> _ResponseSnapshot:
        """
        执行异步请求并缓存可序列化响应快照。

        未取得响应时等待 2 秒，在原客户端上重试一次；该重试也适用于非 GET 请求。
        业务失败快照不缓存，空结果使用较短缓存期限。

        :param method: GET 使用查询请求，其他值均走 POST 传输
        :param url: 已包含认证与查询参数的完整请求地址
        :param data: GET 查询参数或 POST 表单载荷
        :param json: 可选 JSON 请求载荷
        :param kwargs: 参与缓存键的附加参数，不直接传递给 HTTP 客户端
        :return: 含响应头和 JSON 数据的快照
        :raises TMDbConnectionError: 请求与重试均未取得响应
        :raises TMDbException: 响应 JSON 或顶层结构无效
        """
        req = await self._async_request_once(method, url, data, json)
        if req is None:
            logger.debug(f"TMDB异步请求失败，等待{RETRY_BACKOFF_SECONDS}秒后重试一次")
            # 异步会话（AsyncRequestUtils）不像同步会话那样支持按需重建，
            # 这里退化为原会话上的纯重试，同样以退避等待应对瞬时抖动。
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
            req = await self._async_request_once(method, url, data, json)
        if req is None:
            raise TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
        return self._snapshot_response(req)

    async def _async_request_once(self, method: str, url: str, data: dict[str, TMDbJson] | None, json: dict[str, TMDbJson] | None) -> _HttpResponse | None:
        """
        执行一次异步传输，GET 以外的方法沿用 POST 路径。

        :return: 原始 HTTP 响应；未取得响应时返回 None
        """
        if method == "GET":
            return cast(_HttpResponse | None, await self._async_req.get_res(url, params=data, json=json))
        return cast(_HttpResponse | None, await self._async_req.post_res(url, data=data, json=json))

    @classmethod
    def _snapshot_response(cls, response: _HttpResponse) -> _ResponseSnapshot:
        """
        生成可缓存的响应快照，并在入缓存前拦截明显异常的TMDB响应结构。
        """
        json_data = cls._decode_response_json(response)
        cls._validate_json_response(json_data)
        # Redis 不能稳定序列化 requests/httpx 响应对象，缓存里只保留当前流程会用到的数据。
        return cast(_ResponseSnapshot, {
            cls._RESPONSE_SNAPSHOT_MARKER: True,
            "headers": dict(response.headers.items()),
            "json": json_data,
        })

    @classmethod
    def _get_response_headers(cls, response: _HttpResponse | _ResponseSnapshot) -> Mapping[str, str]:
        """
        兼容缓存快照与原始 HTTP 响应的响应头。
        """
        if isinstance(response, dict) and response.get(cls._RESPONSE_SNAPSHOT_MARKER):
            return response.get("headers") or {}
        return cast(_HttpResponse, response).headers

    @classmethod
    def _get_response_json(cls, response: _HttpResponse | _ResponseSnapshot) -> TMDbJson:
        """
        读取原始 JSON，并隔离缓存命中的可变数据。
        """
        if isinstance(response, dict) and response.get(cls._RESPONSE_SNAPSHOT_MARKER):
            # 调用方会补充 media_type 等字段，缓存快照必须隔离这些原地修改。
            return deepcopy(response.get("json"))
        return cls._decode_response_json(cast(_HttpResponse, response))

    @classmethod
    def _decode_response_json(cls, response: _HttpResponse) -> TMDbJson:
        """
        解析TMDB响应JSON，并把空响应、代理错误页或错误编码的响应统一转换为TMDB异常。
        """
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as err:
            raise TMDbException(cls._build_invalid_json_message(response, err)) from err

    @staticmethod
    def _get_header_value(headers: Mapping[str, str], name: str) -> str | None:
        """
        从不同响应头对象中按大小写兼容读取指定响应头。
        """
        try:
            value = headers.get(name)
        except AttributeError:
            return None
        if value is not None:
            return value

        lower_name = name.lower()
        try:
            for header_name, header_value in headers.items():
                if str(header_name).lower() == lower_name:
                    return header_value
        except AttributeError:
            return None
        return None

    @classmethod
    def _build_invalid_json_message(cls, response: object, parse_error: Exception | None = None) -> str:
        """
        生成非JSON响应的诊断信息，避免日志只保留JSONDecodeError文本。
        """
        status_code = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {}) or {}
        content_type = cls._get_header_value(headers, "Content-Type")
        is_encoding_error = isinstance(parse_error, UnicodeDecodeError)

        # 编码错误或压缩字节时响应体是乱码，打印内容只会污染日志。
        content_encoding = cls._get_header_value(headers, "Content-Encoding")
        response_text = ""
        if not is_encoding_error and not content_encoding:
            try:
                response_text = getattr(response, "text", "") or ""
            # 异常响应对象的文本属性也可能抛错，诊断信息不能因此丢失。
            except Exception as err:  # pragma: no cover
                response_text = f"<读取响应内容失败：{err!r}>"
            if not isinstance(response_text, str):
                response_text = repr(response_text)
            response_text = response_text.strip()
            if len(response_text) > 200:
                response_text = f"{response_text[:200]}..."

        message_parts = ["TheMovieDb 返回数据不是有效JSON"]
        if status_code is not None:
            message_parts.append(f"HTTP状态码：{status_code}")
        if content_type:
            message_parts.append(f"Content-Type：{content_type}")
        if content_encoding:
            message_parts.append(f"Content-Encoding：{content_encoding}")
        if is_encoding_error or content_encoding:
            message_parts.append("响应内容编码异常，已省略原始内容")
        elif response_text:
            message_parts.append(f"响应内容：{response_text!r}")
        else:
            message_parts.append("响应内容为空")
        return "，".join(message_parts)

    @staticmethod
    def _validate_json_response(json_data: object) -> None:
        """
        校验TMDB响应JSON顶层结构，避免代理错误页等标量值继续按字典解析。
        """
        if isinstance(json_data, (dict, list)):
            return

        payload_preview = repr(json_data)
        if len(payload_preview) > 200:
            payload_preview = f"{payload_preview[:200]}..."
        raise TMDbException(
            "TheMovieDb 返回数据格式异常：期望JSON对象或数组，"
            f"实际为{type(json_data).__name__}，内容：{payload_preview}"
        )

    @staticmethod
    def _get_json_key(json_data: TMDbJson, key: str) -> TMDbJson:
        """
        从TMDB对象响应中读取指定字段，避免异常顶层结构触发AttributeError。
        """
        if not isinstance(json_data, dict):
            raise TMDbException(
                "TheMovieDb 返回数据格式异常："
                f"期望JSON对象包含字段 {key!r}，实际为{type(json_data).__name__}"
            )
        return json_data.get(key)

    def cache_clear(self) -> None:
        """
        通过缓存装饰器附加的入口清理同步请求缓存。
        """
        return cast(_ClearableRequest, self.request).cache_clear()

    def _validate_api_key(self) -> None:
        """
        在发送请求前拒绝缺失 API Key。
        """
        if self.api_key is None or self.api_key == "":
            raise TMDbException("TheMovieDb API Key 未设置！")

    def _build_url(self, action: str, params: str = "") -> str:
        """
        按旧格式拼接 API 路径、认证参数和语言。
        """
        return "https://%s/3%s?api_key=%s&%s&language=%s" % (
            self.domain,
            action,
            self.api_key,
            params,
            self.language,
        )

    def _handle_headers(self, headers: Mapping[str, str] | None) -> None:
        """
        只记录当前响应的限流状态，缺失字段不能沿用上次耗尽的配额。
        """
        normalized_headers = {
            str(key).lower(): value for key, value in dict(headers or {}).items()
        }

        self._remaining = int(normalized_headers.get("x-ratelimit-remaining", 40))
        reset = normalized_headers.get("x-ratelimit-reset")
        self._reset = int(reset) if reset is not None else None

    def _handle_rate_limit(self) -> int:
        """
        计算尚未到期的限流等待；缺少重置时间时直接报告限流。
        """
        if self._remaining < 1:
            if not self.wait_on_rate_limit or self._reset is None:
                raise TMDbException("达到请求频率限制，请稍后再试！")
            return max(0, self._reset - int(time.time()))
        return 0

    def _process_json_response(self, json_data: TMDbJson, is_async: bool = False) -> None:
        """
        从TMDB对象响应中记录分页信息；数组响应没有分页字段，直接跳过。
        """
        if not isinstance(json_data, dict):
            return

        if "page" in json_data:
            self._page = json_data["page"]

        if "total_results" in json_data:
            self._total_results = json_data["total_results"]

        if "total_pages" in json_data:
            self._total_pages = json_data["total_pages"]

    @staticmethod
    def _handle_errors(json_data: TMDbJson) -> None:
        """
        将TMDB标准错误字段转换为统一异常，非对象响应由结构校验提前处理。
        """
        if not isinstance(json_data, dict):
            return

        if "errors" in json_data:
            raise TMDbException(json_data["errors"])

        if "success" in json_data and json_data["success"] is False:
            raise TMDbException(json_data["status_message"])

    def _request_obj(self, action: str, params: str = "", call_cached: bool = True,
                     method: str = "GET", data: dict[str, TMDbJson] | None = None, json: dict[str, TMDbJson] | None = None,
                     key: str | None = None) -> TMDbJson:
        """
        同步解析 TMDB 响应，按需提取目标字段。

        校验 API 密钥并更新分页和限流状态；服务端限流最多等待后重试一次，
        缓存响应会深拷贝，调用方修改不会污染缓存。

        :param action: 相对于 TMDB v3 根地址的接口路径
        :param params: 已编码的查询参数字符串
        :param call_cached: 是否允许读取底层请求缓存；POST 总是绕过缓存
        :param method: 逻辑请求方法；当前传输层对 GET 以外的值统一使用 POST
        :param data: 查询参数或表单载荷
        :param json: 可选 JSON 请求载荷
        :param key: 需要提取的顶层字段；空值返回完整 JSON
        :return: 目标字段或完整 JSON；未取得响应或字段不存在时返回 None
        :raises TMDbException: 密钥缺失、限流、响应结构无效或服务端报告业务错误
        """
        self._validate_api_key()
        url = self._build_url(action, params)

        rate_limit_retried = False
        while True:
            with fresh(not call_cached or method == "POST"):
                req = self.request(method, url, data, json,
                                   _ts=datetime.strftime(datetime.now(), '%Y%m%d'))

            if req is None:
                return None

            self._handle_headers(self._get_response_headers(req))

            rate_limit_result = self._handle_rate_limit()
            if not rate_limit_result:
                break
            if rate_limit_retried:
                raise TMDbException("达到请求频率限制，请稍后再试！")
            logger.warning(f"达到请求频率限制，将在 {rate_limit_result} 秒后重试...")
            time.sleep(rate_limit_result)
            rate_limit_retried = True
            call_cached = False

        json_data = self._get_response_json(req)
        self._validate_json_response(json_data)
        self._process_json_response(json_data, is_async=False)
        self._handle_errors(json_data)

        if key:
            return self._get_json_key(json_data, key)
        return json_data

    async def _async_request_obj(self, action: str, params: str = "", call_cached: bool = True,
                                 method: str = "GET", data: dict[str, TMDbJson] | None = None, json: dict[str, TMDbJson] | None = None,
                                 key: str | None = None) -> TMDbJson:
        """
        异步解析 TMDB 响应，按需提取目标字段。

        校验 API 密钥并更新分页和限流状态；服务端限流最多等待后重试一次，
        缓存响应会深拷贝，调用方修改不会污染缓存。

        :param action: 相对于 TMDB v3 根地址的接口路径
        :param params: 已编码的查询参数字符串
        :param call_cached: 是否允许读取底层请求缓存；POST 总是绕过缓存
        :param method: 逻辑请求方法；当前传输层对 GET 以外的值统一使用 POST
        :param data: 查询参数或表单载荷
        :param json: 可选 JSON 请求载荷
        :param key: 需要提取的顶层字段；空值返回完整 JSON
        :return: 目标字段或完整 JSON；未取得响应或字段不存在时返回 None
        :raises TMDbException: 密钥缺失、限流、响应结构无效或服务端报告业务错误
        """
        self._validate_api_key()
        url = self._build_url(action, params)

        rate_limit_retried = False
        while True:
            async with async_fresh(not call_cached or method == "POST"):
                req = await self.async_request(method, url, data, json,
                                               _ts=datetime.strftime(datetime.now(), '%Y%m%d'))

            if req is None:
                return None

            self._handle_headers(self._get_response_headers(req))

            rate_limit_result = self._handle_rate_limit()
            if not rate_limit_result:
                break
            if rate_limit_retried:
                raise TMDbException("达到请求频率限制，请稍后再试！")
            logger.warning(f"达到请求频率限制，将在 {rate_limit_result} 秒后重试...")
            await asyncio.sleep(rate_limit_result)
            rate_limit_retried = True
            call_cached = False

        json_data = self._get_response_json(req)
        self._validate_json_response(json_data)
        self._process_json_response(json_data, is_async=True)
        self._handle_errors(json_data)

        if key:
            return self._get_json_key(json_data, key)
        return json_data

    def close(self) -> None:
        """
        关闭当前同步会话，包括调用方传入的会话。

        该入口不关闭异步请求客户端；同步会话关闭异常向调用方传播。
        """
        if self._session:
            self._session.close()
