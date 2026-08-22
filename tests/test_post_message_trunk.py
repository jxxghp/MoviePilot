"""消息发送主干的分发语义。

post_message 的全部发送点都汇入消息队列，由队列回调落到分发层。这条主干是多播：
认领的渠道都要发出，任何短路都会让消息只到达第一个渠道。
"""
import sys
import unittest
from types import ModuleType as _PyModuleType
from unittest.mock import Mock

sys.modules.setdefault("qbittorrentapi", _PyModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", _PyModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)

from app.application.orchestration.context import ChainRuntimeContext  # noqa: E402
from app.application.orchestration import ChainBase  # noqa: E402
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher  # noqa: E402


class ChannelModule:
    """渠道模块替身，post_message 返回 None，与全部真实渠道实现一致"""

    def __init__(self, name: str, calls: list, priority: int = 1, result=None):
        """
        :param name: 渠道名，同时作为调用记录的标识
        :param calls: 共享的调用记录
        :param priority: 模块优先级
        :param result: post_message 的返回值
        """
        self._name = name
        self._calls = calls
        self._priority = priority
        self._result = result

    def get_name(self) -> str:
        """模块名称"""
        return self._name

    def get_priority(self) -> int:
        """模块优先级"""
        return self._priority

    def post_message(self, message=None, **kwargs):
        """发送消息"""
        self._calls.append(self._name)
        return self._result


def build_chain(*modules):
    """
    构造与真实模块、插件运行态隔离的 ChainBase，并捕获队列拿到的回调

    :param modules: 运行态模块替身
    :return: (链基类实例, 队列回调, 两条分发路径的访问计数器)
    """
    counters = {"broadcast_scans": 0, "index_lookups": 0}

    def running_modules(method):
        """广播路径：遍历全体"""
        counters["broadcast_scans"] += 1
        return iter([m for m in modules if callable(getattr(m, method, None))])

    def providers_for(method):
        """查询路径：按能力查表"""
        counters["index_lookups"] += 1
        return tuple(sorted(
            (m for m in modules if callable(getattr(m, method, None))),
            key=lambda m: m.get_priority(),
        ))

    module_manager = Mock()
    module_manager.get_running_modules.side_effect = running_modules
    module_manager.providers_for.side_effect = providers_for
    plugin_manager = Mock()
    plugin_manager.running_plugins = {}
    plugin_manager.get_plugin_modules.return_value = {}

    captured = {}

    def queue_factory(callback):
        """记录队列拿到的回调，替身队列本身不参与断言"""
        captured["callback"] = callback
        return Mock()

    chain = ChainBase(runtime_context=ChainRuntimeContext(
        module_manager=module_manager,
        plugin_manager=plugin_manager,
        event_manager=Mock(),
        message_oper=Mock(),
        message_helper=Mock(),
        file_cache=Mock(),
        async_file_cache=Mock(),
        message_queue_factory=queue_factory,
        module_dispatcher_factory=ModuleInvocationDispatcher,
    ))
    return chain, captured["callback"], counters


class PostMessageTrunkTest(unittest.TestCase):
    """消息主干：队列回调经能力索引分发到每一个认领的渠道"""

    def test_queue_callback_is_the_multicast_primitive(self):
        """构造时把队列回调接到多播原语，而非会短路的聚合分发"""
        _, callback, _ = build_chain()

        self.assertEqual(callback.__func__, ChainBase.multicast)

    def test_queue_callback_dispatches_via_capability_index(self):
        """队列回调查能力索引，不回落到全体遍历。

        渠道是一个族类，「谁能发消息」是查询而非通知，因此代价应为 O(k) 而不是 O(n)。
        """
        calls = []
        _, callback, counters = build_chain(
            ChannelModule("wechat", calls),
            ChannelModule("telegram", calls, priority=2),
        )

        callback("post_message", message=None)

        self.assertEqual(sorted(calls), ["telegram", "wechat"])
        self.assertGreater(counters["index_lookups"], 0)
        self.assertEqual(counters["broadcast_scans"], 0)

    def test_every_channel_receives_even_when_one_answers(self):
        """某个渠道返回了非空值，其余渠道仍须收到。

        锁死多播语义：单播在首个非空答案处短路，聚合分发在首个标量答案处短路，
        用在这里都会让消息只发出一份。
        """
        calls = []
        _, callback, _ = build_chain(
            ChannelModule("wechat", calls, priority=1, result="sent"),
            ChannelModule("telegram", calls, priority=2),
            ChannelModule("slack", calls, priority=3),
        )

        callback("post_message", message=None)

        self.assertEqual(calls, ["wechat", "telegram", "slack"])

    def test_failing_channel_does_not_block_the_rest(self):
        """单个渠道抛错不影响其余渠道发出"""
        calls = []

        class BrokenChannel(ChannelModule):
            """发送时抛错的渠道替身"""

            def post_message(self, message=None, **kwargs):
                """发送消息"""
                self._calls.append(self._name)
                raise RuntimeError("渠道不可用")

        _, callback, _ = build_chain(
            BrokenChannel("broken", calls, priority=1),
            ChannelModule("telegram", calls, priority=2),
        )

        callback("post_message", message=None)

        self.assertEqual(calls, ["broken", "telegram"])


if __name__ == "__main__":
    unittest.main()
