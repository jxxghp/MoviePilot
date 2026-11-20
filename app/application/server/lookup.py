"""
详情页按需查找 TMDB 关系的进程内协调。

本模块只负责共享关系优先、本地结果记录、同条目合并、并发上限和后台任务生命周期；
具体匹配由启动组合根注入，Application 不反向依赖 Chain。状态只在本进程内有效，
安全模式下多个 worker 各自持有一份。
"""

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Final, cast

from app.application.server.mapping import MediaMappingPort, get_media_mapping_port
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry, get_task_registry
from app.schemas.mapping import MediaMapping, MediaMappingMatchQuery

# 本地结果记录的有效期，单位秒；与共享客户端的未命中缓存一致。
RECORD_TTL: Final = 600
# 本地结果记录的条数上限，超出时淘汰最久未访问的条目。
RECORD_LIMIT: Final = 1024
# 同时执行的后台查找条目上限；请求已断开但仍在运行的查找也计入。
MATCH_LIMIT: Final = 4
# 单次后台查找的总预算，单位秒；超时按失败处理，不写入本地记录。
MATCH_TIMEOUT: Final = 180.0

MediaMappingMatcher = Callable[[MediaMappingMatchQuery], Awaitable[MediaMapping | None]]


class MediaMappingLookupBusy(Exception):
    """
    同时查找的条目已达上限，调用方应稍后重试。
    """


class MediaMappingLookupUnavailable(Exception):
    """
    后台任务无法创建或已被关停取消，本次查找没有结论。
    """


class MediaMappingLookupFailed(Exception):
    """
    后台查找超时或出现未预期异常，本次查找没有结论，也不写入本地记录。
    """


class _MatchFailure:
    """
    后台查找失败的终态标记，避免把业务异常抛给任务登记器重复报告。
    """


_MATCH_FAILURE: Final = _MatchFailure()
_Outcome = MediaMapping | None | _MatchFailure


def _describe(query: MediaMappingMatchQuery) -> str:
    """
    生成日志使用的条目描述，例如 `douban:123（电视剧 第2季）`。

    :param query: 已校验的查找请求
    :return: 包含来源、ID、类型和季号的简短描述
    """
    kind = "电影" if query.type == "movie" else "电视剧"
    season = f" 第{query.season}季" if query.season is not None else ""
    return f"{query.media_source.value}:{query.media_id}（{kind}{season}）"


