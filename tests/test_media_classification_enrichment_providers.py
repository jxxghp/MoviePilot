"""内置与插件运行时分类事实补充 provider 的适配测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.domain.context import MusicInfo
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.themoviedb import TheMovieDbModule
from app.schemas.category import (
    ClassificationEnrichmentRequest,
    ClassificationIdentityFacts,
)
from app.schemas.types import MediaSource, MediaType
from app.startup.composition.enrichment import (
    RuntimeClassificationEnrichmentProviderCatalog,
)


def _request(
    *,
    media_type: str = MediaType.MOVIE.value,
    missing_fields: list[str] | None = None,
    external_ids: dict[str, str] | None = None,
) -> ClassificationEnrichmentRequest:
    """构造带稳定主身份的 provider 请求。"""
    return ClassificationEnrichmentRequest(
        identity=ClassificationIdentityFacts(
            media_source=MediaSource.Douban.value,
            media_id="1291561",
        ),
        media_type=media_type,
        missing_fields=missing_fields or ["media.countries"],
        external_ids=external_ids or {},
        policy_revision=3,
        timeout_seconds=2.5,
    )


def test_runtime_catalog_requires_host_declaration_and_plugin_source_ownership() -> None:
    """宿主和插件都必须先声明来源所有权，方法同名本身不授予能力。"""
    host_callback = Mock(name="host_callback")
    undeclared_callback = Mock(name="undeclared_callback")
    plugin_callback = Mock(name="plugin_callback")
    orphan_callback = Mock(name="orphan_callback")
    host_module = SimpleNamespace(
        get_media_classification_facts=host_callback,
        get_classification_enrichment_sources=lambda: [MediaSource.TMDB],
    )
    undeclared_module = SimpleNamespace(
        get_media_classification_facts=undeclared_callback,
    )
    module_manager = SimpleNamespace(
        list_specs=lambda: (
            SimpleNamespace(id="themoviedb", metadata={"name": "TheMovieDb"}),
            SimpleNamespace(id="undeclared", metadata={"name": "Undeclared"}),
        ),
        get_running_module=lambda module_id: {
            "themoviedb": host_module,
            "undeclared": undeclared_module,
        }.get(module_id),
    )
    plugin_manager = SimpleNamespace(
        get_plugin_modules=lambda: {
            ("LunaTVSource", "LunaTV"): {"get_media_classification_facts": plugin_callback},
            ("OrphanSource", "Orphan"): {"get_media_classification_facts": orphan_callback},
        },
        get_media_sources=lambda plugin_id: (
            [
                {
                    "media_source": MediaSource("lunatv"),
                    "name": "LunaTV",
                    "media_types": [MediaType.MOVIE, MediaType.TV],
                }
            ]
            if plugin_id == "LunaTVSource"
            else []
        ),
    )
    providers = RuntimeClassificationEnrichmentProviderCatalog(
        module_manager=lambda: module_manager,
        plugin_manager=lambda: plugin_manager,
    ).providers()

    assert [provider.provider_id for provider in providers] == [
        "host:themoviedb",
        "plugin:LunaTVSource",
    ]
    assert providers[0].media_sources == (MediaSource.TMDB.value,)
    assert providers[0].callback is host_callback
    assert providers[1].media_sources == ("lunatv",)
    assert providers[1].callback is plugin_callback


def test_tmdb_enrichment_uses_exact_external_id_and_returns_requested_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDB provider 只按请求中的 TMDB ID 读取详情并裁剪标准事实。"""
    module = TheMovieDbModule()
    requested: list[dict[str, object]] = []

    def tmdb_info(**kwargs: object) -> dict[str, object]:
        """记录详情身份并返回离线 TMDB 电影载荷。"""
        requested.append(kwargs)
        return {
            "id": 129,
            "media_type": "movie",
            "title": "千与千寻",
            "release_date": "2001-07-20",
            "original_language": "ja",
            "production_countries": [{"iso_3166_1": "JP"}],
            "genre_ids": [16],
            "genres": [{"id": 16, "name": "Animation"}],
        }

    monkeypatch.setattr(module, "tmdb_info", tmdb_info)

    response = module.get_media_classification_facts(
        _request(
            missing_fields=[
                "media.countries",
                "media.language",
                "media.genre_keys",
            ],
            external_ids={MediaSource.TMDB.value: "129"},
        )
    )

    assert requested == [{"tmdbid": 129, "mtype": MediaType.MOVIE}]
    assert response is not None
    assert response.media_source == MediaSource.TMDB.value
    assert response.match.media_source == MediaSource.TMDB.value
    assert response.match.media_id == "129"
    assert response.facts == {
        "media.countries": ["JP"],
        "media.language": "ja",
        "media.genre_keys": ["animation"],
    }


