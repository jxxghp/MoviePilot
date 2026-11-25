"""
过滤器模块健康检查不应把“无需探测”报成“未启用”。

复现 #7354：运行中的 FilterModule 因 test() 返回 None，被
ModuleManager.test() 按 falsy 结果处理成 (False, "")，健康检查接口
据此返回 success=false，前端显示“未启用”。
"""

import pytest

from app.api.endpoints import module as system_endpoint
from app.modules.filter import FilterModule
from app.runtime.extensions.module.manager import ModuleManager


def test_filter_module_reports_available_without_probing() -> None:
    """
    运行中的过滤器不依赖外部服务、无需探测，健康检查应返回成功。
    """
    manager = ModuleManager()
    assert manager.get_running_module("FilterModule") is not None
    assert manager.test("FilterModule") == (True, "模块无需探测")


def test_filter_module_test_returns_success_directly() -> None:
    """
    过滤器自检应直接返回成功结果，而不是表示“无结论”的 None。
    """
    assert FilterModule().test() == (True, "模块无需探测")


def test_subtitle_module_reports_available_without_probing() -> None:
    """
    字幕模块与过滤器同属“不提供独立检查”的已运行模块，应同样返回成功。
    """
    from app.modules.subtitle import SubtitleModule

    manager = ModuleManager()
    assert manager.get_running_module("SubtitleModule") is not None
    assert SubtitleModule().test() == (True, "模块无需探测")
    assert manager.test("SubtitleModule") == (True, "模块无需探测")


def test_unselected_backend_module_keeps_old_contract() -> None:
    """
    未选中对应后端的已运行模块（如未使用 PostgreSQL）仍保持旧合同。

    这正是修复放在模块层而不是管理器层的原因：管理器无法区分“无需探测”
    与“后端未选用”两种 None，统一改成成功会把未选用的后端误报为可用。
    """
    manager = ModuleManager()
    assert manager.get_running_module("PostgreSQLModule") is not None
    assert manager.test("PostgreSQLModule") == (False, "")


def test_moduletest_endpoint_reports_filter_module_available() -> None:
    """
    健康检查接口对 FilterModule 应返回 success=true。
    """
    response = system_endpoint.moduletest("FilterModule", _="token")
    assert response.success is True
    assert response.message == "模块无需探测"


def test_moduletest_endpoint_localizes_no_probing_message() -> None:
    """
    英文请求下健康检查接口应返回翻译后的无需探测说明。
    """
    from app.runtime.localization import LocaleHelper

    token = LocaleHelper.set_current_locale("en-US")
    try:
        response = system_endpoint.moduletest("FilterModule", _="token")
    finally:
        LocaleHelper.reset_current_locale(token)
    assert response.success is True
    assert response.message == "Module does not require probing"


def test_not_running_module_keeps_old_contract() -> None:
    """
    未运行的模块仍保持旧合同，返回 (False, "")。
    """
    manager = ModuleManager()
    assert manager.get_running_module("NavidromeModule") is None
    assert manager.test("NavidromeModule") == (False, "")


def test_real_failure_result_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    模块 test() 真实返回失败时，管理器应原样透传失败结果。
    """
    manager = ModuleManager()
    monkeypatch.setattr(FilterModule, "test", lambda self: (False, "连接失败"))
    assert manager.test("FilterModule") == (False, "连接失败")


def test_real_success_result_still_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    模块 test() 真实返回成功时，管理器应原样透传成功结果。
    """
    manager = ModuleManager()
    monkeypatch.setattr(FilterModule, "test", lambda self: (True, ""))
    assert manager.test("FilterModule") == (True, "")
