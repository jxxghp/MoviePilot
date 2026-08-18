import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.orchestration.media import MediaChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.musicbrainz import MusicBrainzModule


def _release_detail(release_id: str, title: str, artist: str, tracks: list[tuple[str, int]]):
    """构造 MusicBrainz Release 详情响应，tracks 为 (曲名, 时长秒) 列表。"""
    return {
        "id": release_id,
        "title": title,
        "date": "2004-08-03",
        "artist-credit": [{"artist": {"id": "artist-1", "name": artist}}],
        "release-group": {"id": "rg-1", "primary-type": "Album", "secondary-types": []},
        "media": [
            {
                "position": 1,
                "track-count": len(tracks),
                "tracks": [
                    {
                        "position": index + 1,
                        "length": length * 1000,
                        "title": name,
                        "recording": {"id": f"rec-{index + 1}", "title": name, "length": length * 1000},
                    }
                    for index, (name, length) in enumerate(tracks)
                ],
            }
        ],
    }


ALBUM_TRACKS = [("我的地盘", 215), ("七里香", 299), ("借口", 265)]


def _local_tracks():
    """构造无标签整专目录读取得到的本地曲目元数据。"""
    return [
        MetaMusic(title=name, track_number=index + 1, duration=length, audio_format="WAV")
        for index, (name, length) in enumerate(ALBUM_TRACKS)
    ]


def test_match_music_album_selects_release_by_count_and_duration(monkeypatch):
    """曲目数和时长一致的发行版本应被选中并返回曲目表。"""
    module = MusicBrainzModule()
    detail = _release_detail("release-1", "七里香", "周杰伦", ALBUM_TRACKS)

    def fake_request(path, params=None):
        if path == "/release":
            return {"releases": [{"id": "release-1", "title": "七里香"}]}
        if path == "/release/release-1":
            return detail
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)

    album = module.match_music_album(
        MetaMusic(album="七里香", artists=["周杰伦"]),
        _local_tracks(),
    )

    assert album is not None
    assert album.media_id == "rg-1"
    assert album.title == "七里香"
    assert album.artists == ["周杰伦"]
    assert [track.media_id for track in album.tracks] == ["rec-1", "rec-2", "rec-3"]
    assert album.tracks[0].track_number == 1
    assert album.tracks[0].album == "七里香"


def test_match_music_album_rejects_mismatched_trackset(monkeypatch):
    """曲目数和时长都对不上的候选应被拒绝，避免写错标签。"""
    module = MusicBrainzModule()
    # 候选只有 1 首歌且时长差异巨大
    detail = _release_detail("release-1", "七里香", "周杰伦", [("七里香", 60)])

    def fake_request(path, params=None):
        if path == "/release":
            return {"releases": [{"id": "release-1", "title": "七里香"}]}
        if path == "/release/release-1":
            return detail
        return None

    monkeypatch.setattr(module, "_request_json", fake_request)

    album = module.match_music_album(
        MetaMusic(album="七里香", artists=["周杰伦"]),
        _local_tracks(),
    )

    assert album is None


def test_release_queries_fallback_to_track_titles():
    """目录名没有专辑线索时应使用代表性曲名反查发行版本。"""
    queries = MusicBrainzModule._release_queries(
        MetaMusic(title="Various"),
        [MetaMusic(title="晴天"), MetaMusic(title="七里香"), MetaMusic(title="03")],
    )

    assert any("recording:" in query for query in queries)
    # 纯数字文件名不能作为曲名线索
    assert all('"03"' not in query for query in queries)


@pytest.fixture()
def media_chain():
    """构造媒体链并清理目录匹配缓存。"""
    chain = MediaChain()
    MediaChain._album_dir_cache.clear()
    yield chain
    MediaChain._album_dir_cache.clear()


