"""
验证 Bangumi 人物资料归一化和运行配置快照。
"""

import asyncio
import inspect
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.modules.bangumi import BangumiModule
from app.runtime.config import settings


class _FakeBangumiApi:
    """
    测试用Bangumi API客户端。
    """

    def __init__(self, personinfo: dict):
        """
        初始化测试用人物详情数据。
        :param personinfo: 人物详情接口返回数据
        """
        self.personinfo = personinfo

    def person_detail(self, person_id: int) -> dict:
        """
        返回同步人物详情数据。
        :param person_id: 人物ID
        :return: 人物详情接口返回数据
        """
        return self.personinfo

    async def async_person_detail(self, person_id: int) -> dict:
        """
        返回异步人物详情数据。
        :param person_id: 人物ID
        :return: 人物详情接口返回数据
        """
        return self.personinfo


def test_bangumi_person_detail_normalizes_numeric_birthday():
    """
    Bangumi同步人物详情应兼容数字生日字段。
    """
    module = BangumiModule()
    module.bangumiapi = _FakeBangumiApi({
        "id": 1001,
        "name": "测试人物",
        "images": {"large": "https://example.com/person.jpg"},
        "summary": "测试简介",
        "birth_day": 22,
        "gender": 1,
    })

    person = module.bangumi_person_detail(1001)

    assert person.birthday == "22"


def test_async_bangumi_person_detail_normalizes_numeric_birthday():
    """
    Bangumi异步人物详情应兼容数字生日字段。
    """
    module = BangumiModule()
    module.bangumiapi = _FakeBangumiApi({
        "id": 1002,
        "name": "测试异步人物",
        "images": {"large": "https://example.com/async-person.jpg"},
        "summary": "测试异步简介",
        "birth_day": 19,
        "gender": 2,
    })

    person = asyncio.run(module.async_bangumi_person_detail(1002))

    assert person.birthday == "19"


def test_bangumi_test_uses_generation_snapshot_until_reload(monkeypatch):
    """
    长生命周期模块应在 init/reload 时换快照，而不是每次调用读取全局配置。
    """
    module = BangumiModule()
    monkeypatch.setattr(settings, "PROXY_HOST", "http://old-proxy")
    monkeypatch.setattr(settings, "BANGUMI_PROXY_ENABLE", True)
    monkeypatch.setattr(settings, "BANGUMI_API_DOMAIN", "https://old-bangumi.example/api")
    module.init_module()
    old_proxy = settings.PROXY
    monkeypatch.setattr(settings, "PROXY_HOST", "http://new-proxy")
    monkeypatch.setattr(settings, "BANGUMI_PROXY_ENABLE", True)
    monkeypatch.setattr(settings, "BANGUMI_API_DOMAIN", "https://new-bangumi.example/api")
    new_proxy = settings.PROXY

    with patch("app.modules.bangumi.RequestUtils") as request_utils:
        request_utils.return_value.get_res.return_value = MagicMock(status_code=200)
        module.test()
        assert request_utils.call_args.kwargs["proxies"] == old_proxy
        assert request_utils.return_value.get_res.call_args.args[0] == (
            "https://old-bangumi.example/api/"
        )

        module.on_config_changed()
        module.test()
        assert request_utils.call_args.kwargs["proxies"] == new_proxy
        assert request_utils.return_value.get_res.call_args.args[0] == (
            "https://new-bangumi.example/api/"
        )


def test_bangumi_stop_closes_client_without_clearing_cache() -> None:
    """
    模块停止只关闭客户端；缓存键已含 API 地址，不在重载或关闭时清空缓存。
    """
    module = BangumiModule.__new__(BangumiModule)
    module.bangumiapi = MagicMock()

    module.stop()

    module.bangumiapi.close.assert_called_once_with()
    module.bangumiapi.clear_cache.assert_not_called()


def test_bangumi_module_watches_and_passes_image_domain(monkeypatch):
    """
    模块应监听图片代理配置，并把图片地址与数据地址一起交给客户端。
    """
    module = BangumiModule()
    monkeypatch.setattr(settings, "BANGUMI_PROXY_ENABLE", True)
    monkeypatch.setattr(settings, "BANGUMI_API_DOMAIN", "https://bgma.example")
    monkeypatch.setattr(settings, "BANGUMI_IMAGE_DOMAIN", "bgmp.example/mirror/")

    with patch("app.modules.bangumi.BangumiApi") as api_class:
        module.init_module()

    assert api_class.call_args.kwargs["base_url"] == "https://bgma.example"
    assert api_class.call_args.kwargs["image_domain"] == "bgmp.example/mirror/"
    assert "BANGUMI_IMAGE_DOMAIN" in BangumiModule.CONFIG_WATCH

    manifest_path = Path(inspect.getsourcefile(BangumiModule)).parent / "capability.toml"
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["activation"]["watch"]) == set(BangumiModule.CONFIG_WATCH)

    monkeypatch.setattr(settings, "BANGUMI_PROXY_ENABLE", False)
    with patch("app.modules.bangumi.BangumiApi") as api_class:
        module.init_module()

    assert api_class.call_args.kwargs["image_domain"] == ""