def test_tmdb_enrichment_skips_missing_id_and_music_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少精确 TMDB ID 或媒体类型为音乐时不得发起详情查询。"""
    module = TheMovieDbModule()
    monkeypatch.setattr(
        module,
        "tmdb_info",
        lambda **_kwargs: pytest.fail("不应读取 TMDB"),
    )

    assert module.get_media_classification_facts(_request()) is None
    assert (
        module.get_media_classification_facts(
            _request(
                media_type=MediaType.MUSIC.value,
                external_ids={MediaSource.TMDB.value: "129"},
            )
        )
        is None
    )


def test_musicbrainz_enrichment_requires_exact_isrc_and_returns_requested_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MusicBrainz provider 只接受精确 ISRC 候选并按 MBID 读取详情。"""
    module = MusicBrainzModule()
    requests: list[tuple[str, dict[str, object] | None]] = []
    recognized: list[tuple[object, str, str | None]] = []

    def request_json(
        path: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """返回同时包含错误和正确 ISRC 的离线候选。"""
        requests.append((path, params))
        return {
            "recordings": [
                {"id": "wrong", "title": "同名歌曲", "isrcs": ["OTHER"]},
                {
                    "id": "recording-1",
                    "title": "Get Lucky",
                    "isrcs": ["USQX91300105"],
                },
            ]
        }

    def recognize_music(
        media_source: object,
        media_id: str,
        *,
        music_type: str | None = None,
    ) -> MusicInfo:
        """记录稳定 MBID 详情请求并返回标准音乐事实。"""
        recognized.append((media_source, media_id, music_type))
        return MusicInfo(
            media_source=MediaSource.MusicBrainz,
            media_id=media_id,
            title="Get Lucky",
            isrc="USQX91300105",
            genres=["electronic", "funk"],
            release_status="Official",
        )

    monkeypatch.setattr(module, "_request_json", request_json)
    monkeypatch.setattr(module, "recognize_music", recognize_music)

    response = module.get_media_classification_facts(
        _request(
            media_type=MediaType.MUSIC.value,
            missing_fields=["music.genres", "music.release_status"],
            external_ids={"isrc": "USQX91300105"},
        )
    )

    assert requests == [
        (
            "/recording",
            {
                "query": 'isrc:"USQX91300105"',
                "limit": 5,
                "fmt": "json",
            },
        )
    ]
    assert recognized == [(MediaSource.MusicBrainz, "recording-1", "recording")]
    assert response is not None
    assert response.match.media_source == "isrc"
    assert response.match.media_id == "USQX91300105"
    assert response.facts == {
        "music.genres": ["electronic", "funk"],
        "music.release_status": "Official",
    }


def test_musicbrainz_enrichment_rejects_non_exact_isrc_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """标题相同但 ISRC 不同的候选不得作为跨来源同媒体证明。"""
    module = MusicBrainzModule()
    monkeypatch.setattr(
        module,
        "_request_json",
        lambda *_args, **_kwargs: {"recordings": [{"id": "wrong", "title": "Get Lucky", "isrcs": ["OTHER"]}]},
    )
    monkeypatch.setattr(
        module,
        "recognize_music",
        lambda *_args, **_kwargs: pytest.fail("错误 ISRC 不应读取详情"),
    )

    response = module.get_media_classification_facts(
        _request(
            media_type=MediaType.MUSIC.value,
            missing_fields=["music.genres"],
            external_ids={"isrc": "USQX91300105"},
        )
    )

    assert response is None
