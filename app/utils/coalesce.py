"""
通用时间窗口事件合并器。

定位：在固定时间窗口内对相同 key 的重复事件做合并，避免下游（通常是日志、告警、上报）被高频重复事件刷爆。

典型场景：同一原因的高频拦截 warning、同一目标的连续失败告警、同一错误码的批量上报——首条事件立即输出保留上下文，
后续命中在窗口内合并为一条计数摘要。
"""

import asyncio
import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Hashable, Optional, Union

from app.log import logger


class CoalesceDecision(Enum):
    """
    `EventCoalescer.record` 的返回值，告知调用方对当前事件应采取的动作。
    """

    # 首次事件：调用方应立即按原样输出（写日志、发告警等）
    EMIT = "emit"
    # 窗口内已合并：调用方静默，不再输出
    SUPPRESS = "suppress"


@dataclass(frozen=True)
class CoalesceSummary:
    """
    窗口结束时回调给 `on_flush` 的聚合摘要，描述该窗口内被合并的事件。
    """

    # 聚合键，与 `record` 调用方传入的 key 一致
    key: Hashable
    # 窗口内同 key 命中总次数，包含首条 EMIT 的事件
    count: int
    # 首条事件的 payload，便于摘要里附"样例"以减少信息丢失
    first_payload: Any
    # 该窗口的时长（秒），与 coalescer 构造时一致
    window_seconds: float


# `on_flush` 回调签名：同步或 async 均可，由 coalescer 内部按需调度
OnFlushCallback = Callable[[CoalesceSummary], Union[Awaitable[None], None]]


@dataclass
class _BucketState:
    """
    单个 key 的窗口内状态；仅供 `EventCoalescer` 内部使用。
    """

    # 首条事件 payload，原样保留用于 flush 摘要
    first_payload: Any
    # 窗口内累计命中次数（含首条）
    count: int
    # `loop.call_later` 返回的 handle，用于 close() 时取消
    flush_handle: Optional[asyncio.TimerHandle]


