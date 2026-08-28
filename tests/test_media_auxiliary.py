"""多媒体来源附加信息 provider 的单元测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.chain.search import SearchChain
from app.domain.context import MediaInfo
from app.modules._base.media_auxiliary import MediaAuxiliaryProviderMixin
from app.schemas.types import MediaSource, MediaType


class _FakeAuxiliaryProvider(MediaAuxiliaryProviderMixin):
    """记录通用 mixin 下传给现有识别接口的参数。"""

    auxiliary_media_source = MediaSource.AniList

    def __init__(self) -> None:
        """初始化同步和异步调用记录。"""
        self.sync_kwargs = None
        self.async_kwargs = None

    def recognize_media(self, **kwargs) -> MediaInfo:
        """记录同步识别参数并返回固定 AniList 媒体。"""
        self.sync_kwargs = kwargs
        return MediaInfo(
            media_source=MediaSource.AniList,
            media_id="154587",
            type=MediaType.TV,
            title="Sousou no Frieren",
            names=["Frieren"],
        )

    async def async_recognize_media(self, **kwargs) -> MediaInfo:
        """记录异步识别参数并返回固定 AniList 媒体。"""
        self.async_kwargs = kwargs
        return self.recognize_media(**kwargs)


def test_provider_only_runs_when_its_source_is_enabled() -> None:
    """provider 只处理用户选中的自身来源，未选中时不得发起识别。"""
    provider = _FakeAuxiliaryProvider()
    media = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="1",
        type=MediaType.TV,
        title="葬送的芙莉莲",
        year="2023",
    )

    assert provider.get_media_auxiliary_info(
        media,
        media_source=(MediaSource.TMDB,),
    ) == []
    assert provider.sync_kwargs is None

    result = provider.get_media_auxiliary_info(
        media,
        media_source=(MediaSource.TMDB, MediaSource.AniList),
    )

    assert result[0].media_source == MediaSource.AniList
    assert provider.sync_kwargs["media_source"] == MediaSource.AniList
    assert provider.sync_kwargs["media_id"] is None
    assert provider.sync_kwargs["meta"].year == "2023"


def test_provider_uses_native_identity_for_same_source_async() -> None:
    """同来源补充应使用来源原生 ID，避免再次依赖标题消歧。"""
    provider = _FakeAuxiliaryProvider()
    media = MediaInfo(
        media_source=MediaSource.AniList,
        media_id="154587",
        type=MediaType.TV,
        title="葬送的芙莉莲",
    )

    result = asyncio.run(
        provider.async_get_media_auxiliary_info(
            media,
            media_source=(MediaSource.AniList,),
        )
    )

    assert result[0].media_id == "154587"
    assert provider.async_kwargs["media_id"] == "154587"


def test_site_search_keywords_include_aggregated_aliases() -> None:
    """站点搜索参数应优先使用附加信息聚合后的别名列表。"""
    media = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="1",
        type=MediaType.TV,
        title="葬送的芙莉莲",
        names=["Frieren", "Frieren: Beyond Journey's End"],
    )

    with patch(
        "app.chain.search.plan.get_chain_runtime_config_snapshot",
        return_value=SimpleNamespace(max_search_name_limit=3),
    ):
        _, keywords = SearchChain._prepare_params(media)

    assert keywords == [
        "葬送的芙莉莲",
        "Frieren",
        "Frieren: Beyond Journey's End",
    ]
