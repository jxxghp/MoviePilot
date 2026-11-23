"""
公共服务基类的可空配置与实例化边界。
"""

import pytest

from app.modules import ServiceBase, _ModuleBase
from app.schemas.system import NotificationConf


class _Service(ServiceBase[object, NotificationConf]):
    """
    通过内存配置测试公共服务逻辑，不建立外部连接。
    """

    def __init__(self, configs):
        """
        保留初始化前的默认状态并记录可供加载的配置。
        """
        super().__init__()
        self.available_configs = configs

    def get_configs(self):
        """
        返回调用方指定的配置。
        """
        return self.available_configs


def test_default_name_before_initialization_is_empty():
    """
    初始化前不存在默认配置，应与 get_config/get_instance 一样返回 None。
    """
    service = _Service({})
    assert service.get_default_config_name() is None
    assert service.get_config() is None
    assert service.get_instance() is None


@pytest.mark.parametrize("name", [None, "named"])
def test_class_factory_preserves_name_override_and_config_identity(name):
    """
    配置名称可空，构造参数中的 name 仍按原字典展开顺序覆盖配置名。
    """
    token = object()
    config = NotificationConf(name=name, config={"name": "override", "token": token})
    service = _Service({name: config})

    class Client:
        """
        记录收到的原始配置参数。
        """

        def __init__(self, **kwargs):
            """
            保留构造参数供身份断言使用。
            """
            self.kwargs = kwargs

    service.init_service("client", Client)
    client = service.get_instances()[name]
    assert client.kwargs == {"name": "override", "token": token}
    assert client.kwargs["token"] is token
    assert service.get_configs()[name] is config


@pytest.mark.parametrize("name", [None, "named"])
def test_callable_factory_receives_original_configuration(name):
    """
    函数工厂仍接收配置对象本身，包括名称与 config 为空的情况。
    """
    config = NotificationConf(name=name, config=None)
    service = _Service({name: config})
    calls = []
    instance = object()

    def factory(conf):
        """
        记录原始配置，避免被转换为构造参数。
        """
        calls.append(conf)
        return instance

    service.init_service("client", factory)
    assert calls == [config]
    assert calls[0] is config
    assert service.get_instances()[name] is instance


def test_class_factory_rejects_none_config_without_constructing_client():
    """
    空配置继续触发原字典解包的 TypeError，不能误创建默认连接。
    """
    service = _Service({"named": NotificationConf(name="named", config=None)})
    calls = []

    class Client:
        """
        在构造被意外调用时留下可检查的证据。
        """

        def __init__(self, **kwargs):
            """
            记录客户端的实际创建。
            """
            calls.append(kwargs)

    with pytest.raises(TypeError, match="'NoneType' object is not a mapping"):
        service.init_service("client", Client)
    assert calls == []
    assert service.get_instances() == {}


def test_missing_module_metadata_retains_implicit_none():
    """
    已有模块可以只实现原抽象方法；未提供的元数据仍为 None。
    """

    class Module(_ModuleBase):
        """
        仅实现生命周期抽象方法的兼容模块。
        """

        def init_module(self):
            """
            无需初始化外部资源。
            """

        def init_setting(self):
            """
            没有模块设置开关。
            """

        def stop(self):
            """
            没有需要释放的外部资源。
            """

        def test(self):
            """
            没有需要连接的外部服务。
            """

    module = Module()
    assert module.get_name() is None
    assert module.get_reload_name() is None
    assert module.get_type() is None
    assert module.get_subtype() is None
    assert module.get_priority() is None


def test_init_service_swaps_instances_atomically_after_rebuild():
    """
    重新初始化服务实例时应先在临时字典中完成构造，再整体替换现有实例映射。
    """
    existing = object()
    service = _Service({"first": NotificationConf(name="first", config={})})
    service.init_service("client", lambda _conf: existing)
    assert service.get_instances() == {"first": existing}

    observed_during_rebuild = []
    replacement = object()
    service.available_configs = {
        "first": NotificationConf(name="first", config={}),
    }

    def factory(_conf):
        """
        记录构造期间外部可见的旧实例映射，确认未被提前清空。
        """
        observed_during_rebuild.append(dict(service.get_instances()))
        return replacement

    service.init_service("client", factory)
    assert observed_during_rebuild == [{"first": existing}]
    assert service.get_instances() == {"first": replacement}
    assert service.get_init_errors() == {}


