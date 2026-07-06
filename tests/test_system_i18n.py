from types import ModuleType
from unittest.mock import patch

from app.helper.locale import LocaleHelper
from app.testing import stub_modules


def _stub(name: str, **attrs) -> tuple:
    """构造带指定属性的占位模块，返回给 stub_modules 使用。"""
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return name, module


class _Dummy:
    """隔离 system endpoint 导入期重依赖的占位对象。"""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _FakeDoubanModule:
    """构造带中文名称的模块类，模拟真实 DoubanModule。"""

    @staticmethod
    def get_name() -> str:
        """获取模块中文名称"""
        return "豆瓣"


class _FakeModuleManager:
    """提供 system 模块接口测试所需的最小模块管理器。"""

    def get_modules(self) -> dict:
        """返回模块字典"""
        return {"DoubanModule": _FakeDoubanModule}

    def test(self, moduleid: str) -> tuple[bool, str]:
        """返回模块测试结果"""
        return False, "模块不支持测试"


_STUB_MODULES = dict([
    _stub("pillow_avif"),
    _stub("aiofiles"),
    _stub("psutil"),
    _stub("app.helper.sites", SitesHelper=_Dummy),
    _stub("app.chain.media", MediaChain=_Dummy),
    _stub("app.chain.mediaserver", MediaServerChain=_Dummy),
    _stub("app.chain.search", SearchChain=_Dummy),
    _stub("app.chain.system", SystemChain=_Dummy),
    _stub("app.core.event", eventmanager=_Dummy(), Event=_Dummy, EventManager=_Dummy),
    _stub("app.core.metainfo", MetaInfo=_Dummy),
    _stub("app.core.module", ModuleManager=_Dummy),
    _stub("app.core.security", verify_apitoken=_Dummy, verify_resource_token=_Dummy, verify_token=_Dummy),
    _stub("app.db.models", User=_Dummy),
    _stub("app.db.systemconfig_oper", SystemConfigOper=_Dummy),
    _stub("app.db.user_oper", get_current_active_superuser=_Dummy,
          get_current_active_superuser_async=_Dummy, get_current_active_user_async=_Dummy),
    _stub("app.helper.image", ImageHelper=_Dummy),
    _stub("app.helper.mediaserver", MediaServerHelper=_Dummy),
    _stub("app.helper.message", MessageHelper=_Dummy),
    _stub("app.helper.progress", ProgressHelper=_Dummy),
    _stub("app.helper.rule", RuleHelper=_Dummy),
    _stub("app.helper.server", MoviePilotServerHelper=_Dummy),
    _stub("app.helper.system", SystemHelper=_Dummy),
    _stub("app.log", logger=_Dummy(), log_settings=_Dummy(),
          LogConfigModel=type("LogConfigModel", (), {})),
    _stub("app.scheduler", Scheduler=_Dummy),
    _stub("app.utils.crypto", HashUtils=_Dummy),
    _stub("app.utils.http", RequestUtils=_Dummy, AsyncRequestUtils=_Dummy),
    _stub("version", APP_VERSION="test"),
])


with stub_modules(_STUB_MODULES):
    from app.api.endpoints import system as system_endpoint


def test_system_modulelist_keeps_chinese_name_and_adds_i18n_name():
    """模块列表接口应保留旧中文字段，并提供前端可用的多语言字段。"""
    token = LocaleHelper.set_current_locale("en-US")
    with patch.object(system_endpoint, "ModuleManager", return_value=_FakeModuleManager()):
        try:
            response = system_endpoint.modulelist(_="token")
        finally:
            LocaleHelper.reset_current_locale(token)

    module = response.data["modules"][0]
    assert module["id"] == "DoubanModule"
    assert module["name"] == "豆瓣"
    assert module["name_i18n"] == "Douban"
    assert module["name_key"] == "system.modules.DoubanModule.name"


def test_system_moduletest_keeps_chinese_message_and_adds_i18n_message():
    """模块测试接口应保留旧中文 message，并在顶层提供多语言 message_i18n。"""
    token = LocaleHelper.set_current_locale("en-US")
    with patch.object(system_endpoint, "ModuleManager", return_value=_FakeModuleManager()):
        try:
            response = system_endpoint.moduletest("DoubanModule", _="token")
        finally:
            LocaleHelper.reset_current_locale(token)

    assert response.success is False
    assert response.message == "模块不支持测试"
    assert response.message_i18n == "Module does not support testing"
