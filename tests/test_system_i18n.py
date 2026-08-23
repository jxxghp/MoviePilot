from unittest.mock import patch
from types import SimpleNamespace

from app.api.endpoints import system as system_endpoint
from app.runtime.localization import LocaleHelper


class _FakeModuleManager:
    """提供 system 模块接口测试所需的最小模块管理器。"""

    def list_specs(self) -> tuple:
        """返回 manifest 元数据视图。"""
        return (
            SimpleNamespace(id="DoubanModule", metadata={"name": "豆瓣"}),
        )

    def test(self, moduleid: str) -> tuple[bool, str]:
        """返回模块测试结果"""
        return False, "模块不支持测试"


def test_system_modulelist_keeps_chinese_name_and_adds_i18n_name():
    """模块列表接口应保留旧中文字段，并提供前端可用的多语言字段。"""
    token = LocaleHelper.set_current_locale("en-US")
    with patch.object(system_endpoint, "get_module_manager", return_value=_FakeModuleManager()):
        try:
            response = system_endpoint.modulelist(_="token")
        finally:
            LocaleHelper.reset_current_locale(token)

    module = response.data["modules"][0]
    assert module["id"] == "DoubanModule"
    assert module["name"] == "豆瓣"
    assert module["name_i18n"] == "Douban"
    assert module["name_key"] == "system.modules.DoubanModule.name"


def test_system_moduletest_localizes_message():
    """模块测试接口应按当前请求语言直接返回翻译后的 message。"""
    token = LocaleHelper.set_current_locale("en-US")
    with patch.object(system_endpoint, "get_module_manager", return_value=_FakeModuleManager()):
        try:
            response = system_endpoint.moduletest("DoubanModule", _="token")
        finally:
            LocaleHelper.reset_current_locale(token)

    assert response.success is False
    assert response.message == "Module does not support testing"
    assert not hasattr(response, "message_i18n")
