import pytest

from app.chain.media import MediaChain


@pytest.mark.parametrize("tmdbid", (None, "", "None", "not-a-number"))
def test_media_chain_ignores_invalid_legacy_tmdb_id(tmdbid):
    """历史 NFO 中的无效 TMDB ID 应按无 ID 处理，而不是抛出转换异常。"""
    assert MediaChain._resolve_media_source_params(tmdbid=tmdbid) == (
        None,
        None,
        None,
        None,
        None,
    )
