import asyncio
from unittest.mock import AsyncMock

from app.db.plugindata_oper import PluginDataOper
from app.plugins import _PluginBase


class DemoPlugin(_PluginBase):
    """用于验证插件数据接口的测试插件"""

    def init_plugin(self, config: dict = None):
        """初始化测试插件"""

    def get_state(self) -> bool:
        """返回测试插件运行状态"""
        return True

    def get_api(self) -> list[dict]:
        """返回测试插件API"""
        return []

    def get_form(self) -> tuple[None, dict]:
        """返回测试插件配置表单"""
        return None, {}

    def get_page(self) -> None:
        """返回测试插件详情页面"""

    def stop_service(self):
        """停止测试插件"""


def test_async_plugin_data_oper_saves_and_gets_data() -> None:
    """异步接口应支持新增、覆盖更新以及按键和全量读取。"""

    async def run_test() -> None:
        oper = PluginDataOper()
        plugin_id = "AsyncPluginDataOperTest"

        await oper.async_save(plugin_id, "settings", {"enabled": True})
        await oper.async_save(plugin_id, "settings", {"enabled": False})
        await oper.async_save(plugin_id, "history", [1, 2])

        assert await oper.async_get_data(plugin_id, "settings") == {
            "enabled": False
        }
        assert await oper.async_get_data(plugin_id, "missing") is None

        all_data = await oper.async_get_data(plugin_id)
        assert {item.key: item.value for item in all_data} == {
            "settings": {"enabled": False},
            "history": [1, 2],
        }

    asyncio.run(run_test())


def test_plugin_base_async_data_interfaces_delegate_plugin_id() -> None:
    """插件基类异步接口应默认使用类名并允许显式指定插件ID。"""

    async def run_test() -> None:
        plugin = DemoPlugin()
        plugin.plugindata.async_save = AsyncMock()
        plugin.plugindata.async_get_data = AsyncMock(return_value={"value": 1})

        await plugin.async_save_data("key", "value")
        result = await plugin.async_get_data("key", plugin_id="ClonePlugin")

        plugin.plugindata.async_save.assert_awaited_once_with(
            "DemoPlugin", "key", "value"
        )
        plugin.plugindata.async_get_data.assert_awaited_once_with(
            "ClonePlugin", "key"
        )
        assert result == {"value": 1}

    asyncio.run(run_test())
