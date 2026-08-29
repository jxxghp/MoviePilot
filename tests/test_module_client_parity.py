import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.acoustid import AcoustIdModule
from app.modules.anilist.anilist import AniListApi
from app.modules.bangumi.bangumi import BangumiApi
from app.modules.musicbrainz import MusicBrainzModule
from app.modules.theaudiodb import TheAudioDbModule
from app.schemas.types import MediaSource


def test_bangumi_sync_async_share_request_and_projection(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bangumi 同步异步搜索与人物投影应使用同一请求和结果规则。"""
    api = BangumiApi()
    sync_invoke = Mock(
        side_effect=[
            {"list": [{"id": 1}]},
            [{"id": 2, "name": "角色", "actors": [{"id": 3}]}],
        ]
    )
    async_invoke = AsyncMock(
        side_effect=[
            {"list": [{"id": 1}]},
            [{"id": 2, "name": "角色", "actors": [{"id": 3}]}],
        ]
    )
    monkeypatch.setattr(api, "_BangumiApi__invoke", sync_invoke)
    monkeypatch.setattr(api, "_BangumiApi__async_invoke", async_invoke)

    sync_search = api.search("葬送的芙莉莲")
    async_search = asyncio.run(api.async_search("葬送的芙莉莲"))
    sync_credits = api.credits(154587)
    async_credits = asyncio.run(api.async_credits(154587))

    assert sync_search == async_search == [{"id": 1}]
    assert sync_credits == async_credits == [
        {"id": 3, "career": ["角色"]}
    ]
    assert sync_invoke.call_args_list[0].args == async_invoke.await_args_list[0].args
    assert sync_invoke.call_args_list[1].args == async_invoke.await_args_list[1].args
    assert sync_invoke.call_args_list[1].kwargs.keys() == async_invoke.await_args_list[1].kwargs.keys()


def test_anilist_sync_async_share_queries_and_projection(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AniList 同步异步搜索与人物入口应共享查询文本、变量和投影。"""
    api = AniListApi()
    search_payload = {"Page": {"media": [{"id": 154587}]}}
    person_payload = {"Staff": {"id": 95075, "name": {"native": "种崎敦美"}}}
    sync_invoke = Mock(side_effect=[search_payload, person_payload])
    async_invoke = AsyncMock(side_effect=[search_payload, person_payload])
    monkeypatch.setattr(api, "_invoke", sync_invoke)
    monkeypatch.setattr(api, "_async_invoke", async_invoke)

    sync_search = AniListApi.search.__wrapped__(api, "Frieren", 12)
    async_search = asyncio.run(
        AniListApi.async_search.__wrapped__(api, "Frieren", 12)
    )
    sync_person = AniListApi.person_detail.__wrapped__(api, 95075)
    async_person = asyncio.run(
        AniListApi.async_person_detail.__wrapped__(api, 95075)
    )

    assert sync_search == async_search == [{"id": 154587}]
    assert sync_person == async_person == person_payload["Staff"]
    assert sync_invoke.call_args_list[0] == async_invoke.await_args_list[0]
    assert sync_invoke.call_args_list[1] == async_invoke.await_args_list[1]


def test_musicbrainz_sync_async_share_detail_decision(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MusicBrainz 显式详情识别应共享来源准入、参数和缓存收尾。"""
    module = MusicBrainzModule()
    module.cache = Mock()
    meta = MetaMusic(
        title="Yellow",
        media_source=MediaSource.MusicBrainz,
        media_id="recording-1",
    )
    expected = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="recording-1",
        title="Yellow",
    )
    sync_detail = Mock(return_value=expected)
    async_detail = AsyncMock(return_value=expected)
    monkeypatch.setattr(module, "recognize_music", sync_detail)
    monkeypatch.setattr(module, "async_recognize_music", async_detail)

    sync_result = module.recognize_media(meta=meta, cache=False)
    async_result = asyncio.run(
        module.async_recognize_media(meta=meta, cache=False)
    )

    assert sync_result == async_result == expected
    assert async_detail.await_args is not None
    assert sync_detail.call_args.args == async_detail.await_args.args
    assert sync_detail.call_args.kwargs == async_detail.await_args.kwargs == {}
    assert module.cache.update.call_count == 2


def test_theaudiodb_sync_async_share_request_plan_and_response(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TheAudioDB 同步异步详情应共享请求计划、响应解析和实体选择。"""
    module = TheAudioDbModule()
    payload = {
        "track": [
            {
                "idTrack": "32793500",
                "strTrack": "Yellow",
                "strArtist": "Coldplay",
            }
        ]
    }
    sync_request = Mock(return_value=payload)
    async_request = AsyncMock(return_value=payload)
    monkeypatch.setattr(module, "_request_json", sync_request)
    monkeypatch.setattr(module, "_async_request_json", async_request)

    sync_result = module.recognize_music(
        MediaSource.TheAudioDB, "32793500", music_type="recording"
    )
    async_result = asyncio.run(
        module.async_recognize_music(
            MediaSource.TheAudioDB, "32793500", music_type="recording"
        )
    )

    assert sync_result == async_result
    assert sync_result is not None and sync_result.media_id == "32793500"
    assert sync_request.call_args == async_request.await_args
    plan = module._request_plan("client-key", "track.php", {"h": "32793500"})
    assert plan is not None
    assert plan.url.endswith("/client-key/track.php")
    assert plan.params == {"h": "32793500"}


def test_acoustid_lookup_plan_and_response_projection_are_transport_neutral() -> None:
    """AcoustID 请求载荷和响应筛选不得依赖同步或异步传输实现。"""
    recording_id = "38035858-f990-4fbb-b3b2-f2f8b958eeba"
    plan = AcoustIdModule._lookup_plan(" client-key ", 243, "AQADtM...")
    payload = {
        "status": "ok",
        "results": [
            {"score": 0.98, "recordings": [{"id": recording_id}]}
        ],
    }

    assert plan is not None
    assert plan.data == {
        "client": "client-key",
        "duration": 243,
        "fingerprint": "AQADtM...",
        "meta": "recordingids",
        "format": "json",
    }
    assert AcoustIdModule._project_lookup_response(200, payload) == recording_id
    assert AcoustIdModule._project_lookup_response(503, payload) is None
