"""媒体目录识别结果的进程内缓存。"""

import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from threading import RLock
from typing import Awaitable, Callable, TypeAlias

from app.domain.context import MusicInfo

AlbumSignature: TypeAlias = tuple[tuple[str, int, int], ...]
AlbumMapping: TypeAlias = dict[str, MusicInfo]
_CacheValue: TypeAlias = tuple[AlbumSignature, AlbumMapping]
_FlightKey: TypeAlias = tuple[str, AlbumSignature]


class AlbumDirectoryCache:
    """提供有界 LRU、隔离副本与同目录单飞的专辑识别缓存。"""

    def __init__(self, capacity: int) -> None:
        """按指定最大目录数初始化缓存。"""
        if capacity < 1:
            raise ValueError("专辑目录缓存容量必须大于零")
        self._capacity = capacity
        self._values: OrderedDict[str, _CacheValue] = OrderedDict()
        self._flights: dict[_FlightKey, Future[AlbumMapping]] = {}
        self._lock = RLock()

    def __len__(self) -> int:
        """返回当前缓存目录数。"""
        with self._lock:
            return len(self._values)

    def clear(self) -> None:
        """清空已完成结果；进行中的识别仍由原调用负责收尾。"""
        with self._lock:
            self._values.clear()

    def get(self, key: str, signature: AlbumSignature) -> AlbumMapping | None:
        """按目录与文件签名读取隔离副本，并刷新最近使用顺序。"""
        with self._lock:
            cached = self._values.get(key)
            if cached is None or cached[0] != signature:
                return None
            self._values.move_to_end(key)
            return deepcopy(cached[1])

    def put(self, key: str, signature: AlbumSignature, value: AlbumMapping) -> AlbumMapping:
        """保存隔离副本并逐项淘汰最久未使用目录。"""
        stored = deepcopy(value)
        with self._lock:
            self._values[key] = (signature, stored)
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
        flight, leader = self._claim(key, signature)
        if not leader:
            return deepcopy(flight.result())
        try:
            result = self.put(key, signature, resolver())
        except BaseException as error:
            self._finish(key, signature, flight, error=error)
            raise
        self._finish(key, signature, flight, result=result)
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
        flight, leader = self._claim(key, signature)
        if not leader:
            return deepcopy(await asyncio.shield(asyncio.wrap_future(flight)))
        try:
            result = self.put(key, signature, await resolver())
        except BaseException as error:
            self._finish(key, signature, flight, error=error)
            raise
        self._finish(key, signature, flight, result=result)
        return result

    def _claim(
        self,
        key: str,
        signature: AlbumSignature,
    ) -> tuple[Future[AlbumMapping], bool]:
        """取得目录签名的单飞凭据并标记当前调用是否为首个解析者。"""
        flight_key = (key, signature)
        with self._lock:
            flight = self._flights.get(flight_key)
            if flight is not None:
                return flight, False
            flight = Future()
            self._flights[flight_key] = flight
            return flight, True

    def _finish(
        self,
        key: str,
        signature: AlbumSignature,
        flight: Future[AlbumMapping],
        *,
        result: AlbumMapping | None = None,
        error: BaseException | None = None,
    ) -> None:
        """发布单飞结果并移除凭据，确保后续调用可重新解析。"""
        with self._lock:
            self._flights.pop((key, signature), None)
        if error is not None:
            flight.set_exception(error)
            flight.exception()
        else:
            flight.set_result(deepcopy(result if result is not None else {}))
