from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from app.modules.emby.emby import Emby
from app.modules.plex.plex import Plex
from app.modules.zspace.zspace import ZSpace
from app.schemas import RefreshMediaItem
from app.schemas.types import MediaType


def _music_item(path: str) -> RefreshMediaItem:
    """构造不依赖发行年份的音乐媒体库刷新项。"""
    return RefreshMediaItem(
        title="晴天",
        type=MediaType.MUSIC,
        category="音乐",
        target_path=Path(path),
    )


def test_emby_music_refresh_resolves_library_by_path_without_year():
    """Emby 音乐刷新应直接按目标路径定位媒体库，不得误查电视剧。"""
    service = object.__new__(Emby)
    service.folders = [
        {"Id": "music-library", "SubFolders": [{"Path": "/library/music"}]},
    ]
    service._Emby__get_emby_series_id_by_name = Mock()
    service.get_movies = Mock()

    library_id = service._Emby__get_emby_library_id_by_item(
        _music_item("/library/music/周杰伦/叶惠美/03 - 晴天.flac")
    )

    assert library_id == "music-library"
    service._Emby__get_emby_series_id_by_name.assert_not_called()
    service.get_movies.assert_not_called()


def test_zspace_music_refresh_resolves_library_by_path_without_year():
    """极影视音乐刷新应直接按目标路径定位媒体库，不得误查电视剧。"""
    service = object.__new__(ZSpace)
    service.folders = [
        {"Id": "music-library", "SubFolders": [{"Path": "/library/music"}]},
    ]
    service._ZSpace__get_series_id_by_name = Mock()
    service.get_movies = Mock()

    library_id = service._ZSpace__get_library_id_by_item(
        _music_item("/library/music/周杰伦/叶惠美/03 - 晴天.flac")
    )

    assert library_id == "music-library"
    service._ZSpace__get_series_id_by_name.assert_not_called()
    service.get_movies.assert_not_called()


def test_emby_refreshes_all_unique_libraries_and_aggregates_failures():
    """Emby 批量刷新必须处理全部唯一媒体库，并汇总任一刷新失败。"""
    service = object.__new__(Emby)
    service._Emby__get_emby_library_id_by_item = Mock(
        side_effect=["library-a", "library-b", "library-a"]
    )
    service._Emby__refresh_emby_library_by_id = Mock(side_effect=[True, False])
    items = [_music_item(f"/library/music/{index}.flac") for index in range(3)]

    assert service.refresh_library_by_items(items) is False
    assert service._Emby__refresh_emby_library_by_id.call_args_list == [
        call("library-a"),
        call("library-b"),
    ]


def test_zspace_refreshes_all_unique_libraries_and_aggregates_failures():
    """极影视批量刷新必须处理全部唯一媒体库，并汇总任一刷新失败。"""
    service = object.__new__(ZSpace)
    service._ZSpace__get_library_id_by_item = Mock(
        side_effect=["library-a", "library-b", "library-a"]
    )
    service._ZSpace__refresh_library_by_id = Mock(side_effect=[True, False])
    items = [_music_item(f"/library/music/{index}.flac") for index in range(3)]

    assert service.refresh_library_by_items(items) is False
    assert service._ZSpace__refresh_library_by_id.call_args_list == [
        call("library-a"),
        call("library-b"),
    ]


def test_plex_refreshes_every_matched_path():
    """Plex 批量刷新不能在首个路径后提前返回。"""
    service = object.__new__(Plex)
    service._plex = SimpleNamespace(
        library=SimpleNamespace(update=Mock(return_value=True)),
        query=Mock(),
    )
    service._libraries = []
    service._Plex__find_librarie = Mock(
        side_effect=[
            ("1", Path("/library/music/Album A/01.flac")),
            ("2", Path("/library/music/Album B/01.flac")),
        ]
    )

    result = service.refresh_library_by_items(
        [
            _music_item("/library/music/Album A/01.flac"),
            _music_item("/library/music/Album B/01.flac"),
        ]
    )

    assert result is True
    assert service._plex.query.call_count == 2
    assert "/library/sections/1/refresh" in service._plex.query.call_args_list[0].args[0]
    assert "/library/sections/2/refresh" in service._plex.query.call_args_list[1].args[0]
