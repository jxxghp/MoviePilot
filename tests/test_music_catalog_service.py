"""音乐目录应用服务测试。"""

import asyncio
from types import SimpleNamespace

from app.application.music.catalog import MusicCatalogService
from app.domain.context import MusicInfo
from app.schemas.types import MediaSource


class _Source:
    """提供同步和异步搜索接口的音乐来源替身。"""

    def search_music(self, _meta, limit=20):
        """返回重复候选，验证归一化去重。"""
        return [
            MusicInfo(media_source=MediaSource.MusicBrainz, media_id="1", title="A"),
            MusicInfo(media_source=MediaSource.MusicBrainz, media_id="1", title="A"),
        ][:limit]

    async def async_search_music(self, _meta, limit=20):
        """返回异步候选。"""
        return self.search_music(_meta, limit)


def test_music_catalog_service_searches_and_deduplicates_sources():
    """同步和异步音乐搜索都应保留来源身份并去重。"""
    service = MusicCatalogService(
        source_resolver=lambda source: _Source() if source == MediaSource.MusicBrainz else None,
        warning=lambda _message: None,
    )

    assert len(service.search("artist title")) == 1
    assert len(asyncio.run(service.async_search("artist title"))) == 1


def test_music_catalog_service_isolates_failed_source():
    """一个来源失败不应阻断其它来源。"""
    errors = []

    class _Broken:
        def search_music(self, *_args, **_kwargs):
            """模拟来源错误。"""
            raise RuntimeError("broken")

    service = MusicCatalogService(
        source_resolver=lambda _source: _Broken(),
        warning=errors.append,
    )

    assert service.search("artist title") == []
    assert errors and "broken" in errors[0]
