import pytest

from app.modules.themoviedb.scraper import TmdbScraper


@pytest.mark.parametrize(
    ("episode_id", "expected_tmdb_id"),
    ((None, None), ("None", None), ("not-a-number", None), (129, "129")),
)
def test_episode_nfo_omits_invalid_tmdb_id(episode_id, expected_tmdb_id):
    """集查询返回无效 ID 时应省略身份节点，但保留其余集元数据。"""
    document = TmdbScraper._TmdbScraper__gen_tv_episode_nfo_file(
        tmdbid=100,
        episodeinfo={"id": episode_id, "name": "测试集"},
        season=2,
        episode=3,
    )

    xml = document.toxml()
    if expected_tmdb_id is None:
        assert "<tmdbid>" not in xml
        assert "<uniqueid" not in xml
    else:
        assert f"<tmdbid>{expected_tmdb_id}</tmdbid>" in xml
        assert f'<uniqueid type="tmdb" default="true">{expected_tmdb_id}</uniqueid>' in xml
    assert "<title>测试集</title>" in xml
