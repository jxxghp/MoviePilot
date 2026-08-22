"""插件连通性自检契约测试：三态语义、异常吞掉与返回值形状校验。"""

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.sdk.extension as extension_module
from app.sdk.extension import _PluginBase
from app.runtime.extensions.contract.extension import supports_extension_hook
from app.runtime.extensions.projection import plugin as projection_module
from app.runtime.extensions.projection.plugin import PluginExtension


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
        # 长度与第二元素都合契约，只有首元素不是布尔：真值也不得被当作可连通
        (1, "ok"),
        ("yes", ""),
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


def _base_method_definition_lines() -> dict:
    """按源码统计 `_PluginBase` 类体内每个方法名各被定义在哪些行。

    类对象只保留最后一次定义，重复定义在运行期查不出来，因此按源码的抽象语法树统计。

    :return: 方法名到其全部定义行号列表的映射
    """
    source = Path(extension_module.__file__).read_text(encoding="utf-8")
    definitions: dict = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef) or node.name != "_PluginBase":
            continue
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions.setdefault(member.name, []).append(member.lineno)
    return definitions


def test_base_plugin_defines_each_method_only_once():
    """`_PluginBase` 类体内不得重复定义同名方法。

    Python 后定义覆盖先定义，重复定义里靠前的那一份是永不生效的死代码：它既不会
    报错，也无法从类对象上察觉，只会让读钩子清单的人以为自己看的是生效的那一份。
    """
    duplicated = {
        name: lines
        for name, lines in _base_method_definition_lines().items()
        if len(lines) > 1
    }

    assert duplicated == {}


@pytest.mark.parametrize("hook", ["test", "get_media_source"])
def test_base_plugin_keeps_one_definition_of_previously_duplicated_hooks(hook):
    """曾各被定义两次的两个钩子在类体内只剩一份定义，且仍然挂在基类上。"""
    assert _base_method_definition_lines().get(hook) == [
        getattr(_PluginBase, hook).__code__.co_firstlineno
    ]
