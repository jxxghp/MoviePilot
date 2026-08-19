"""插件连通性自检契约测试：三态语义、异常吞掉与返回值形状校验。"""

from unittest.mock import Mock

import pytest

from app.plugins import _PluginBase
from app.runtime.extensions.contract import supports_extension_hook
from app.runtime.extensions.plugin import projection as projection_module
from app.runtime.extensions.plugin.projection import PluginExtension


class _BasePluginStub(_PluginBase):
    """继承 `_PluginBase` 且只实现必需抽象方法的最小插件桩，不牵入宿主依赖。"""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_api(self):
        """声明插件 API，测试桩无 API。"""
        return []

    def get_form(self):
        """声明插件配置页面，测试桩无配置页面。"""
        return None, {}

    def get_page(self):
        """声明插件详情页面，测试桩无详情页面。"""
        return None

    def stop_service(self) -> None:
        """停止插件后台服务，测试桩无后台服务。"""


class _SuccessTestPlugin(_BasePluginStub):
    """自检返回可连通结果的插件桩。"""

    def test(self):
        """返回可连通结果。"""
        return True, ""


class _FailureTestPlugin(_BasePluginStub):
    """自检返回不可连通结果的插件桩。"""

    def test(self):
        """返回不可连通结果与失败原因。"""
        return False, "连不上"


class _RaisingTestPlugin(_BasePluginStub):
    """自检过程中抛出异常的插件桩。"""

    def test(self):
        """模拟自检时下游服务异常。"""
        raise RuntimeError("下游服务异常")


class _InvalidShapeTestPlugin(_BasePluginStub):
    """自检返回值形状不合契约的插件桩。"""

    def __init__(self, value, enabled: bool = True) -> None:
        super().__init__(enabled)
        self._value = value

    def test(self):
        """返回不合 `(bool, str)` 契约的值。"""
        return self._value


def test_self_test_returns_none_when_hook_not_implemented():
    """插件未实现 `test` 钩子时，`self_test()` 返回 ``None``。"""
    extension = PluginExtension(_BasePluginStub(), "BasePluginStub")

    assert extension.self_test() is None


def test_self_test_returns_success_result_unchanged():
    """插件自检返回可连通结果时，`self_test()` 原样返回。"""
    extension = PluginExtension(_SuccessTestPlugin(), "SuccessTestPlugin")

    assert extension.self_test() == (True, "")


def test_self_test_returns_failure_result_unchanged():
    """插件自检返回不可连通结果时，`self_test()` 原样返回。"""
    extension = PluginExtension(_FailureTestPlugin(), "FailureTestPlugin")

    assert extension.self_test() == (False, "连不上")


def test_self_test_swallows_exception_and_reports_failure(monkeypatch):
    """插件自检抛异常时返回失败结果，异常不冒泡且被记录。"""
    error_log = Mock()
    monkeypatch.setattr(projection_module.default_logger, "error", error_log)
    extension = PluginExtension(_RaisingTestPlugin(), "RaisingTestPlugin")

    success, message = extension.self_test()

    assert success is False
    assert "下游服务异常" in message
    error_log.assert_called_once()


@pytest.mark.parametrize(
    "invalid_value",
    [
        True,
        (True, "ok", "extra"),
        "connected",
        (True,),
    ],
)
def test_self_test_rejects_results_with_invalid_shape(monkeypatch, invalid_value):
    """自检返回值形状不合 `(bool, str)` 契约时返回 ``None`` 且记录告警，不做宽容转换。"""
    warning_log = Mock()
    monkeypatch.setattr(projection_module.default_logger, "warning", warning_log)
    extension = PluginExtension(
        _InvalidShapeTestPlugin(invalid_value), "InvalidShapeTestPlugin"
    )

    assert extension.self_test() is None
    warning_log.assert_called_once()


def test_base_plugin_empty_test_implementation_is_not_treated_as_implemented():
    """`_PluginBase` 自身的空 `test` 实现不应被判定为「已实现」。

    `_PluginBase` 含未实现的抽象方法，无法直接实例化，因此在类本身与只实现
    必需抽象方法、未覆盖 `test` 的最小子类实例上分别校验。
    """
    assert supports_extension_hook(_PluginBase, "test") is False

    stub_extension = PluginExtension(_BasePluginStub(), "BasePluginStub")
    assert stub_extension.supports_hook("test") is False
