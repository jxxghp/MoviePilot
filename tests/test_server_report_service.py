from types import SimpleNamespace
from unittest.mock import Mock

from app.application.server.report import ServerReportService


def _service(**overrides) -> ServerReportService:
    """构造不依赖数据库和网络的中心服务上报用例。"""
    defaults = {
        "config_reader": Mock(return_value=None),
        "config_writer": Mock(),
        "installed_plugins_provider": Mock(return_value=[]),
        "subscribes_provider": Mock(return_value=[]),
        "plugin_report_sender": Mock(
            return_value=SimpleNamespace(status_code=200)
        ),
        "async_plugin_report_sender": Mock(),
        "subscribe_report_sender": Mock(
            return_value=SimpleNamespace(status_code=200)
        ),
        "repo_url_sanitizer": lambda value: value,
    }
    defaults.update(overrides)
    return ServerReportService(**defaults)


def test_subscribe_report_uses_fake_local_reader_and_transport():
    """订阅存量上报只依赖注入的数据读取和发送端口。"""
    sender = Mock(return_value=SimpleNamespace(status_code=200))
    subscribe = SimpleNamespace(to_dict=lambda: {
        "name": "Demo",
        "type": "电影",
        "media_source": "themoviedb",
        "media_id": "123",
        "username": "private",
    })
    service = _service(
        subscribes_provider=Mock(return_value=[subscribe]),
        subscribe_report_sender=sender,
    )

    assert service.report_subscribes(enabled=True) is True
    sender.assert_called_once_with([{
        "name": "Demo",
        "type": "电影",
        "media_source": "themoviedb",
        "media_id": "123",
    }])


def test_initial_report_marker_is_written_only_after_success():
    """首次上报失败时不得提前写完成标记。"""
    writer = Mock()
    service = _service(config_writer=writer)

    service.init_report(
        enabled=True,
        state_key="report",
        reporter=Mock(return_value=False),
    )

    writer.assert_not_called()


def test_plugin_report_sanitizes_explicit_sources_before_transport():
    """插件统计载荷在进入传输适配器前完成来源脱敏。"""
    sender = Mock(return_value=SimpleNamespace(status_code=200))
    service = _service(
        plugin_report_sender=sender,
        repo_url_sanitizer=lambda value: "local://Demo" if value else value,
    )

    assert service.report_plugins(
        enabled=True,
        items=[("Demo", "local://Demo?path=/private/repo")],
    ) is True
    sender.assert_called_once_with([{
        "plugin_id": "Demo",
        "repo_url": "local://Demo",
    }])