class MediaMappingLookup:
    """
    串联共享关系、本地记录与后台匹配，保证同一条目同一时间只匹配一次。

    共享关系始终优先；本地记录只在共享查询未命中时补充，记录找到与没找到两种结论。
    后台查找由任务登记器持有，请求断开不会取消；终态统一在任务完成回调中收口。
    只在宿主事件循环中调用。
    """

    def __init__(
        self,
        matcher: MediaMappingMatcher,
        *,
        port: Callable[[], MediaMappingPort | None] = get_media_mapping_port,
        tasks: Callable[[], TaskRegistry] = get_task_registry,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        保存注入能力，并建立有界记录、在途表及保护二者的锁。

        :param matcher: 共享关系未命中时执行的订阅同款匹配
        :param port: 返回当前共享关系端口，未装配时返回 None
        :param tasks: 返回持有后台查找的任务登记器
        :param clock: 单调时钟，单位秒，测试可注入
        """
        self._matcher = matcher
        self._port = port
        self._tasks = tasks
        self._clock = clock
        self._records: OrderedDict[str, tuple[float, MediaMapping | None]] = OrderedDict()
        self._flights: dict[str, asyncio.Task[_Outcome]] = {}
        self._lock = threading.Lock()

    async def lookup(self, query: MediaMappingMatchQuery) -> MediaMapping | None:
        """
        查找条目的 TMDB 关系：共享关系优先，其次本地记录，最后合并或启动后台匹配。

        :param query: 已校验的原来源身份、影视类型和季号
        :return: TMDB 关系；查找已完成但没有找到时为 None
        :raises MediaMappingLookupBusy: 同时查找的条目已达上限
        :raises MediaMappingLookupUnavailable: 后台任务无法创建或已被关停取消
        :raises MediaMappingLookupFailed: 后台查找超时或出现未预期异常
        """
        port = self._port()
        shared = await port.async_query(query) if port is not None else None
        if shared is not None:
            return shared
        key = query.model_dump_json()
        with self._lock:
            record = self._records.get(key)
            if record is not None and record[0] > self._clock():
                self._records.move_to_end(key)
                return record[1].model_copy(deep=True) if record[1] else None
            task = self._flights.get(key)
            if task is None:
                if len(self._flights) >= MATCH_LIMIT:
                    logger.info(f"详情页查找 TMDB 链接：{_describe(query)} → 同时查找已达 {MATCH_LIMIT} 个，拒绝")
                    raise MediaMappingLookupBusy()
                task = self._start(key, query)
        return await self._wait(task)

    def _start(self, key: str, query: MediaMappingMatchQuery) -> asyncio.Task[_Outcome]:
        """
        在锁内创建并登记后台查找；任务登记器拒绝时不留下在途项。

        :param key: 规范化的查找键
        :param query: 已校验的查找请求
        :return: 由任务登记器持有的后台查找任务
        :raises MediaMappingLookupUnavailable: 任务登记器正在关闭
        """
        try:
            task = self._tasks().create(self._run(query), owner="media-mapping.match")
        except RuntimeError as err:
            raise MediaMappingLookupUnavailable() from err
        flight = cast(asyncio.Task[_Outcome], task)
        self._flights[key] = flight
        flight.add_done_callback(partial(self._settle, key))
        return flight

    async def _run(self, query: MediaMappingMatchQuery) -> _Outcome:
        """
        在总预算内执行匹配并记录结论；超时和异常转换为失败终态，取消照常向外传播。

        :param query: 已校验的查找请求
        :return: 找到的关系、没找到时的 None，或失败终态
        """
        label = _describe(query)
        budget = asyncio.timeout(MATCH_TIMEOUT)
        try:
            async with budget:
                result = await self._matcher(query)
        except Exception as err:  # 插件转换事件可能抛出任意异常，统一按本次失败处理
            if isinstance(err, TimeoutError) and budget.expired():
                logger.warning(f"详情页查找 TMDB 链接：{label} → 超时（{MATCH_TIMEOUT:g} 秒）")
            else:
                # 匹配内部自行抛出的超时不是总预算到期，保留原始原因便于排查。
                logger.error(f"详情页查找 TMDB 链接：{label} → 失败：{err!r}")
            return _MATCH_FAILURE
        if result is None:
            logger.info(f"详情页查找 TMDB 链接：{label} → 未找到")
        else:
            season = f" 第{result.tmdb_season}季" if result.tmdb_season is not None else ""
            logger.info(f"详情页查找 TMDB 链接：{label} → 找到 TMDB {result.tmdb_type}/{result.tmdb_id}{season}")
        return result

    def _settle(self, key: str, task: asyncio.Task[_Outcome]) -> None:
        """
        在任务终态统一移出在途表；只有正常完成并得到结论时才写入本地记录。

        任务在首次调度前被取消时协程体不会执行，因此不能依赖协程内的 finally 收口。

        :param key: 规范化的查找键
        :param task: 已结束的后台查找任务
        """
        concluded = not task.cancelled() and task.exception() is None
        outcome = task.result() if concluded else _MATCH_FAILURE
        with self._lock:
            if self._flights.get(key) is task:
                del self._flights[key]
            if isinstance(outcome, _MatchFailure):
                return
            self._records[key] = (self._clock() + RECORD_TTL, outcome)
            self._records.move_to_end(key)
            while len(self._records) > RECORD_LIMIT:
                self._records.popitem(last=False)

    @staticmethod
    async def _wait(task: asyncio.Task[_Outcome]) -> MediaMapping | None:
        """
        等待后台查找的终态；等待方被取消不影响查找本身。

        :param task: 后台查找任务
        :return: 找到的关系副本，没找到时为 None
        :raises MediaMappingLookupUnavailable: 后台查找被取消，例如应用正在关停
        :raises MediaMappingLookupFailed: 后台查找超时或出现未预期异常
        """
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or current.cancelling() == 0):
                # 后台查找被取消而不是本请求被取消，不能把取消信号冒充为请求中断。
                raise MediaMappingLookupUnavailable() from None
            raise
        if isinstance(outcome, _MatchFailure):
            raise MediaMappingLookupFailed()
        return outcome.model_copy(deep=True) if outcome else None


_lookup: MediaMappingLookup | None = None


def configure_media_mapping_lookup(lookup: MediaMappingLookup | None) -> None:
    """
    由宿主组合根装配或释放详情页查找协调对象。

    :param lookup: 协调对象；None 表示释放
    """
    global _lookup
    _lookup = lookup


def get_media_mapping_lookup() -> MediaMappingLookup | None:
    """
    返回已装配的详情页查找协调对象。

    :return: 协调对象；未装配时为 None
    """
    return _lookup
