import pytest

from app.chain.music import MusicChain
from app.core.context import MusicAlbumInfo, MusicInfo
from app.core.meta import MetaMusic
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
def music_chain():
    """构造绕过重量级初始化的 MusicChain，并清理目录匹配缓存。"""
    chain = MusicChain.__new__(MusicChain)
    MusicChain._album_dir_cache.clear()
    yield chain
    MusicChain._album_dir_cache.clear()


def test_recognize_album_directory_maps_files(tmp_path, music_chain, monkeypatch):
    """目录级匹配应把每个音频文件对位到专辑曲目并缓存结果。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    files = []
    for index, (name, length) in enumerate(ALBUM_TRACKS):
        file = album_dir / f"{index + 1:02d}.{name}.wav"
        file.write_bytes(b"RIFF")
        files.append(file)

    album = MusicAlbumInfo(
        source="musicbrainz",
        media_id="rg-1",
        title="七里香",
        artists=["周杰伦"],
        tracks=[
            MusicInfo(
                source="musicbrainz",
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
    calls = {"count": 0}

    def fake_run_module(method, **kwargs):
        calls["count"] += 1
        return [album] if method == "match_music_album" else []

    monkeypatch.setattr(music_chain, "run_module", fake_run_module)

    matched = music_chain.recognize_album_directory(album_dir)

    assert len(matched) == len(files)
    for index, file in enumerate(files):
        info = matched[str(file.resolve())]
        assert info.media_id == f"rec-{index + 1}"
        assert info.title == ALBUM_TRACKS[index][0]
    # 同一目录再次识别直接命中缓存，不重复请求模块
    assert music_chain.recognize_album_directory(album_dir) == matched
    assert calls["count"] == 1


def test_recognize_album_directory_skips_single_file(tmp_path, music_chain, monkeypatch):
    """单文件目录不走专辑匹配，交给单曲识别链路。"""
    album_dir = tmp_path / "单曲"
    album_dir.mkdir()
    (album_dir / "晴天.wav").write_bytes(b"RIFF")

    def fake_run_module(method, **kwargs):
        raise AssertionError("单文件目录不应触发专辑匹配")

    monkeypatch.setattr(music_chain, "run_module", fake_run_module)

    assert music_chain.recognize_album_directory(album_dir) == {}


def test_recognize_by_path_falls_back_to_album_match(tmp_path, music_chain, monkeypatch):
    """单曲识别无远端身份时应用目录级匹配结果兜底。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    file = album_dir / "01.我的地盘.wav"
    file.write_bytes(b"RIFF")

    matched_info = MusicInfo(
        source="musicbrainz",
        media_id="rec-1",
        title="我的地盘",
        artists=["周杰伦"],
        album="七里香",
        track_number=1,
    )
    monkeypatch.setattr(
        music_chain, "recognize_media", lambda **kwargs: None
    )
    monkeypatch.setattr(
        music_chain,
        "recognize_album_directory",
        lambda path: {str(file.resolve()): matched_info},
    )

    meta, info = music_chain.recognize_by_path(file)

    assert info.media_id == "rec-1"
    assert info.title == "我的地盘"
    assert info.album == "七里香"
    # 本地音频参数应保留在识别结果中
    assert meta.audio_format == "WAV"
