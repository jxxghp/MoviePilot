import asyncio
import sys
import unittest
from types import ModuleType
from unittest.mock import Mock

sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", ModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)

from app.chain import ChainBase
from app.schemas import RateLimitExceededException


class _LimitedModule:
    def get_name(self):
        """
        返回测试模块名称。
        """
        return "限流测试模块"

    def get_priority(self):
        """
        返回测试模块优先级。
        """
        return 1

    def limited_method(self, raise_exception: bool = False):
        """
        模拟同步模块在本地限流期间跳过调用。
        """
        raise RateLimitExceededException("[limited_method] 限流期间，跳过调用")

    async def async_limited_method(self, raise_exception: bool = False):
        """
        模拟异步模块在本地限流期间跳过调用。
        """
        raise RateLimitExceededException("[async_limited_method] 限流期间，跳过调用")


class ChainRateLimitTest(unittest.TestCase):
    def _build_chain(self):
        """
        构造隔离的 ChainBase，避免依赖真实模块和插件运行状态。
        """
        chain = ChainBase()
        limited_module = _LimitedModule()
        chain.pluginmanager = Mock()
        chain.pluginmanager.get_plugin_modules.return_value = {}
        chain.modulemanager = Mock()
        chain.modulemanager.get_running_modules.return_value = [limited_module]
        chain.messagehelper = Mock()
        chain.eventmanager = Mock()
        return chain

    def test_rate_limit_is_not_reported_as_system_error(self):
        """
        本地限流跳过不应写入系统错误通知或事件。
        """
        chain = self._build_chain()

        result = chain.run_module("limited_method")

        self.assertIsNone(result)
        chain.messagehelper.put.assert_not_called()
        chain.eventmanager.send_event.assert_not_called()

    def test_rate_limit_can_still_be_raised_explicitly(self):
        """
        调用方显式要求抛出异常时，限流异常应继续向上抛出。
        """
        chain = self._build_chain()

        with self.assertRaises(RateLimitExceededException):
            chain.run_module("limited_method", raise_exception=True)

        chain.messagehelper.put.assert_not_called()
        chain.eventmanager.send_event.assert_not_called()

    def test_async_rate_limit_is_not_reported_as_system_error(self):
        """
        异步模块的本地限流跳过也不应触发系统错误路径。
        """
        chain = self._build_chain()

        result = asyncio.run(chain.async_run_module("async_limited_method"))

        self.assertIsNone(result)
        chain.messagehelper.put.assert_not_called()
        chain.eventmanager.send_event.assert_not_called()
