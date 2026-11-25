"""
音乐来源回退的发行对象与目录装配；不注册测试用例。
"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.domain.context import MusicAlbumInfo, MusicInfo


def _directory_setup(tmp_path, monkeypatch, sources, *, known_album=True):
    """
    使用真实无标签FLAC和独立媒体链测试整专来源选择，不注册全局单例。
    """
    directory = tmp_path / ("Artist - Album" if known_album else "Music")
    directory.mkdir()
    for index, title in enumerate(("First", "Second"), 1):
        name = f"{index:02} - {title}.flac" if known_album else f"{index:02} - Artist - {title}.flac"
        shutil.copyfile(Path(__file__).parents[2] / "fixtures/audio/silence.flac", directory / name)
    config = {"sources": sources}
    monkeypatch.setattr("app.chain.media.album.get_chain_runtime_config_snapshot", lambda: SimpleNamespace(
        search_source=config["sources"], audio_extensions=(".flac",), music_cue_enable=True,
        music_release_region_priority=("CN",), music_release_script_priority=("Hans",)))
    chain = object.__new__(MediaChain)
    chain._album_dir_cache = AlbumDirectoryCache(8)
    monkeypatch.setattr(chain, "_finalize_recognition_result", lambda info, **_kwargs: info)
    monkeypatch.setattr(chain, "_async_finalize_recognition_result", AsyncMock(side_effect=lambda info, **_kwargs: info))
    primary = SimpleNamespace(match_music_album=Mock(return_value=None), async_match_music_album=AsyncMock(return_value=None))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", lambda: primary)
    return directory, chain, config, primary


def _album(source, identity="album-one", *, tracks=True):
    """
    构造同一来源命名空间内的完整专辑和原生曲目身份。
    """
    album = MusicAlbumInfo(media_source=source, media_id=identity, title="Album", artists=["Artist"])
    if tracks:
        album.tracks = [MusicInfo(media_source=source, media_id=f"{identity}-track-{index}", title=title,
                                  artists=["Artist"], album="Album", album_id=identity,
                                  track_number=index, disc_number=1, duration=3)
                        for index, title in enumerate(("First", "Second"), 1)]
    return album


def _backend(albums):
    """
    模拟来源链的读边界，真正的搜索计划、候选核验和文件对位继续运行。
    """
    by_id = {album.media_id: album for album in albums}
    cards = [album.to_music_info() for album in albums]
    return SimpleNamespace(search_music=Mock(return_value=cards), async_search_music=AsyncMock(return_value=cards),
                           get_music_album=Mock(side_effect=by_id.get), async_get_music_album=AsyncMock(side_effect=by_id.get))
