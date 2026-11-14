"""
验证内置 NFO 生成器的返回值符合模块契约。
"""

import xml.etree.ElementTree as ET
from typing import Optional
from unittest.mock import Mock

import pytest

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.domain.scraper import MediaScraperHelper
from app.modules.douban.scraper import DoubanScraper
from app.modules.themoviedb.scraper import TmdbScraper
from app.runtime.extensions.module.contracts import diagnose_module_result
from app.schemas.types import MediaType


@pytest.mark.parametrize(
    "scraper_type",
    (TmdbScraper, DoubanScraper, MediaScraperHelper),
)
def test_builtin_movie_nfo_returns_utf8_text(scraper_type: type) -> None:
    """
    内置生成器应返回可解析的中文 XML 文本，且通过模块结果契约检查。
    """
    mediainfo = MediaInfo(type=MediaType.MOVIE, title="测试电影")
    scraper = scraper_type()
    if isinstance(scraper, TmdbScraper):
        nfo = scraper.get_metadata_nfo(meta=Mock(spec=MetaBase), mediainfo=mediainfo)
    else:
        nfo = scraper.get_metadata_nfo(mediainfo=mediainfo)

    assert isinstance(nfo, str)
    assert diagnose_module_result("metadata_nfo", nfo) == ()
    assert nfo.startswith('<?xml version="1.0" encoding="utf-8"?>')
    document = ET.fromstring(nfo)
    assert document.tag == "movie"
    assert document.findtext("title") == "测试电影"


@pytest.mark.parametrize(
    ("season", "episode", "expected_root"),
    (
        (None, None, "tvshow"),
        (1, None, "season"),
        (1, 2, "episodedetails"),
    ),
)
def test_shared_tv_nfo_returns_text_for_each_shape(
    season: Optional[int], episode: Optional[int], expected_root: str
) -> None:
    """
    统一生成器生成剧集、季和单集时都应保持文本返回值。
    """
    mediainfo = MediaInfo(type=MediaType.TV, title="测试剧集")

    nfo = MediaScraperHelper.get_metadata_nfo(
        mediainfo, season=season, episode=episode
    )

    assert isinstance(nfo, str)
    assert diagnose_module_result("metadata_nfo", nfo) == ()
    document = ET.fromstring(nfo)
    assert document.tag == expected_root
    assert document.findtext("title") == "测试剧集"
