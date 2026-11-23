"""
默认服务配置的选取必须尊重用户的显式标记。

配置的先后来自读取顺序，用户既看不见也控制不了。按顺序取第一个意味着：删掉一个无关配置，
「默认下载器」就会静默改指另一个，而下载任务的归属记录已经按旧的默认写进了历史。
"""

from typing import Dict, Optional

import pytest

from app.modules import ServiceBase, _DownloaderBase
from app.runtime.extensions.service import ServiceConfigHelper
from app.schemas.system import DownloaderConf


class _StubService(ServiceBase[object, DownloaderConf]):
    """
    只提供配置字典的服务基类桩，不连接任何真实后端。
    """

    def __init__(self, configs: Dict[str, DownloaderConf]) -> None:
        """
        记录待测配置，跳过真实服务实例化。

        :param configs: 名称到配置的映射，顺序即读取顺序
        """
        self._configs = configs
        self._instances = {}
        self._service_name = "qbittorrent"

    def get_configs(self) -> Dict[str, DownloaderConf]:
        """
        返回构造时传入的配置字典。

        :return: 名称到配置的映射
        """
        return self._configs


def _conf(name: str, *, default: bool = False, enabled: bool = True) -> DownloaderConf:
    """
    构造一份下载器配置。

    :param name: 配置名称
    :param default: 是否标记为默认
    :param enabled: 是否启用该下载器
    :return: 下载器配置
    """
    return DownloaderConf(
        name=name,
        type="qbittorrent",
        enabled=enabled,
        default=default,
        config={},
    )


def test_no_config_has_no_default() -> None:
    """
    没有任何配置时不应凭空给出默认名称。
    """
    service = _StubService({})

    assert service.get_default_config_name() is None


def test_single_config_is_the_default() -> None:
    """
    只有一份配置时它就是默认，无需用户额外标记。
    """
    service = _StubService({"only": _conf("only")})

    assert service.get_default_config_name() == "only"


def test_marked_config_wins_over_order() -> None:
    """
    标记为默认的配置优先，即使它不排在最前。
    """
    service = _StubService(
        {
            "first": _conf("first"),
            "second": _conf("second", default=True),
        }
    )

    assert service.get_default_config_name() == "second"


def test_marked_config_survives_reordering() -> None:
    """
    读取顺序变化不得改变已标记的默认配置。

    这是本修复要守住的性质：顺序是实现细节，标记才是用户的意思。
    """
    marked = _conf("marked", default=True)
    plain = _conf("plain")

    forward = _StubService({"marked": marked, "plain": plain})
    backward = _StubService({"plain": plain, "marked": marked})

    assert forward.get_default_config_name() == "marked"
    assert backward.get_default_config_name() == "marked"


def test_unmarked_configs_fall_back_to_first() -> None:
    """
    一个标记都没有时保持原有行为，不改变既有部署的默认目标。
    """
    service = _StubService(
        {
            "alpha": _conf("alpha"),
            "beta": _conf("beta"),
        }
    )

    assert service.get_default_config_name() == "alpha"


@pytest.mark.parametrize("name", ["alpha", "beta"])
def test_get_config_without_name_follows_the_default(name: str) -> None:
    """
    不指定名称时取到的必须是默认配置本身。
    """
    service = _StubService(
        {
            "alpha": _conf("alpha", default=name == "alpha"),
            "beta": _conf("beta", default=name == "beta"),
        }
    )

    resolved: Optional[DownloaderConf] = service.get_config()

    assert resolved is not None
    assert resolved.name == name


def _downloader_service(
    monkeypatch: pytest.MonkeyPatch,
    configs: list[DownloaderConf],
) -> _DownloaderBase[object]:
    """
    构造隔离了全局配置来源的下载器基类实例，只注入已启用配置的运行实例。

    :param monkeypatch: pytest 补丁夹具
    :param configs: 模拟的完整下载器配置列表（含已禁用项）
    :return: 可调用默认名称与实例解析的下载器基类对象
    """
    monkeypatch.setattr(ServiceConfigHelper, "get_downloader_configs", staticmethod(lambda: configs))
    service: _DownloaderBase[object] = _DownloaderBase()
    service._service_name = "qbittorrent"
    service._configs = {conf.name: conf for conf in configs if conf.enabled}
    service._instances = {conf.name: f"instance:{conf.name}" for conf in configs if conf.enabled}
    return service


def test_downloader_default_skips_disabled_default_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    已禁用的下载器即使仍带有 default 标记，也不得盖过已启用的默认下载器。
    """
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-default", default=True, enabled=False),
            _conf("backup-qb", enabled=True),
            _conf("active-qb", default=True, enabled=True),
        ],
    )

    assert service.get_default_config_name() == "active-qb"
    assert service.get_instance() == "instance:active-qb"


def test_downloader_default_falls_back_to_first_enabled_when_only_disabled_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    仅已禁用的下载器带有 default 标记且其余已启用项均未标记时，默认下载器应落到首个已启用配置。
    """
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-default", default=True, enabled=False),
            _conf("first-enabled", enabled=True),
            _conf("second-enabled", enabled=True),
        ],
    )

    assert service.get_default_config_name() == "first-enabled"
    assert service.get_instance() == "instance:first-enabled"


def test_downloader_default_skips_disabled_first_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    没有默认标记且首个配置已禁用时，默认下载器应落到首个已启用配置。
    """
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-first", enabled=False),
            _conf("second-enabled", enabled=True),
        ],
    )

    assert service.get_default_config_name() == "second-enabled"
    assert service.get_instance() == "instance:second-enabled"


def test_downloader_default_returns_none_when_all_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    所有下载器都已禁用时，默认配置名称与默认实例均应返回 None。
    """
    service = _downloader_service(
        monkeypatch,
        [
            _conf("disabled-only", default=True, enabled=False),
        ],
    )

    assert service.get_default_config_name() is None
    assert service.get_instance() is None
