"""媒体目录识别结果的进程内缓存。"""

import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from threading import RLock
from time import monotonic
from typing import Awaitable, Callable, TypeAlias

from app.domain.context import MusicInfo
from app.domain.music import MusicDirectoryMatch

AlbumSignature: TypeAlias = tuple[tuple[str, int, int], ...]
AlbumMapping: TypeAlias = dict[str, MusicInfo]
_CacheValue: TypeAlias = tuple[AlbumSignature, AlbumMapping, float]
_FlightKey: TypeAlias = tuple[str, AlbumSignature, int]


class AlbumDirectoryCache:
    """提供有界 LRU、隔离副本与同目录单飞的专辑识别缓存。"""

    def __init__(self, capacity: int, *, ttl: float = 3600, negative_ttl: float = 300,
                 failure_ttl: float = 15, clock: Callable[[], float] | None = None) -> None:
        """初始化有界缓存；无匹配短暂记忆，服务故障仅用于抑制同批文件的重试风暴。"""
        if capacity < 1:
            raise ValueError("专辑目录缓存容量必须大于零")
        self._capacity = capacity
        self._ttl, self._negative_ttl, self._failure_ttl = ttl, negative_ttl, failure_ttl
        self._clock = clock or monotonic
        self._generation = 0
        self._values: OrderedDict[str, _CacheValue] = OrderedDict()
        self._flights: dict[_FlightKey, Future[AlbumMapping]] = {}
        self._lock = RLock()

    def __len__(self) -> int:
        """返回当前缓存目录数。"""
        with self._lock:
            return len(self._values)

    def clear(self) -> None:
        """清空结果并切换代际；新请求不等待旧识别，旧结果也不能重新回填。"""
        with self._lock:
            self._values.clear()
            self._generation += 1

    def get(self, key: str, signature: AlbumSignature) -> AlbumMapping | None:
        """按目录与文件签名读取隔离副本，并刷新最近使用顺序。"""
        with self._lock:
            cached = self._values.get(key)
            if cached is None:
                return None
            if cached[0] != signature or cached[2] <= self._clock():
                self._values.pop(key, None)
                return None
            self._values.move_to_end(key)
            return deepcopy(cached[1])

    def put(self, key: str, signature: AlbumSignature, value: AlbumMapping, *, generation: int | None = None) -> AlbumMapping:
        """保存隔离副本并逐项淘汰最久未使用目录。"""
        stored = deepcopy(value)
        status = value.recognition.get("status") if isinstance(value, MusicDirectoryMatch) else None
        ttl = self._failure_ttl if status in {"service_error", "budget_exhausted"} else self._ttl if value else self._negative_ttl
        with self._lock:
            if generation is not None and generation != self._generation:
                return deepcopy(stored)
            self._values[key] = (signature, stored, self._clock() + max(ttl, 0))
            self._values.move_to_end(key)
            while len(self._values) > self._capacity:
                self._values.popitem(last=False)
        return deepcopy(stored)

    def resolve(
        self,
        key: str,
        signature: AlbumSignature,
        resolver: Callable[[], AlbumMapping],
    ) -> AlbumMapping:
        """同步解析一次目录；并发调用复用首个调用的结果或异常。"""
        cached = self.get(key, signature)
        if cached is not None:
            return cached
        flight, leader, generation = self._claim(key, signature)
        if not leader:
            return deepcopy(flight.result())
        try:
            cached = self.get(key, signature)
            result = cached if cached is not None else self.put(key, signature, resolver(), generation=generation)
        except BaseException as error:
            self._finish(key, signature, flight, generation, error=error)
            raise
        self._finish(key, signature, flight, generation, result=result)
        return result

    async def async_resolve(
        self,
        key: str,
        signature: AlbumSignature,
        resolver: Callable[[], Awaitable[AlbumMapping]],
    ) -> AlbumMapping:
        """异步解析一次目录；等待者不阻塞事件循环并复用首个结果。"""
        cached = self.get(key, signature)
        if cached is not None:
            return cached
        flight, leader, generation = self._claim(key, signature)
        if not leader:
            return deepcopy(await asyncio.shield(asyncio.wrap_future(flight)))
        try:
            cached = self.get(key, signature)
            result = cached if cached is not None else self.put(key, signature, await resolver(), generation=generation)
        except BaseException as error:
            self._finish(key, signature, flight, generation, error=error)
            raise
        self._finish(key, signature, flight, generation, result=result)
        return result

    def _claim(
        self,
        key: str,
        signature: AlbumSignature,
    ) -> tuple[Future[AlbumMapping], bool, int]:
        """取得目录签名的单飞凭据并标记当前调用是否为首个解析者。"""
        with self._lock:
            generation = self._generation
            flight_key = (key, signature, generation)
            flight = self._flights.get(flight_key)
            if flight is not None:
                return flight, False, generation
            flight = Future()
            self._flights[flight_key] = flight
            return flight, True, generation

    def _finish(
        self,
        key: str,
        signature: AlbumSignature,
        flight: Future[AlbumMapping],
        generation: int,
        *,
        result: AlbumMapping | None = None,
        error: BaseException | None = None,
    ) -> None:
        """发布单飞结果并移除凭据，确保后续调用可重新解析。"""
        with self._lock:
            self._flights.pop((key, signature, generation), None)
        if error is not None:
            flight.set_exception(error)
            flight.exception()
        else:
            flight.set_result(deepcopy(result if result is not None else {}))
