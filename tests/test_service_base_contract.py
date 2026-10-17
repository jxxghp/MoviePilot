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
