"""插件缺失依赖安装结果测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.runtime.extensions.plugin_manager import PluginManager


def _configure_installer(monkeypatch, installer: SimpleNamespace) -> None:
    """把依赖安装器替身装配进插件外部系统端口。"""
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_system",
        lambda: SimpleNamespace(dependency=installer),
    )


def test_install_missing_skips_installer_when_environment_is_satisfied(
    monkeypatch,
) -> None:
    """依赖均满足时只执行轻量检查，不进入包安装链。"""
    installer = SimpleNamespace(
        find_missing=MagicMock(return_value=[]),
        install=MagicMock(),
    )
    _configure_installer(monkeypatch, installer)

    result = PluginManager.install_plugin_missing_dependencies_with_status()

    assert result.success is True
    assert result.missing == []
    installer.install.assert_not_called()


def test_install_missing_reports_installer_failure(monkeypatch) -> None:
    """安装器失败时结果必须如实标记，供启动流程降级激活已就绪插件。"""
    installer = SimpleNamespace(
        find_missing=MagicMock(return_value=["demo>=1"]),
        install=MagicMock(return_value=(False, "boom")),
    )
    _configure_installer(monkeypatch, installer)

    result = PluginManager.install_plugin_missing_dependencies_with_status()

    assert result.success is False
    assert result.missing == ["demo>=1"]


def test_install_missing_preserves_list_return_contract(monkeypatch) -> None:
    """旧入口继续返回缺失项列表，供现有调用方按真值判断。"""
    installer = SimpleNamespace(
        find_missing=MagicMock(return_value=["demo>=1"]),
        install=MagicMock(return_value=(True, "")),
    )
    _configure_installer(monkeypatch, installer)

    assert PluginManager.install_plugin_missing_dependencies() == ["demo>=1"]
    installer.install.assert_called_once_with(["demo>=1"])


def test_classify_plugins_maps_installer_tuple_to_named_fields(monkeypatch) -> None:
    """分类结果按字段命名回传，避免调用方按位置解构安装器元组。"""
    installer = SimpleNamespace(
        classify_plugins=MagicMock(
            return_value=(["Ready"], ["DependencyPending"], ["SourceMissing"])
        )
    )
    _configure_installer(monkeypatch, installer)

    classification = PluginManager.classify_plugins()

    assert classification.ready == ("Ready",)
    assert classification.missing_dependencies == ("DependencyPending",)
    assert classification.missing_source == ("SourceMissing",)