def test_init_service_isolates_single_config_failure_and_logs_error(monkeypatch):
    """
    多条配置中单条实例化失败时应隔离故障并保留其余合法实例，且记录错误供健康检查与排障。
    """
    from app.modules import logger as module_logger

    logged_errors = []
    monkeypatch.setattr(module_logger, "error", logged_errors.append)

    good_instance = object()
    service = _Service(
        {
            "broken": NotificationConf(name="broken", config={"bad": True}),
            "healthy": NotificationConf(name="healthy", config={"ok": True}),
        }
    )

    def factory(conf):
        """
        仅对故障配置抛错，其余配置正常返回实例。
        """
        if conf.name == "broken":
            raise RuntimeError("连接超时")
        return good_instance

    service.init_service("qbittorrent", factory)

    assert service.get_instances() == {"healthy": good_instance}
    assert service.get_init_errors() == {"broken": "连接超时"}
    assert any("broken" in message and "连接超时" in message for message in logged_errors)

    service.available_configs = {
        "healthy": NotificationConf(name="healthy", config={"ok": True}),
    }
    service.init_service("qbittorrent", factory)
    assert service.get_init_errors() == {}


def test_init_service_raises_first_error_when_all_configs_fail():
    """
    所有已启用配置均实例化失败时应清空实例、保留全部错误并抛出首个异常。
    """
    service = _Service(
        {
            "first": NotificationConf(name="first", config={}),
            "second": NotificationConf(name="second", config={}),
        }
    )

    def factory(conf):
        """
        按配置名返回不同异常，用于验证抛出首个异常且完整记录错误。
        """
        raise ValueError(f"bad:{conf.name}")

    with pytest.raises(ValueError, match="bad:first"):
        service.init_service("qbittorrent", factory)

    assert service.get_instances() == {}
    assert service.get_init_errors() == {
        "first": "bad:first",
        "second": "bad:second",
    }


def test_service_module_bases_report_init_errors_before_healthy_siblings():
    """
    下载器、媒体服务器与消息渠道基类的 test() 在存在初始化失败实例时不得因其余实例正常而返回成功。
    """
    from unittest.mock import Mock

    from app.modules._base.downloader import _DownloaderModuleBase
    from app.modules._base.mediaserver import _MediaServerModuleBase
    from app.modules._base.notification import _MessageChannelModuleBase

    class _DummyDownloader(_DownloaderModuleBase[object]):
        """
        仅用于验证初始化错误暴露的下载器基类桩。
        """

        def init_module(self):
            """
            无外部初始化逻辑。
            """

        def init_setting(self):
            """
            无模块设置。
            """

        def stop(self):
            """
            无外部资源需要释放。
            """

        def get_name(self):
            """
            返回固定模块名。
            """
            return "Qbittorrent"

    class _DummyMediaServer(_MediaServerModuleBase):
        """
        仅用于验证初始化错误暴露的媒体服务器基类桩。
        """

        def init_module(self):
            """
            无外部初始化逻辑。
            """

        def init_setting(self):
            """
            无模块设置。
            """

        def stop(self):
            """
            无外部资源需要释放。
            """

        def get_name(self):
            """
            返回固定模块名。
            """
            return "Emby"

    class _DummyNotification(_MessageChannelModuleBase):
        """
        仅用于验证初始化错误暴露的消息渠道基类桩。
        """

        def init_module(self):
            """
            无外部初始化逻辑。
            """

        def init_setting(self):
            """
            无模块设置。
            """

        def stop(self):
            """
            无外部资源需要释放。
            """

        def get_name(self):
            """
            返回固定模块名。
            """
            return "Telegram"

    healthy_downloader = Mock()
    healthy_downloader.is_inactive.return_value = False
    healthy_downloader.transfer_info.return_value = object()
    downloader_mod = _DummyDownloader()
    downloader_mod._instances = {"ok-qb": healthy_downloader}
    downloader_mod._init_errors = {"bad-qb": "认证失败"}

    healthy_server = Mock()
    healthy_server.is_inactive.return_value = False
    healthy_server.get_user.return_value = {"id": "1"}
    mediaserver_mod = _DummyMediaServer()
    mediaserver_mod._instances = {"ok-emby": healthy_server}
    mediaserver_mod._init_errors = {"bad-emby": "地址无效"}

    healthy_channel = Mock()
    healthy_channel.get_state.return_value = True
    notification_mod = _DummyNotification()
    notification_mod._instances = {"ok-tg": healthy_channel}
    notification_mod._init_errors = {"bad-tg": "令牌无效"}

    dl_ok, dl_msg = downloader_mod.test()
    ms_ok, ms_msg = mediaserver_mod.test()
    nt_ok, nt_msg = notification_mod.test()

    assert dl_ok is False and "bad-qb" in dl_msg and "认证失败" in dl_msg
    assert ms_ok is False and "bad-emby" in ms_msg and "地址无效" in ms_msg
    assert nt_ok is False and "bad-tg" in nt_msg and "令牌无效" in nt_msg
