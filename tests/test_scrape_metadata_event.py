from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import schemas
from app.chain.scraping import ScrapingChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.events import Event
from app.schemas.types import EventType, MediaType


@pytest.fixture
def scraping_chain() -> Generator[ScrapingChain, None, None]:
    """构造隔离的刮削链，避免单例状态影响事件测试。"""
    key = (ScrapingChain, (), frozenset())
    previous = ScrapingChain._instances.pop(key, None)
    chain = ScrapingChain()
    chain.storagechain = MagicMock()
    try:
        yield chain
    finally:
        ScrapingChain._instances.pop(key, None)
        if previous is not None:
            ScrapingChain._instances[key] = previous


def test_tv_season_scrape_event_includes_series_root(
    scraping_chain: ScrapingChain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """电视剧整理事件以季目录为根时也应刮削剧集根目录。"""
    season_item = schemas.FileItem(
        path="/tv/Show/Season 1",
        name="Season 1",
        type="dir",
        storage="local",
    )
    episode_path = "/tv/Show/Season 1/S01E01.mkv"
    scraping_chain.storagechain.get_item.return_value = season_item
    scraping_chain.storagechain.is_bluray_folder.return_value = False

    def get_file_item(storage: str, path: Path) -> schemas.FileItem:
        """构造事件目录收集所需的文件项。"""
        item_path = Path(path)
        return schemas.FileItem(
            storage=storage,
            path=item_path.as_posix(),
            name=item_path.name,
            type="file" if item_path.suffix else "dir",
        )

    scraping_chain.storagechain.get_file_item.side_effect = get_file_item
    scrape_metadata = MagicMock()
    monkeypatch.setattr(scraping_chain, "scrape_metadata", scrape_metadata)

    scraping_chain.scrape_metadata_event(
        Event(
            event_type=EventType.MetadataScrape,
            event_data={
                "fileitem": season_item,
                "file_list": [episode_path],
                "meta": MetaInfo("Show S01E01"),
                "mediainfo": MediaInfo(type=MediaType.TV),
            },
        )
    )

    assert [call.kwargs["fileitem"].path for call in scrape_metadata.call_args_list] == [
        "/tv/Show",
        "/tv/Show/Season 1",
        episode_path,
    ]
