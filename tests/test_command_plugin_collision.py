"""插件命令重名先到者胜的行为契约测试。

多实例只是放大了这个既有缺陷：同一插件的多个实例、或不同插件都可能声明
同一个命令字符串，命令表必须保留登记顺序中第一个并对后续重复声明告警，
而不是静默覆盖。
"""

from types import SimpleNamespace

from app.command import Command


def _build_command(monkeypatch, plugin_commands: list) -> tuple:
    """构造只挂插件命令来源的命令中枢测试对象，返回 (实例, 捕获的告警列表)。"""
    warnings: list = []
    command_chain = object.__new__(Command)
    command_chain.pluginmanager = SimpleNamespace(
        get_plugin_commands=lambda: plugin_commands
    )
    monkeypatch.setattr(
        "app.command.logger", SimpleNamespace(warning=lambda msg: warnings.append(msg))
    )
    return command_chain, warnings


def test_duplicate_command_from_sibling_instance_keeps_first_registration(monkeypatch):
    """同一插件两个实例声明同一命令字符串时，先登记的实例生效，后者被跳过并告警。"""
    command_chain, warnings = _build_command(
        monkeypatch,
        [
            {"cmd": "/sync", "desc": "同步", "pid": "DemoPlugin"},
            {"cmd": "/sync", "desc": "同步-第二实例", "pid": "DemoPlugin@second"},
        ],
    )

    result = command_chain._Command__build_plugin_commands()

    assert list(result) == ["/sync"]
    assert result["/sync"]["pid"] == "DemoPlugin"
    assert len(warnings) == 1
    assert "DemoPlugin" in warnings[0] and "DemoPlugin@second" in warnings[0]


def test_non_colliding_commands_are_all_registered(monkeypatch):
    """互不重名的命令都被正常登记，不触发告警。"""
    command_chain, warnings = _build_command(
        monkeypatch,
        [
            {"cmd": "/foo", "desc": "foo", "pid": "DemoPlugin"},
            {"cmd": "/bar", "desc": "bar", "pid": "DemoPlugin@second"},
        ],
    )

    result = command_chain._Command__build_plugin_commands()

    assert set(result) == {"/foo", "/bar"}
    assert warnings == []