class EventCoalescer:
    """
    时间窗口内对相同 key 的重复事件做合并；

    工作流程：
    - 首次出现某 key：`record` 返回 `EMIT`，调用方按原样输出事件；
      coalescer 通过 `loop.call_later(window_seconds, ...)` 注册 flush。
    - 窗口内同 key 再次出现：`record` 返回 `SUPPRESS`，累加计数。
    - 窗口到期：取出 bucket，若 `count > 1` 则触发 `on_flush(summary)`；
      `count == 1` 时认为单次事件已被首条 EMIT 完整表达，不再补摘要。

    线程模型：所有公开方法均为 `async`，仅设计在单个事件循环内使用。
    bucket 字典的读写均落在不含 `await` 的同步段内，靠事件循环的协作式调度
    天然原子，因此不需要显式锁；也避免了模块级实例化的 `asyncio.Lock` 在
    跨事件循环复用时可能触发的 `RuntimeError`。
    """

    def __init__(
        self,
        window_seconds: float,
        on_flush: OnFlushCallback,
        source: str = "",
    ) -> None:
        """
        :param window_seconds: 合并窗口时长（秒），必须 > 0
        :param on_flush: 窗口到期且 count>1 时回调；同步或 async 函数均可
        :param source: 业务来源标识，仅用于内部 debug 日志的前缀，便于区分多
            个 coalescer 的来源；不会出现在 `on_flush` 摘要里
        """
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._window_seconds = window_seconds
        self._on_flush = on_flush
        self._source = source
        self._buckets: Dict[Hashable, _BucketState] = {}
        self._is_flush_async = inspect.iscoroutinefunction(on_flush)

    @property
    def window_seconds(self) -> float:
        """
        合并窗口时长（秒），供外部只读。
        """
        return self._window_seconds

    async def record(
        self, key: Hashable, payload: Any = None
    ) -> CoalesceDecision:
        """
        登记一次事件。

        :param key: 聚合键，必须可哈希；推荐使用 tuple 组合业务维度（如
            `(host, reason)`），避免不同业务维度互相吞并
        :param payload: 事件附加信息，仅在该 key 在当前窗口内"首次出现"
            时被保留，用于 flush 摘要里附样例
        :return: `EMIT` 表示调用方应立即输出原事件；`SUPPRESS` 表示窗口
            内已合并，调用方应静默
        """
        bucket = self._buckets.get(key)
        if bucket is None:
            handle = self._schedule_flush(key)
            self._buckets[key] = _BucketState(
                first_payload=payload,
                count=1,
                flush_handle=handle,
            )
            return CoalesceDecision.EMIT
        bucket.count += 1
        return CoalesceDecision.SUPPRESS

    async def close(self) -> None:
        """
        立即 flush 所有未到期窗口并清空内部状态。

        典型用于进程退出路径与单元测试。已注册的 `loop.call_later` 句柄
        会被取消，避免在事件循环关闭后再被触发；count>1 的 bucket 同步
        调用 `on_flush`（async on_flush 会被 await）。
        """
        buckets = list(self._buckets.items())
        self._buckets.clear()
        for key, bucket in buckets:
            if bucket.flush_handle is not None:
                bucket.flush_handle.cancel()
            await self._emit_summary_if_needed(key, bucket)

    def _schedule_flush(self, key: Hashable) -> asyncio.TimerHandle:
        """
        为指定 key 注册窗口到期 flush。

        `call_later` 回调本身只能是同步函数，因此用 `asyncio.create_task`
        把异步 flush 链接回事件循环。捕获事件循环异常并降级为 debug 日志，
        避免基础设施层把异常抛回业务调用方。
        """
        loop = asyncio.get_running_loop()
        return loop.call_later(self._window_seconds, self._on_flush_timer, key)

    def _on_flush_timer(self, key: Hashable) -> None:
        """
        `loop.call_later` 到期回调：从事件循环里把异步 flush 任务接力起来。
        """
        try:
            asyncio.get_running_loop().create_task(self._flush_key(key))
        except RuntimeError as exc:
            # 事件循环已关闭等罕见路径：记录后丢弃，避免影响其它 bucket
            self._log_debug(f"flush 调度失败，已忽略 key={key!r}: {exc}")

    async def _flush_key(self, key: Hashable) -> None:
        """
        窗口到期后的实际 flush 路径：取出 bucket 并按需调用 `on_flush`。
        """
        bucket = self._buckets.pop(key, None)
        if bucket is None:
            return
        await self._emit_summary_if_needed(key, bucket)

    async def _emit_summary_if_needed(
        self, key: Hashable, bucket: _BucketState
    ) -> None:
        """
        仅当窗口内命中次数 > 1 时输出聚合摘要。

        on_flush 的同步/异步形态在构造时已识别；该方法负责按形态正确调度，
        并把消费者异常吞掉转为 debug 日志，避免基础设施把上层业务搞崩。
        """
        if bucket.count <= 1:
            return
        summary = CoalesceSummary(
            key=key,
            count=bucket.count,
            first_payload=bucket.first_payload,
            window_seconds=self._window_seconds,
        )
        try:
            if self._is_flush_async:
                await self._on_flush(summary)  # type: ignore[misc]
            else:
                self._on_flush(summary)
        except Exception as exc:  # noqa: BLE001 - 基础设施不能因消费者异常崩溃
            self._log_debug(f"on_flush 回调异常已吞: key={key!r}, error={exc}")

    def _log_debug(self, message: str) -> None:
        """
        内部 debug 日志统一加 source 前缀，便于排查多 coalescer 共存时的来源。
        """
        if self._source:
            logger.debug(f"[EventCoalescer:{self._source}] {message}")
        else:
            logger.debug(f"[EventCoalescer] {message}")
