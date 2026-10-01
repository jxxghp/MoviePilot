"""下载种子原始证据在丢弃旧身份后仍参与整专查询，并保持作用范围。"""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen.flac import FLAC

from app.application.history import DownloadHistorySnapshot
from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaType
from tests.test_transfer_sync_extra_files import make_fileitem


def _history(root: Path, **kwargs) -> DownloadHistorySnapshot:
    """构造只有站点原始名称、没有已选媒体身份的下载历史。"""
    values = dict(
        id=1, path=str(root), type=MediaType.MUSIC.value, title="Unrecognized",
        torrent_name="Artist - Actual Album (2003) [FLAC]",
        torrent_description="音乐专辑", note=None,
    )
    values.update(kwargs)
    return DownloadHistorySnapshot(**values)


def _audio_files(root: Path) -> list[Path]:
    """生成无标签但具有真实时长和曲名的整专输入。"""
    root.mkdir(parents=True)
    files = [root / "01 - First Song.flac", root / "02 - Second Song.flac"]
    for path in files:
        shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    return files


@pytest.mark.parametrize("note", [
    None, {"music": {"version": 99}}, {"music": {"version": 1}},
    {"music": {"version": 1, "meta": {"type": "电影"}}},
])
def test_missing_or_invalid_music_snapshot_keeps_torrent_evidence(tmp_path, note):
    """旧历史没有可用音乐快照时，原始主副标题仍可补艺人、专辑和年份。"""
    root = tmp_path / "opaque"
    paths = _audio_files(root)
    history = _history(root, note=note)

    meta, info = TransferChain._restore_music_download_context(history, paths[0])

    assert info is None
    assert meta.title == "First Song"
    assert meta.track_number == 1
    assert meta.duration == 3
    assert meta.artists == ["Artist"]
    assert meta.album == "Actual Album"
    assert meta.year == 2003
    assert meta.media_id is None


def test_discarded_wrong_identity_does_not_discard_original_resource(tmp_path):
    """选错的媒体快照可以丢弃，原始种子不是错误身份的一部分。"""
    root = tmp_path / "opaque"
    paths = _audio_files(root)
    wrong = MusicInfo(media_source="musicbrainz", media_id="wrong", title="Wrong Song", album="Wrong Album")
    history = _history(root, note={"music": {
        "version": 1, "meta": MetaMusic.from_music_info(wrong).to_dict(), "media": wrong.to_dict(),
    }})

    meta, info = TransferChain._restore_music_download_context(history, paths[0], discard_saved_identity=True)

    assert info is None
    assert (meta.title, meta.album) == ("First Song", "Actual Album")
    assert meta.artists == ["Artist"]
    assert meta.media_id is None


@pytest.mark.parametrize("subdir,torrent", [
    ("2001 - Child Album", "Artist - Actual Album (2003) [FLAC]"),
    ("", "Artist - Discography (2003) [FLAC]"),
])
def test_package_title_does_not_override_independent_albums(tmp_path, subdir, torrent):
    """全集和独立子专辑只接收艺人，不能被顶层下载标题改成同一个专辑。"""
    root = tmp_path / "opaque"
    paths = _audio_files(root / subdir if subdir else root)

    meta, _ = TransferChain._restore_music_download_context(_history(root, torrent_name=torrent), paths[0])

    assert meta.artists == ["Artist"]
    assert meta.album == ("Child Album" if subdir else None)
    assert meta.year == (2001 if subdir else None)


def test_resource_album_reaches_disc_directory_and_uses_subtitle(tmp_path):
    """CD 子目录继承同一发行信息，中文副标题补齐主标题没有的作品与署名。"""
    root = tmp_path / "opaque"
    paths = _audio_files(root / "CD2")
    history = _history(root, torrent_name="FLAC", torrent_description="歌手：周杰伦；专辑：叶惠美；[2003]")

    meta, _ = TransferChain._restore_music_download_context(history, paths[0])

    assert meta.artists == ["周杰伦"]
    assert meta.album == "叶惠美"
    assert meta.year == 2003
    assert meta.disc_number == 2
    assert meta.title == "First Song"


