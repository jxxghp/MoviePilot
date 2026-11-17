"""
带有界本地缓存、故障冷却和总超时的中心媒体关系客户端。
"""

import asyncio
import concurrent.futures
import threading
import time
from collections import OrderedDict
from typing import Any, cast

from app.adapters.network.http import AsyncHttpRequestError, AsyncRequestUtils
from app.runtime.loop import main_loop_registry
from app.runtime.settings import get_runtime_setting
from app.runtime.tasks import get_task_registry
from app.schemas.mapping import MediaMapping, MediaMappingQuery

# 查询总预算包含 DNS、重定向、连接、读取和传输层重试。
QUERY_TIMEOUT = 0.8
REPORT_TIMEOUT = 0.5
POSITIVE_TTL = 3600
NEGATIVE_TTL = 600
FAILURE_TTL = 30
CACHE_LIMIT = 4096
INFLIGHT_LIMIT = 8


class MediaMappingClient:
    """
    同异步调用共用进程缓存，中心故障不会阻塞正常转换和详情加载。
    """

    def __init__(self) -> None:
        """
        建立有界缓存、在途集合与跨线程保护锁。
        """
        self._cache: OrderedDict[str, tuple[float, MediaMapping | None]] = OrderedDict()
        self._inflight: set[str] = set()
        self._queries: dict[str, concurrent.futures.Future[MediaMapping | None]] = {}
        self._lock = threading.Lock()
        self._retry_at = 0.0

    @staticmethod
    def _url() -> str | None:
        """
        沿用共享识别开关，始终向配置的 Server 查询持久关系。

        :return: 中心关系接口，未启用时为 None
        """
        host = (get_runtime_setting("MP_SERVER_HOST") or "").strip().rstrip("/")
        return f"{host}/recognize/mapping" if host and get_runtime_setting("MEDIA_RECOGNIZE_SHARE") else None

    @staticmethod
    def _key(url: str, query: MediaMappingQuery) -> str:
        """
        隔离 Server 地址、来源、ID、类型和季号不同的缓存。

        :param url: 当前中心接口地址
        :param query: 关系查询
        :return: 无分隔符碰撞的规范 JSON 键
        """
        return url + ":" + query.model_dump_json()

    def _remember(self, key: str, value: MediaMapping | None, ttl: int) -> None:
        """
        记录命中或未命中并淘汰最久未访问条目，调用方持有锁。

        :param key: 查询键
        :param value: 已验证关系或负缓存
        :param ttl: 有效秒数
        """
        self._cache[key] = (time.monotonic() + ttl, value)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_LIMIT:
            self._cache.popitem(last=False)

    async def _request(self, url: str, payload: dict[str, Any], *, report: bool = False) -> MediaMapping | None:
        """
        在总时间预算内完成一次请求并验证外部响应，失败进入全局短冷却。

        :param url: 中心接口地址
        :param payload: 来源查询或共享关系
        :param report: 是否发送共享写入
        :return: 中心确认的关系或未命中
        """
        try:
            async with asyncio.timeout(REPORT_TIMEOUT if report else QUERY_TIMEOUT):
                client = AsyncRequestUtils(http2=False, timeout=REPORT_TIMEOUT if report else QUERY_TIMEOUT,
                                           proxies=get_runtime_setting("PROXY"), verify=True, follow_redirects=False,
                                           content_type="application/json")
                if report:
                    response = await client.post(url, json=payload)
                else:
                    response = await client.get_res(url, params=payload)
                if response is None or response.status_code != 200:
                    raise ValueError("中心关系服务不可用")
                data = response.json()
                if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), dict):
                    raise ValueError("中心关系响应无效")
                item = data["data"].get("item")
                return MediaMapping.model_validate(item) if item is not None else None
        except (TimeoutError, ValueError, TypeError, OSError, AsyncHttpRequestError):
            with self._lock:
                self._retry_at = time.monotonic() + FAILURE_TTL
            return None

    async def async_query(self, query: MediaMappingQuery) -> MediaMapping | None:
        """
        优先复用本地正负缓存，合并在途键并在故障冷却期间跳过请求。

        :param query: 原来源身份与上下文
        :return: 匹配当前请求的共享关系，未命中或故障为 None
        """
        url = self._url()
        if not url:
            return None
        key = self._key(url, query)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                return cached[1].model_copy(deep=True) if cached[1] else None
            pending = self._queries.get(key)
            if pending is None and (time.monotonic() < self._retry_at or key in self._inflight
                                    or len(self._inflight) >= INFLIGHT_LIMIT):
                return None
            if pending is None:
                self._inflight.add(key)
                completion: concurrent.futures.Future[MediaMapping | None] = concurrent.futures.Future()
                self._queries[key] = completion
        if pending is not None:
            try:
                # 不让一个请求的取消传递给其他调用方正在使用的中心查询。
                result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(pending)), timeout=QUERY_TIMEOUT)
                return result.model_copy(deep=True) if result else None
            except TimeoutError:
                return None
        result = None
        try:
            result = await self._request(url, query.model_dump(mode="json", exclude_none=True))
            if result and (result.media_source != query.media_source or result.media_id != query.media_id
                           or (query.type and result.type != query.type)
                           or (query.season is not None and result.season not in (None, query.season))):
                result = None
            with self._lock:
                ttl = FAILURE_TTL if time.monotonic() < self._retry_at else POSITIVE_TTL if result else NEGATIVE_TTL
                self._remember(key, result, ttl)
            return result.model_copy(deep=True) if result else None
        finally:
            with self._lock:
                self._inflight.discard(key)
                self._queries.pop(key, None)
                completion.set_result(result)

    def query(self, query: MediaMappingQuery) -> MediaMapping | None:
        """
        在宿主循环执行有总超时的查询，同步等待也有独立上限。

        :param query: 原来源身份与上下文
        :return: 已知关系；不可执行或超时为 None
        """
        url = self._url()
        if not url:
            return None
        with self._lock:
            cached = self._cache.get(self._key(url, query))
            if cached and cached[0] > time.monotonic():
                return cached[1].model_copy(deep=True) if cached[1] else None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # 同步兼容调用落在事件循环线程时不能阻塞宿主。
            return None
        loop = main_loop_registry.current
        if loop is None or not loop.is_running() or loop.is_closed():
            return asyncio.run(self.async_query(query))
        try:
            future = get_task_registry().submit_threadsafe(self.async_query(query), loop=loop, owner="media-mapping.query")
            return cast(MediaMapping | None, future.result(timeout=QUERY_TIMEOUT + 0.1))
        except concurrent.futures.TimeoutError:
            future.cancel()
            return None
        except (RuntimeError, concurrent.futures.CancelledError):
            return None

    def share(self, mapping: MediaMapping) -> None:
        """
        立即缓存成功转换，并有界投递后台共享；公共写入不阻塞业务结果。

        :param mapping: 转换成功后的原身份与 TMDB 目标
        """
        url = self._url()
        if not url:
            return
        query = MediaMappingQuery(**mapping.model_dump(include={"media_source", "media_id", "type", "season"}))
        key = self._key(url, query)
        with self._lock:
            self._remember(key, mapping.model_copy(deep=True), POSITIVE_TTL)
            if time.monotonic() < self._retry_at or key in self._inflight or len(self._inflight) >= INFLIGHT_LIMIT:
                return
            self._inflight.add(key)
        coroutine = self._report(url, key, mapping)
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                loop = main_loop_registry.current
                if loop is not None and loop.is_running() and not loop.is_closed():
                    get_task_registry().submit_threadsafe(coroutine, loop=loop, owner="media-mapping.share")
                else:
                    # 独立同步命令没有宿主循环，上报仍受 500 毫秒总预算保护。
                    asyncio.run(coroutine)
            else:
                get_task_registry().create(coroutine, owner="media-mapping.share")
        except RuntimeError:
            coroutine.close()
            with self._lock:
                self._inflight.discard(key)

    async def _report(self, url: str, key: str, mapping: MediaMapping) -> None:
        """
        消费短时上报并按服务器的既有关系修正缓存，结束后释放并发名额。

        :param url: 中心地址
        :param key: 共享查询键
        :param mapping: 本地新转换的关系
        """
        try:
            confirmed = await self._request(url, mapping.model_dump(mode="json"), report=True)
            with self._lock:
                if time.monotonic() >= self._retry_at:
                    self._remember(key, confirmed, POSITIVE_TTL if confirmed else NEGATIVE_TTL)
        finally:
            with self._lock:
                self._inflight.discard(key)
