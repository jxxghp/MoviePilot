from app.core.meta import MetaMusic
from app.helper.music import MusicNameParser


def test_strip_track_prefix_handles_dot_separator():
    """曲序前缀 01. 应剥离并返回曲名。"""
    track, disc, title = MusicNameParser.strip_track_prefix("01.晴天")

    assert (track, disc, title) == (1, None, "晴天")


def test_strip_track_prefix_handles_dash_and_space():
    """常见 rip 命名 01 - 曲名 和 01 曲名 都应识别曲序。"""
    assert MusicNameParser.strip_track_prefix("03 - 七里香") == (3, None, "七里香")
    assert MusicNameParser.strip_track_prefix("05 借口") == (5, None, "借口")


def test_strip_track_prefix_handles_disc_track_number():
    """碟号-曲序前缀 1-02 应同时提取碟号和曲序。"""
    track, disc, title = MusicNameParser.strip_track_prefix("1-02 半岛铁盒")

    assert (track, disc, title) == (2, 1, "半岛铁盒")


def test_strip_track_prefix_handles_number_only_name():
    """纯数字文件名只能得到曲序，曲名返回 None 由调用方兜底。"""
    track, disc, title = MusicNameParser.strip_track_prefix("07")

    assert (track, disc, title) == (7, None, None)


def test_strip_track_prefix_keeps_normal_title():
    """普通曲名不应被误判为曲序前缀。"""
    assert MusicNameParser.strip_track_prefix("晴天") == (None, None, None)
    assert MusicNameParser.strip_track_prefix("2002") == (None, None, None)


def test_split_artist_title():
    """歌手 - 曲名结构应拆分，无分隔符时原文作为标题。"""
    artist, title = MusicNameParser.split_artist_title("周杰伦 - 晴天")

    assert artist == "周杰伦"
    assert title == "晴天"
    assert MusicNameParser.split_artist_title("晴天") == (None, "晴天")


def test_parse_disc_dir():
    """CD1、Disc 2 等碟片目录应识别碟号。"""
    assert MusicNameParser.parse_disc_dir("CD1") == 1
    assert MusicNameParser.parse_disc_dir("Disc 2") == 2
    assert MusicNameParser.parse_disc_dir("disk03") == 3
    assert MusicNameParser.parse_disc_dir("无损音乐") is None


def test_parse_album_dir_extracts_artist_album_year():
    """专辑目录名应提取歌手、专辑、年份和音质描述。"""
    info = MusicNameParser.parse_album_dir("周杰伦 - 七里香 (2004) [FLAC 24bit-96kHz]")

    assert info["artist"] == "周杰伦"
    assert info["album"] == "七里香"
    assert info["year"] == 2004
    assert "FLAC" in info["quality_text"]


def test_parse_album_dir_without_artist():
    """没有歌手分隔的目录名整体作为专辑名。"""
    info = MusicNameParser.parse_album_dir("Random Access Memories (2013)")

    assert info["artist"] is None
    assert info["album"] == "Random Access Memories"
    assert info["year"] == 2013


def test_apply_path_context_fills_wav_meta(tmp_path):
    """无标签 WAV 应从文件名和目录结构补齐曲序、专辑和歌手。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004) [FLAC]"
    album_dir.mkdir()
    wav_file = album_dir / "01.我的地盘.wav"
    wav_file.write_bytes(b"RIFF")

    meta = MetaMusic(org_string=wav_file.name, title=wav_file.stem, audio_format="WAV")
    MusicNameParser.apply_path_context(meta, wav_file)

    assert meta.track_number == 1
    assert meta.title == "我的地盘"
    assert meta.album == "七里香"
    assert meta.artists == ["周杰伦"]
    assert meta.album_artist == "周杰伦"
    assert meta.year == 2004


def test_apply_path_context_uses_disc_subdir(tmp_path):
    """CD1 子目录内的文件应继承碟号并向上找到专辑目录。"""
    album_dir = tmp_path / "Daft Punk - Discovery (2001)"
    disc_dir = album_dir / "CD1"
    disc_dir.mkdir(parents=True)
    wav_file = disc_dir / "01 - One More Time.flac"
    wav_file.write_bytes(b"fLaC")

    meta = MetaMusic(org_string=wav_file.name, title=wav_file.stem, audio_format="FLAC")
    MusicNameParser.apply_path_context(meta, wav_file)

    assert meta.disc_number == 1
    assert meta.track_number == 1
    assert meta.title == "One More Time"
    assert meta.album == "Discovery"
    assert meta.artists == ["Daft Punk"]


def test_apply_path_context_keeps_existing_tags(tmp_path):
    """已有标签字段不应被目录猜测覆盖。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    audio_file = album_dir / "01.我的地盘.mp3"
    audio_file.write_bytes(b"")

    meta = MetaMusic(
        org_string=audio_file.name,
        title="我的地盘",
        artists=["周杰伦"],
        album="七里香",
        year=2004,
        track_number=1,
        audio_format="MP3",
    )
    MusicNameParser.apply_path_context(meta, audio_file)

    assert meta.title == "我的地盘"
    assert meta.artists == ["周杰伦"]
    assert meta.album == "七里香"
    assert meta.year == 2004
