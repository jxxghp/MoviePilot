"""人工音乐候选从 HTTP 参数到共用搜索、结果序列化的集成回归。"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.web.security.access import verify_token
from app.api.endpoints import search as endpoint
from app.chain.search import media
from app.chain.search.facade import SearchChain
from app.domain.context import MusicInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType


@pytest.mark.anyio
@pytest.mark.parametrize("include_candidates", [False, True])
async def test_music_manual_candidate_opt_in_survives_http_and_serialization(include_candidates):
    """默认精确搜索不会放行未核验署名，显式人工候选请求保留资源但不绑定目标。"""
    media_id = "695f5ac8-cfd5-4e7b-96a0-22830c931bb0"
    target = MusicInfo(media_source=MediaSource.MusicBrainz, media_id=media_id, title="晴天", artists=["周杰伦"])
    chain = SearchChain()
    chain.async_save_last_search_params = AsyncMock()
    chain._async_save_results = AsyncMock()
    chain.cancel_ai_recommend = Mock()
    chain._SearchChain__async_search_all_sites = AsyncMock(return_value=[
        TorrentInfo(title="Jay Chou - 晴天 FLAC", category=MediaType.MUSIC.value),
    ])
    metadata = Mock()
    metadata.async_recognize_media = AsyncMock(return_value=target)
    metadata.async_supplement_media_info = AsyncMock(side_effect=lambda mediainfo: mediainfo)
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/api/v1/search")
    app.dependency_overrides[verify_token] = lambda: Mock()
    with (
        patch.object(endpoint, "SearchChain", return_value=chain),
        patch.object(media, "MediaChain", return_value=metadata),
        patch("app.chain.search.execution.asyncio.sleep", new=AsyncMock()),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get(f"/api/v1/search/media/{media_id}", params={
                "media_source": "musicbrainz", "include_candidates": include_candidates,
            })
    assert response.status_code == 200
    payload = response.json()
    if include_candidates:
        assert payload["success"] is True
        assert len(payload["data"]) == 1
        candidate = payload["data"][0]
        assert candidate["match_status"] == "candidate"
        assert candidate["match_reason"] == "artist_unverified"
        assert candidate["media_info"] is None
        assert candidate["meta_info"]["artists"] == ["Jay Chou"]
        assert candidate["meta_info"]["media_id"] is None
        assert chain.async_save_last_search_params.call_args.kwargs["include_candidates"] is True
    else:
        assert payload["success"] is False
        assert chain._async_save_results.call_args.args[0] == []