@pytest.mark.parametrize("outside,media_type", [(True, "音乐"), (False, "电影"), (False, "电视剧")])
def test_unrelated_history_cannot_supply_music_identity(tmp_path, outside, media_type):
    """原始资源必须属于当前音频，影视任务的附加音轨不能被改为音乐。"""
    root = tmp_path / "opaque"
    paths = _audio_files(root)
    history = _history(tmp_path / "other" if outside else root, type=media_type)

    assert TransferChain._restore_music_download_context(history, paths[0]) == (None, None)


@pytest.fixture
def media_chain(monkeypatch):
    """使用隔离目录缓存，避免同步、异步测试共享其它用例的识别结果。"""
    from app.chain.media.cache import AlbumDirectoryCache

    chain = MediaChain()
    monkeypatch.setattr(chain, "_album_dir_cache", AlbumDirectoryCache(16))
    return chain


@pytest.mark.parametrize("asynchronous", [False, True])
def test_original_resource_is_used_by_album_query(tmp_path, monkeypatch, media_chain, asynchronous):
    """资源艺人、专辑、年份进入真实目录查询，并保留各文件曲名和时长。"""
    root = tmp_path / "opaque"
    paths = _audio_files(root)
    meta, _ = TransferChain._restore_music_download_context(_history(root), paths[0])
    album = MusicAlbumInfo(media_source="musicbrainz", media_id="group", title="Actual Album", tracks=[
        MusicInfo(media_source="musicbrainz", media_id=f"recording-{index}", title=title,
                  album="Actual Album", artists=["Artist"], year=2003, track_number=index, duration=3)
        for index, title in enumerate(("First Song", "Second Song"), 1)
    ])
    source = Mock(match_music_album=Mock(return_value=album), async_match_music_album=AsyncMock(return_value=album))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))

    if asynchronous:
        matched = asyncio.run(media_chain.async_recognize_music_album_directory(root, contextual_meta=meta))
        query = source.async_match_music_album.call_args
    else:
        matched_meta, info = TransferChain._match_music_album_context(make_fileitem(str(paths[0])), paths[0], meta)
        assert info.media_id == "recording-1"
        assert matched_meta.title == "First Song"
        matched = media_chain.recognize_music_album_directory(root, contextual_meta=meta)
        query = source.match_music_album.call_args

    assert query.args[0].title == query.args[0].album == "Actual Album"
    assert query.args[0].artists == ["Artist"]
    assert query.args[0].year == 2003
    assert [track.duration for track in query.args[1]] == [3, 3]
    assert [info.title for info in matched.values()] == ["First Song", "Second Song"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_album_cache_changes_with_resource_context(tmp_path, monkeypatch, media_chain, asynchronous):
    """同一目录和文件签名也必须区分先后提供的不同专辑证据。"""
    root = tmp_path / "opaque"
    _audio_files(root)
    source = Mock(match_music_album=Mock(return_value=None), async_match_music_album=AsyncMock(return_value=None))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))
    for album in ("First Album", "First Album", "Second Album"):
        context = MetaMusic(album=album, artists=["Artist"])
        if asynchronous:
            asyncio.run(media_chain.async_recognize_music_album_directory(root, contextual_meta=context))
        else:
            media_chain.recognize_music_album_directory(root, contextual_meta=context)
    operation = source.async_match_music_album if asynchronous else source.match_music_album

    assert [call.args[0].album for call in operation.call_args_list] == ["First Album", "Second Album"]


def test_local_album_tags_override_conflicting_resource(tmp_path, monkeypatch, media_chain):
    """目录里已明确的专辑标签和年份不能被种子候选覆盖。"""
    root = tmp_path / "opaque"
    paths = _audio_files(root)
    for path in paths:
        audio = FLAC(path)
        audio.update({"album": ["Tagged Album"], "artist": ["Tagged Artist"], "date": ["2001"]})
        audio.save()
    source = Mock(match_music_album=Mock(return_value=None))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))

    media_chain.recognize_music_album_directory(root, contextual_meta=MetaMusic(
        album="Wrong Album", artists=["Wrong Artist"], year=2020,
    ))

    query = source.match_music_album.call_args.args[0]
    assert query.album == "Tagged Album"
    assert query.artists == ["Tagged Artist"]
    assert query.year == 2001