def test_recognize_album_directory_maps_files(tmp_path, media_chain, monkeypatch):
    """目录级匹配应把每个音频文件对位到专辑曲目并缓存结果。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    files = []
    for index, (name, length) in enumerate(ALBUM_TRACKS):
        file = album_dir / f"{index + 1:02d}.{name}.wav"
        file.write_bytes(b"RIFF")
        files.append(file)

    album = MusicAlbumInfo(
        media_source="musicbrainz",
        media_id="rg-1",
        title="七里香",
        artists=["周杰伦"],
        tracks=[
            MusicInfo(
                media_source="musicbrainz",
                media_id=f"rec-{index + 1}",
                title=name,
                artists=["周杰伦"],
                album="七里香",
                track_number=index + 1,
                duration=length,
            )
            for index, (name, length) in enumerate(ALBUM_TRACKS)
        ],
    )
    source_chain = Mock()
    source_chain.match_music_album.side_effect = lambda *_args, **_kwargs: album
    monkeypatch.setattr(
        "app.application.orchestration.media.MusicBrainzChain",
        Mock(return_value=source_chain),
    )

    matched = media_chain.recognize_music_album_directory(album_dir)

    assert len(matched) == len(files)
    for index, file in enumerate(files):
        info = matched[str(file.resolve())]
        assert info.media_id == f"rec-{index + 1}"
        assert info.title == ALBUM_TRACKS[index][0]
    # 同一目录再次识别直接命中缓存，不重复请求模块
    assert media_chain.recognize_music_album_directory(album_dir) == matched
    source_chain.match_music_album.assert_called_once()


def test_async_recognize_album_directory_calls_async_module(
        tmp_path,
        media_chain,
        monkeypatch,
):
    """异步目录识别应直接调用模块异步接口，并兼容单个专辑返回值。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    files = []
    for index, (name, _length) in enumerate(ALBUM_TRACKS):
        file = album_dir / f"{index + 1:02d}.{name}.wav"
        file.write_bytes(b"RIFF")
        files.append(file)

    album = MusicAlbumInfo(
        media_source="musicbrainz",
        media_id="rg-1",
        title="七里香",
        artists=["周杰伦"],
        tracks=[
            MusicInfo(
                media_source="musicbrainz",
                media_id=f"rec-{index + 1}",
                title=name,
                artists=["周杰伦"],
                album="七里香",
                track_number=index + 1,
                duration=length,
            )
            for index, (name, length) in enumerate(ALBUM_TRACKS)
        ],
    )
    async_run_module = AsyncMock(return_value=album)
    run_module = Mock(side_effect=AssertionError("异步目录识别不应调用同步模块接口"))
    source_chain = Mock()
    source_chain.async_match_music_album = async_run_module
    source_chain.match_music_album = run_module
    monkeypatch.setattr("app.application.orchestration.media.MusicBrainzChain", Mock(return_value=source_chain))
    monkeypatch.setattr(
        "app.application.orchestration.media.AudioMetadataHelper.read_many",
        lambda _files: _local_tracks(),
    )

    matched = asyncio.run(media_chain.async_recognize_music_album_directory(album_dir))

    assert [matched[str(file.resolve())].media_id for file in files] == [
        "rec-1",
        "rec-2",
        "rec-3",
    ]
    async_run_module.assert_awaited_once()
    assert async_run_module.await_args.args[0].album == "七里香"
    run_module.assert_not_called()


def test_recognize_album_directory_skips_single_file(tmp_path, media_chain, monkeypatch):
    """单文件目录不走专辑匹配，交给单曲识别链路。"""
    album_dir = tmp_path / "单曲"
    album_dir.mkdir()
    (album_dir / "晴天.wav").write_bytes(b"RIFF")

    def fake_run_module(method, **kwargs):
        raise AssertionError("单文件目录不应触发专辑匹配")

    monkeypatch.setattr(media_chain, "_match_music_album_directory", fake_run_module)

    assert media_chain.recognize_music_album_directory(album_dir) == {}


def test_recognize_album_directory_invalidates_cache_after_same_count_rename(
        tmp_path,
        media_chain,
        monkeypatch,
):
    """目录内文件数量不变但文件名变化时，专辑曲目映射缓存必须失效。"""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    first = album_dir / "01 - First.wav"
    second = album_dir / "02 - Second.wav"
    first.write_bytes(b"RIFF")
    second.write_bytes(b"RIFF")
    calls = []

    def fake_match(_dir_path, files):
        """记录目录匹配输入，返回空映射以专注验证缓存签名。"""
        calls.append([file.name for file in files])
        return {}

    monkeypatch.setattr(media_chain, "_match_music_album_directory", fake_match)

    media_chain.recognize_music_album_directory(album_dir)
    first.rename(album_dir / "01 - Renamed.wav")
    media_chain.recognize_music_album_directory(album_dir)

    assert calls == [
        ["01 - First.wav", "02 - Second.wav"],
        ["01 - Renamed.wav", "02 - Second.wav"],
    ]


def test_recognize_music_by_path_falls_back_to_album_match(tmp_path, monkeypatch):
    """单曲识别无远端身份时应用目录级匹配结果兜底。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    file = album_dir / "01.我的地盘.wav"
    file.write_bytes(b"RIFF")

    matched_info = MusicInfo(
        media_source="musicbrainz",
        media_id="rec-1",
        title="我的地盘",
        artists=["周杰伦"],
        album="七里香",
        track_number=1,
    )
    # recognize_music_by_path 已下沉至 MediaChain 统一识别入口
    chain = MediaChain()
    monkeypatch.setattr(chain, "recognize_media", lambda **kwargs: None)
    monkeypatch.setattr(
        chain,
        "_music_album_dir_fallback",
        lambda path: matched_info,
    )

    meta, info = chain.recognize_music_by_path(file)

    assert info.media_id == "rec-1"
    assert info.title == "我的地盘"
    assert info.album == "七里香"
    # 本地音频参数应保留在识别结果中
    assert meta.audio_format == "WAV"


def test_async_music_album_fallback_calls_async_directory_match(tmp_path, monkeypatch):
    """异步路径回退应等待目录异步识别，不得调用同步目录识别。"""
    album_dir = tmp_path / "七里香"
    album_dir.mkdir()
    file = album_dir / "01.我的地盘.wav"
    file.write_bytes(b"RIFF")
    matched_info = MusicInfo(
        media_source="musicbrainz",
        media_id="rec-1",
        title="我的地盘",
    )
    async_recognize = AsyncMock(
        return_value={str(file.resolve()): matched_info}
    )
    sync_recognize = Mock(side_effect=AssertionError("异步回退不应调用同步目录识别"))
    monkeypatch.setattr(
        MediaChain,
        "async_recognize_music_album_directory",
        async_recognize,
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        sync_recognize,
    )

    result = asyncio.run(MediaChain()._async_music_album_dir_fallback(file))

    assert result is matched_info
    async_recognize.assert_awaited_once_with(album_dir)
    sync_recognize.assert_not_called()
