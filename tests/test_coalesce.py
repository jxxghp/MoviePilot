"""
`EventCoalescer` 基础设施单元测试。

测试策略：用极短窗口（默认 0.05s）驱动真实事件循环触发 flush，避免引入
对时间 mock 的复杂度；同时通过 `asyncio.sleep` 让出控制权以保证 flush
回调被调度执行。
"""

import asyncio
from typing import List
from unittest import IsolatedAsyncioTestCase

from app.utils.coalesce import (
    CoalesceDecision,
    CoalesceSummary,
    EventCoalescer,
)


# 窗口尽量短，但要大于事件循环单次 tick 的开销，避免 flush 在 record 仍持锁时触发
_TEST_WINDOW = 0.05
# 等待窗口到期 + flush 任务完成所需的额外余量
_TEST_WAIT = _TEST_WINDOW * 4


class EventCoalescerTest(IsolatedAsyncioTestCase):
    """
    覆盖 EventCoalescer 的核心契约：首条 EMIT、窗口内 SUPPRESS、count>1
    时 flush 摘要、不同 key 互不影响、close() 立即 flush、on_flush 异常
    被吞、同步/async on_flush 都可用。
    """

    async def test_first_record_returns_emit(self):
        """
        某 key 在新窗口内的首次出现必须返回 EMIT，确保调用方按原样输出。
        """
        summaries: List[CoalesceSummary] = []
        coalescer = EventCoalescer(_TEST_WINDOW, summaries.append)

        decision = await coalescer.record(("host", "reason"), payload={"i": 1})

        self.assertIs(decision, CoalesceDecision.EMIT)
        await coalescer.close()

    async def test_subsequent_same_key_records_are_suppressed(self):
        """
        同一 key 在窗口内连续命中，第 2 次起返回 SUPPRESS。
        """
        coalescer = EventCoalescer(_TEST_WINDOW, lambda _s: None)
        await coalescer.record("k", payload="first")

        for _ in range(3):
            self.assertIs(
                await coalescer.record("k", payload="ignored"),
                CoalesceDecision.SUPPRESS,
            )
        await coalescer.close()

    async def test_window_expiry_flushes_summary_when_count_gt_one(self):
        """
        窗口到期且 count>1 时，on_flush 收到包含 count、first_payload、window 的摘要。
        """
        summaries: List[CoalesceSummary] = []
        coalescer = EventCoalescer(_TEST_WINDOW, summaries.append, source="test")
        key = ("h", "r")
        await coalescer.record(key, payload={"url": "u1"})
        await coalescer.record(key, payload={"url": "u2"})
        await coalescer.record(key, payload={"url": "u3"})

        await asyncio.sleep(_TEST_WAIT)

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.key, key)
        self.assertEqual(summary.count, 3)
        self.assertEqual(summary.first_payload, {"url": "u1"})
        self.assertEqual(summary.window_seconds, _TEST_WINDOW)

    async def test_window_expiry_does_not_flush_when_count_is_one(self):
        """
        窗口内只出现一次时，首条 EMIT 已表达完整事件，不再补发聚合摘要。
        """
        summaries: List[CoalesceSummary] = []
        coalescer = EventCoalescer(_TEST_WINDOW, summaries.append)
        await coalescer.record("solo", payload=None)

        await asyncio.sleep(_TEST_WAIT)

        self.assertEqual(summaries, [])

    async def test_different_keys_do_not_collapse(self):
        """
        不同 key 各自独立计数与 flush，互不吞并。
        """
        summaries: List[CoalesceSummary] = []
        coalescer = EventCoalescer(_TEST_WINDOW, summaries.append)
        await coalescer.record("a", payload="a1")
        await coalescer.record("b", payload="b1")
        await coalescer.record("a", payload="a2")
        await coalescer.record("b", payload="b2")
        await coalescer.record("a", payload="a3")

        await asyncio.sleep(_TEST_WAIT)

        by_key = {s.key: s for s in summaries}
        self.assertEqual(set(by_key.keys()), {"a", "b"})
        self.assertEqual(by_key["a"].count, 3)
        self.assertEqual(by_key["a"].first_payload, "a1")
        self.assertEqual(by_key["b"].count, 2)
        self.assertEqual(by_key["b"].first_payload, "b1")

    async def test_new_window_after_flush_emits_again(self):
        """
        窗口结束后下一条同 key 事件应被视为新窗口的首条，返回 EMIT。
        """
        coalescer = EventCoalescer(_TEST_WINDOW, lambda _s: None)
        await coalescer.record("k", payload=1)
        await coalescer.record("k", payload=2)
        await asyncio.sleep(_TEST_WAIT)

        decision = await coalescer.record("k", payload=3)

        self.assertIs(decision, CoalesceDecision.EMIT)
        await coalescer.close()

    async def test_close_flushes_pending_buckets_immediately(self):
        """
        close() 必须取消未到期 timer 并立即触发 count>1 的 bucket flush，
        用于进程退出路径。
        """
        # 使用一个足够长的窗口，确保自然到期不会先于 close 触发
        summaries: List[CoalesceSummary] = []
        coalescer = EventCoalescer(1.0, summaries.append)
        await coalescer.record("k", payload="first")
        await coalescer.record("k", payload="second")

        await coalescer.close()

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].count, 2)
        self.assertEqual(summaries[0].first_payload, "first")

    async def test_close_does_not_emit_when_count_is_one(self):
        """
        close() 与正常窗口到期一致，count==1 时不输出摘要。
        """
        summaries: List[CoalesceSummary] = []
        coalescer = EventCoalescer(1.0, summaries.append)
        await coalescer.record("k", payload="only")

        await coalescer.close()

        self.assertEqual(summaries, [])

    async def test_async_on_flush_is_awaited(self):
        """
        on_flush 为 async 函数时应被正确 await，而不是被丢弃成协程对象。
        """
        awaited: List[CoalesceSummary] = []

        async def on_flush(summary: CoalesceSummary) -> None:
            await asyncio.sleep(0)
            awaited.append(summary)

        coalescer = EventCoalescer(_TEST_WINDOW, on_flush)
        await coalescer.record("k", payload="a")
        await coalescer.record("k", payload="b")

        await asyncio.sleep(_TEST_WAIT)

        self.assertEqual(len(awaited), 1)
        self.assertEqual(awaited[0].count, 2)

    async def test_on_flush_exception_is_swallowed(self):
        """
        on_flush 抛异常不能影响 coalescer 自身或上层调用方，仅 debug 记录。
        """
        def on_flush(_summary: CoalesceSummary) -> None:
            raise RuntimeError("boom")

        coalescer = EventCoalescer(_TEST_WINDOW, on_flush)
        await coalescer.record("k", payload="x")
        await coalescer.record("k", payload="y")

        await asyncio.sleep(_TEST_WAIT)

        # 异常被吞，新窗口可以继续接受 record
        self.assertIs(
            await coalescer.record("k", payload="z"),
            CoalesceDecision.EMIT,
        )
        await coalescer.close()

    async def test_invalid_window_raises(self):
        """
        非正数窗口值在构造期即拒绝，避免运行期出现 0 或负窗口的死循环 flush。
        """
        with self.assertRaises(ValueError):
            EventCoalescer(0, lambda _s: None)
        with self.assertRaises(ValueError):
            EventCoalescer(-1.0, lambda _s: None)
