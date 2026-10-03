"""Fanart 模块缓存清理生命周期测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.modules.fanart import FanartModule
from app.runtime.tasks import TaskRegistry


def test_fanart_clear_cache_registers_async_cleanup_in_running_loop() -> None:
    """同步清缓存入口应复用宿主 owner，并保持立即返回的兼容约定。"""

    async def scenario() -> None:
        """验证异步缓存清理完成前始终由宿主登记器持有。"""
        registry = TaskRegistry()
        release = asyncio.Event()

        async def clear_async_cache() -> None:
            """等待测试释放，以便观察登记中的清理任务。"""
            await release.wait()

        sync_cache = SimpleNamespace(cache_clear=Mock())
        async_cache = SimpleNamespace(
            cache_clear=Mock(side_effect=lambda: clear_async_cache())
        )
        module = FanartModule()
        with (
            patch.object(
                FanartModule,
                "_FanartModule__request_fanart",
                sync_cache,
            ),
            patch.object(
                FanartModule,
                "_FanartModule__async_request_fanart",
                async_cache,
            ),
            patch("app.modules.fanart.get_task_registry", return_value=registry),
        ):
            assert module.clear_cache() is None
            assert [record.owner for record in registry.records] == [
                "module.fanart.cache_clear"
            ]

            release.set()
            await registry.records[0].task
            await asyncio.sleep(0)

        sync_cache.cache_clear.assert_called_once_with()
        async_cache.cache_clear.assert_called_once_with()
        assert registry.records == ()

    asyncio.run(scenario())


class _FakeFanartResponse:
    """模拟成功的 Fanart HTTP 响应。"""

    def json(self) -> dict:
        """返回最小图集载荷。"""
        return {"name": "fanart"}


def test_fanart_cache_key_follows_api_key(monkeypatch) -> None:
    """同一媒体在 API Key 不变时命中缓存，换 Key 后重新请求而不依赖清空缓存。"""
    from app.domain.context import MediaInfo
    from app.runtime.config import settings
    from app.schemas.types import MediaType

    requested_urls: list[str] = []

    class _FakeRequestUtils:
        """记录请求地址的同步 HTTP 替身。"""

        def __init__(self, **_kwargs) -> None:
            pass

        def get_res(self, url: str, **_kwargs) -> _FakeFanartResponse:
            requested_urls.append(url)
            return _FakeFanartResponse()

    monkeypatch.setattr("app.modules.fanart.RequestUtils", _FakeRequestUtils)
    monkeypatch.setattr(settings, "FANART_ENABLE", True)
    monkeypatch.setattr(settings, "FANART_API_KEY", "key-a")
    module = FanartModule()
    # 使用测试专属 ID，避免与其他用例共享进程内缓存条目。
    mediainfo = MediaInfo(type=MediaType.MOVIE, tmdb_id=987654321, title="Fanart Cache Key")

    module._FanartModule__obtain_fanart_images(mediainfo=mediainfo)
    module._FanartModule__obtain_fanart_images(mediainfo=mediainfo)
    monkeypatch.setattr(settings, "FANART_API_KEY", "key-b")
    module._FanartModule__obtain_fanart_images(mediainfo=mediainfo)

    assert len(requested_urls) == 2
    assert requested_urls[0].endswith("api_key=key-a")
    assert requested_urls[1].endswith("api_key=key-b")


def test_fanart_init_module_keeps_request_cache() -> None:
    """启动和重载不再清空请求缓存，Redis 缓存得以跨重启保留。"""
    module = FanartModule()
    with patch.object(FanartModule, "clear_cache") as clear_cache:
        module.init_module()

    clear_cache.assert_not_called()
