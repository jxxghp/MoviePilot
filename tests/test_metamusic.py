from pathlib import Path
from typing import Optional

import pytest

from app.domain.meta.metamusic import (
    MetaMusic,
    MusicNameContext,
    MusicNameParseResult,
    MusicNameParser,
    MusicNamePattern,
    MusicNameRegistry,
)
from app.domain.metainfo import MetaInfo, MetaInfoPath


def parse_title(title: str) -> MetaMusic:
    """构造种子/文件名标题解析结果，供识别断言复用。"""
    return MetaMusic(org_string=title, title=title, parse_title=True)


def test_music_name_registry_supports_dynamic_pattern_and_parser(monkeypatch):
    """外部程序可独立注册命名模式和解析器，并在使用后完整注销。"""
    pattern_name = "test_program"
    parser_name = "test_program_parser"

    def match_program(context: MusicNameContext):
        """匹配测试程序的双冒号命名。"""
        if not context.text.startswith("PROGRAM::"):
            return None
        parts = context.text.split("::")
        return parts if len(parts) == 3 else None

    def parse_program(context, matched):
        """把测试程序命名解析为艺术家和标题。"""
        _prefix, artist, title = matched.payload
        return MusicNameParseResult(
            title=title,
            artists=[artist],
            year=context.year,
        )

    MusicNameRegistry.register_pattern(
        MusicNamePattern(pattern_name, match_program, priority=1000)
    )
    MusicNameRegistry.register_parser(
        MusicNameParser(parser_name, (pattern_name,), parse_program, priority=1000)
    )
    try:
        monkeypatch.setattr(
            "app.adapters.system.rust.parse_metamusic",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("自定义注册表不应调用 Rust")
            ),
        )
        context = MusicNameContext(
            raw="PROGRAM::周杰伦::晴天",
            normalized="PROGRAM::周杰伦::晴天",
            text="PROGRAM::周杰伦::晴天",
            artists=(),
        )
        matched = MusicNameRegistry.match_pattern(context)
        parser = MusicNameRegistry.match_parser(matched)

        assert matched.pattern_name == pattern_name
        assert parser.name == parser_name

        # FLAC 由公共层剔除，扩展解析器只需处理自身命名结构。
        meta = parse_title("PROGRAM::周杰伦::晴天 FLAC")
        assert meta.artists == ["周杰伦"]
        assert meta.title == "晴天"
        assert meta.audio_format == "FLAC"
    finally:
        MusicNameRegistry.unregister_parser(parser_name)
        MusicNameRegistry.unregister_pattern(pattern_name)

    assert parser_name not in {parser.name for parser in MusicNameRegistry.get_parsers()}
    assert pattern_name not in {pattern.name for pattern in MusicNameRegistry.get_patterns()}


def test_music_name_registry_same_name_replacement_falls_back_to_python(monkeypatch):
    """同名替换内置解析器时应保留 Python 动态扩展语义。"""
    original = next(
        parser for parser in MusicNameRegistry.get_parsers()
        if parser.name == "fallback"
    )

    def parse_replacement(context, _matched):
        """为同名替换用例返回固定解析结果。"""
        return MusicNameParseResult(
            title=f"Python:{context.text}",
            artists=["扩展解析器"],
        )

    replacement = MusicNameParser(
        "fallback",
        ("fallback",),
        parse_replacement,
    )
    MusicNameRegistry.register_parser(replacement, replace=True)
    try:
        monkeypatch.setattr(
            "app.adapters.system.rust.parse_metamusic",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("同名替换解析器时不应调用 Rust")
            ),
        )

        meta = parse_title("自定义曲名")

        assert meta.title == "Python:自定义曲名"
        assert meta.artists == ["扩展解析器"]
    finally:
        MusicNameRegistry.register_parser(original, replace=True)

    assert MusicNameRegistry._uses_default_components()


def test_parse_query_uses_rust_and_maps_music_fields(monkeypatch):
    """MetaMusic 公共查询入口应使用 Rust 并回填完整核心字段。"""
    calls = []
    parsed = {
        "title": "晴天",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "year": 2003,
        "disc_number": 1,
        "track_number": 3,
        "audio_format": "FLAC",
        "audio_lossless": True,
        "bit_depth": 24,
        "sample_rate": 96000,
        "bitrate": 2304000,
    }

    def parse_metamusic(title, artists=None, year=None):
        """记录 Rust wrapper 调用并返回完整音乐字段。"""
        calls.append((title, artists, year))
        return parsed

    monkeypatch.setattr(
        "app.adapters.system.rust.parse_metamusic",
        parse_metamusic,
    )

    meta = MetaMusic.parse_query("周杰伦 - 晴天 FLAC 24bit 96kHz")

    assert calls == [("周杰伦 - 晴天 FLAC 24bit 96kHz", None, None)]
    assert {
        key: getattr(meta, key)
        for key in parsed
    } == parsed


def test_apply_title_preserves_existing_fields_from_rust(monkeypatch):
    """Rust 解析只应补充空字段，不覆盖标签或文件后缀证据。"""
    monkeypatch.setattr(
        "app.adapters.system.rust.parse_metamusic",
        lambda *_args, **_kwargs: {
            "title": "Rust 曲名",
            "artists": ["Rust 歌手"],
            "album": "Rust 专辑",
            "year": 2026,
            "disc_number": 2,
            "track_number": 8,
            "audio_format": "AAC",
            "audio_lossless": False,
            "bit_depth": 16,
            "sample_rate": 48000,
            "bitrate": 320000,
        },
    )

    meta = MetaMusic(
        org_string="source.flac",
        title="source",
        artists=["标签歌手"],
        album="标签专辑",
        year=2003,
        disc_number=1,
        track_number=3,
        audio_format="FLAC",
        bit_depth=24,
        sample_rate=96000,
        bitrate=2304000,
        parse_title=True,
    )

    assert meta.title == "Rust 曲名"
    assert meta.artists == ["标签歌手"]
    assert meta.album == "标签专辑"
    assert meta.year == 2003
    assert meta.disc_number == 1
    assert meta.track_number == 3
    assert meta.audio_format == "FLAC"
    assert meta.audio_lossless is True
    assert meta.bit_depth == 24
    assert meta.sample_rate == 96000
    assert meta.bitrate == 2304000


def test_apply_title_falls_back_when_rust_returns_none(monkeypatch):
    """Rust wrapper 不可用时应完整执行现有 Python 命名解析。"""
    monkeypatch.setattr(
        "app.adapters.system.rust.parse_metamusic",
        lambda *_args, **_kwargs: None,
    )

    meta = MetaMusic.parse_query("周杰伦 - 晴天 FLAC 24bit 96kHz")

    assert meta.title == "晴天"
    assert meta.artists == ["周杰伦"]
    assert meta.audio_format == "FLAC"
    assert meta.bit_depth == 24
    assert meta.sample_rate == 96000


def test_metainfo_audio_suffix_remains_authoritative_with_rust(monkeypatch):
    """音频文件后缀应优先于标题中的演唱会音频编码标记。"""
    calls = []

    def parse_metamusic(title, artists=None, year=None):
        """模拟 Rust 从标题规格中识别出 AAC。"""
        calls.append((title, artists, year))
        return {
            "title": "S.H.E十七音乐会",
            "artists": ["S.H.E"],
            "year": 2018,
            "audio_format": "AAC",
            "audio_lossless": False,
        }

    monkeypatch.setattr(
        "app.adapters.system.rust.parse_metamusic",
        parse_metamusic,
    )
    filename = "S H E - S H E十七音乐会 2018 WEB-DL 1080P AVC AAC-FHDMv.flac"

    meta = MetaInfo(filename)

    assert calls == [(filename.removesuffix(".flac"), None, None)]
    assert meta.title == "S.H.E十七音乐会"
    assert meta.artists == ["S.H.E"]
    assert meta.audio_format == "FLAC"
    assert meta.audio_lossless is True


def test_metainfo_path_uses_rust_once_and_keeps_python_directory_context(
        monkeypatch,
):
    """音频路径只应用 Rust 解析一次文件名，目录线索仍由 Python 补齐。"""
    calls = []

    def parse_metamusic(title, artists=None, year=None):
        """记录路径中的 Rust 文件名解析并返回曲目字段。"""
        calls.append((title, artists, year))
        return {
            "title": "我的地盘",
            "artists": [],
            "track_number": 1,
            "audio_format": "AAC",
            "audio_lossless": False,
        }

    monkeypatch.setattr(
        "app.adapters.system.rust.parse_metamusic",
        parse_metamusic,
    )
    path = MetaInfoPath(
        Path("/music/周杰伦 - 七里香 (2004) [FLAC]/01.我的地盘.flac")
    )

    assert calls == [("我的地盘", None, None)]
    assert path.title == "我的地盘"
    assert path.track_number == 1
    assert path.album == "七里香"
    assert path.artists == ["周杰伦"]
    assert path.album_artist == "周杰伦"
    assert path.year == 2004
    assert path.audio_format == "FLAC"
    assert path.audio_lossless is True


@pytest.mark.parametrize(
    ("raw", "artists", "title", "year", "audio_format"),
    [
        (
            "The Beatles - Vinyl Collection【2020】【CD】【FLAC分轨】",
            ["The Beatles"],
            "Vinyl Collection",
            2020,
            "FLAC",
        ),
        (
            "Primeval - Forged In Earth【2026】【WEB】【FLAC分轨】(24/48bit)",
            ["Primeval"],
            "Forged In Earth",
            2026,
            "FLAC",
        ),
        (
            "Professor Green - Alive Till I'm Dead 2010-FLAC 分轨-nbarock",
            ["Professor Green"],
            "Alive Till I'm Dead",
            2010,
            "FLAC",
        ),
        (
            "Togenashi Togeari 5th One Man Live Moments of Sound 2025 "
            "1080p BluRay x265 10bit FLAC 2.0-ADE",
            [],
            "Togenashi Togeari 5th One Man Live Moments of Sound",
            2025,
            "FLAC",
        ),
        (
            "田震 - 田震 (1996) FLAC {HRS-004-2}",
            ["田震"],
            "田震",
            1996,
            "FLAC",
        ),
        (
            "[2022.02.23] 中恵光城 - SELENiTE -Mitsuki Nakae Works Best Album- "
            "[CD][FLAC+CUE+LOG+BK][KDSD-01049]",
            ["中恵光城"],
            "SELENiTE -Mitsuki Nakae Works Best Album",
            None,
            "FLAC",
        ),
        (
            "[260123] 映画「超かぐや姫！」劇中曲「超かぐや姫！ 」 "
            "[48kHz/24bit][FLAC]",
            [],
            "映画「超かぐや姫!」劇中曲「超かぐや姫! 」",
            None,
            "FLAC",
        ),
        (
            "[Audio-4U] 茶太 — Chata 1.0 (flac)",
            ["茶太"],
            "Chata 1.0",
            None,
            "FLAC",
        ),
    ],
)
def test_apply_title_real_site_music_samples(
        raw: str,
        artists: list[str],
        title: str,
        year: Optional[int],
        audio_format: str,
):
    """真实站点音乐种子标题应剔除公共干扰并保留有效命名字段。"""
    meta = parse_title(raw)

    assert meta.artists == artists
    assert meta.title == title
    assert meta.year == year
    assert meta.audio_format == audio_format


@pytest.mark.parametrize(
    ("raw", "artists", "title", "year"),
    [
        (
            "Aimer-Aimer Hall Tour 2022 ''Walpurgisnacht'' Live at "
            "TOKYO GARDEN THEATER Blu-ray 1080p AVC LPCM 2.0",
            [],
            "Aimer-Aimer Hall Tour ''Walpurgisnacht'' Live at TOKYO GARDEN THEATER",
            2022,
        ),
        (
            "MANATSU NO ZENKOKU TOUR 2021 FINAL! IN TOKYO DOME "
            "Blu-ray 1080p AVC LPCM 2.0",
            [],
            "MANATSU NO ZENKOKU TOUR FINAL! IN TOKYO DOME",
            2021,
        ),
        (
            "Rainie Yang - Ban Shu Xuan Yan 2008 DVD 480i MPEG-2 MPEG-2",
            ["Rainie Yang"],
            "Ban Shu Xuan Yan",
            2008,
        ),
        (
            "SARD UNDERGROUND LIVE TOUR 2025 FANTASY "
            "Blu-ray 1080p AVC LPCM2.0",
            [],
            "SARD UNDERGROUND LIVE TOUR FANTASY",
            2025,
        ),
        (
            "Kylie: Tension Tour Live 2026 2160p NF WEB-DL "
            "DDP 5.1 H.265-CHORTLE",
            [],
            "Kylie: Tension Tour Live",
            2026,
        ),
        (
            "Nogizaka46 2021 'Kimi ni Shikarareta' Type-A, B, C, D，"
            "Blu-ray 1080p AVC",
            [],
            "Nogizaka46 'Kimi ni Shikarareta' Type-A, B, C, D",
            2021,
        ),
        (
            "SBS Korea Pop Music Festival in Summer 2026 "
            "1080p AAC 2.0 x264@JJL",
            [],
            "SBS Korea Pop Music Festival in Summer",
            2026,
        ),
        (
            "RTHK31 China Philharmonic Orchestra Concert Series - "
            "23rd Anniversary Concert 260704 1080i HDTV H264-NGBRTHK31",
            ["RTHK31 China Philharmonic Orchestra Concert Series"],
            "23rd Anniversary Concert",
            2026,
        ),
    ],
)
def test_music_video_scene_pattern_uses_music_specific_token_parser(
        raw: str,
        artists: list[str],
        title: str,
        year: int,
):
    """影视式音乐资源应按音乐 token 语义清理，且保留年份后的演出名称。"""
    context = MetaMusic._prepare_name_context(raw=raw, artists=[], year=None)
    matched = MusicNameRegistry.match_pattern(context)
    meta = parse_title(raw)

    assert matched.pattern_name == "music_video_scene"
    assert meta.artists == artists
    assert meta.title == title
    assert meta.year == year


@pytest.mark.parametrize(
    "raw",
    [
        "Daft Punk - Random Access Memories 2013 FLAC",
        "[Audio-4U] 茶太 — Chata 1.0 (flac)",
    ],
)
def test_music_video_scene_pattern_requires_combined_video_signature(raw: str):
    """普通音频标题只有格式或版本数字时，不得误入音乐视频场景模式。"""
    context = MetaMusic._prepare_name_context(raw=raw, artists=[], year=None)
    matched = MusicNameRegistry.match_pattern(context)

    assert matched.pattern_name != "music_video_scene"


@pytest.mark.parametrize(
    ("raw", "artists", "title", "year", "audio_format"),
    [
        (
            "The Bug Club - On the Intricate Inner Workings of the System "
            "2025-FLAC 分軌-Redacted",
            ["The Bug Club"],
            "On the Intricate Inner Workings of the System",
            2025,
            "FLAC",
        ),
        (
            "李宇春 - 皇后与梦想 - 2006-FLAC分轨-OpenCD-九月萌",
            ["李宇春"],
            "皇后与梦想",
            2006,
            "FLAC",
        ),
        ("西班牙幻想曲SACD", [], "西班牙幻想曲", None, "DSD"),
        ("無字天碟 Indefinable（WAV+CUE原抓）", [], "無字天碟 Indefinable", None, "WAV"),
        ("刘星-无所事事（WAV+CUE原抓）", ["刘星"], "无所事事", None, "WAV"),
        ("喜多郎-古事记SACD", ["喜多郎"], "古事记", None, "DSD"),
        ("巫启贤太傻（黄金版）WAV分轨原抓", [], "巫启贤太傻(黄金版)", None, "WAV"),
        (
            "王若琳 - The Adult Storybook 2009 SACD",
            ["王若琳"],
            "The Adult Storybook",
            2009,
            "DSD",
        ),
    ],
)
def test_common_audio_release_noise_is_removed(
        raw: str,
        artists: list[str],
        title: str,
        year: Optional[int],
        audio_format: str,
):
    """真实音频发布尾链只提供格式和年份，不应污染艺术家或标题。"""
    meta = parse_title(raw)

    assert meta.artists == artists
    assert meta.title == title
    assert meta.year == year
    assert meta.audio_format == audio_format


def test_strip_track_prefix_handles_dot_separator():
    """曲序前缀 01. 应剥离并返回曲名。"""
    track, disc, title = MetaMusic.split_track_prefix("01.晴天")

    assert (track, disc, title) == (1, None, "晴天")


def test_strip_track_prefix_handles_dash_and_space():
    """常见 rip 命名 01 - 曲名 和 01 曲名 都应识别曲序。"""
    assert MetaMusic.split_track_prefix("03 - 七里香") == (3, None, "七里香")
    assert MetaMusic.split_track_prefix("05 借口") == (5, None, "借口")


def test_strip_track_prefix_handles_disc_track_number():
    """碟号-曲序前缀 1-02 应同时提取碟号和曲序。"""
    track, disc, title = MetaMusic.split_track_prefix("1-02 半岛铁盒")

    assert (track, disc, title) == (2, 1, "半岛铁盒")


def test_strip_track_prefix_handles_number_only_name():
    """纯数字文件名只能得到曲序，曲名返回 None 由调用方兜底。"""
    track, disc, title = MetaMusic.split_track_prefix("07")

    assert (track, disc, title) == (7, None, None)


def test_strip_track_prefix_keeps_normal_title():
    """普通曲名不应被误判为曲序前缀。"""
    assert MetaMusic.split_track_prefix("晴天") == (None, None, None)
    assert MetaMusic.split_track_prefix("2002") == (None, None, None)


def test_split_artist_title():
    """歌手 - 曲名结构应拆分，无分隔符时原文作为标题。"""
    artist, title = MetaMusic.split_artist_title("周杰伦 - 晴天")

    assert artist == "周杰伦"
    assert title == "晴天"
    assert MetaMusic.split_artist_title("晴天") == (None, "晴天")


def test_parse_disc_dir():
    """CD1、Disc 2 等碟片目录应识别碟号。"""
    assert MetaMusic.parse_disc_dir("CD1") == 1
    assert MetaMusic.parse_disc_dir("Disc 2") == 2
    assert MetaMusic.parse_disc_dir("disk03") == 3
    assert MetaMusic.parse_disc_dir("无损音乐") is None


def test_parse_album_dir_extracts_artist_album_year():
    """专辑目录名应提取歌手、专辑、年份和音质描述。"""
    info = MetaMusic.parse_album_dir("周杰伦 - 七里香 (2004) [FLAC 24bit-96kHz]")

    assert info["artist"] == "周杰伦"
    assert info["album"] == "七里香"
    assert info["year"] == 2004
    assert "FLAC" in info["quality_text"]


def test_parse_album_dir_without_artist():
    """没有歌手分隔的目录名整体作为专辑名。"""
    info = MetaMusic.parse_album_dir("Random Access Memories (2013)")

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
    meta.apply_path_context(wav_file)

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
    meta.apply_path_context(wav_file)

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
    meta.apply_path_context(audio_file)

    assert meta.title == "我的地盘"
    assert meta.artists == ["周杰伦"]
    assert meta.album == "七里香"
    assert meta.year == 2004


def test_apply_path_context_uses_full_dynamic_filename_parser(tmp_path):
    """无标签文件名应进入完整动态模式，清理音乐视频场景规格。"""
    audio_file = tmp_path / (
        "S H E - S H E十七音乐会 2018 WEB-DL 1080P AVC AAC-FHDMv.flac"
    )
    audio_file.write_bytes(b"fake-flac")
    meta = MetaMusic(
        org_string=audio_file.name,
        title=audio_file.stem,
        audio_format="FLAC",
    )

    meta.apply_path_context(audio_file)

    assert meta.artists == ["S.H.E"]
    assert meta.title == "S.H.E十七音乐会"
    assert meta.year == 2018
    assert meta.audio_format == "FLAC"


def test_apply_path_context_only_fills_missing_tag_fields(tmp_path):
    """部分标签存在时保留标签值，只从完整文件名解析补充空字段。"""
    audio_file = tmp_path / "周杰伦 - 文件名曲目 2018 FLAC.flac"
    audio_file.write_bytes(b"fake-flac")
    meta = MetaMusic(
        org_string=audio_file.name,
        title="标签曲名",
        artists=[],
        album="标签专辑",
        year=2020,
    )

    meta.apply_path_context(audio_file)

    assert meta.title == "标签曲名"
    assert meta.artists == ["周杰伦"]
    assert meta.album == "标签专辑"
    assert meta.year == 2020
    assert meta.audio_format == "FLAC"
    assert meta.audio_lossless is True


def test_apply_title_splits_artist_and_track():
    """标准「歌手 - 曲名」种子标题应拆分艺术家与曲名。"""
    meta = parse_title("周杰伦 - 晴天")

    assert meta.artists == ["周杰伦"]
    assert meta.title == "晴天"


def test_apply_title_splits_multi_artists():
    """多艺术家 & 联名写法应拆分为列表保留顺序。"""
    meta = parse_title("章子怡 & 周深 - 灯火里的中国")

    assert meta.artists == ["章子怡", "周深"]
    assert meta.title == "灯火里的中国"


def test_apply_title_strips_quality_tokens():
    """格式、位深采样与发行标记不应进入曲名。"""
    meta = parse_title("[250917] SARD UNDERGROUND - 故障した車 FLAC")

    assert meta.artists == ["SARD UNDERGROUND"]
    assert meta.title == "故障した車"
    assert meta.audio_format == "FLAC"


def test_apply_title_strips_video_tokens():
    """演唱会视频种子的分辨率与编码标记不应进入曲名。"""
    meta = parse_title("S H E - S H E十七音乐会 2018 WEB-DL 1080P AVC AAC-FHDMv")

    # 连续单字母空格序列是缩写点号被压平的结果，还原为 S.H.E 才能与条目署名比对
    assert meta.artists == ["S.H.E"]
    assert meta.title == "S.H.E十七音乐会"
    # 尾部年份提取为发行年份线索
    assert meta.year == 2018


def test_apply_title_splits_latin_hyphen_artist_album():
    """拉丁「艺术家-专辑」无空格连字符命名应拆分，单侧单词不采信。"""
    meta = parse_title("Gene Clark-White Light 1971 - FLAC 16bit 44 1khz")

    assert meta.artists == ["Gene Clark"]
    assert meta.title == "White Light"
    assert meta.year == 1971
    # 左侧单词（Heize-Undo）与右侧发布组标签不触发拆分
    assert parse_title("Heize-Undo.2022.FLAC").artists == []
    # 全大写复合词属于艺术家名本身，不能从 KUNG-FU 中间拆开。
    compound = parse_title("ASIAN KUNG-FU GENERATION Discography (2003-2026) [FLAC]")
    assert compound.artists == []
    assert compound.title == "ASIAN KUNG-FU GENERATION Discography (2003-2026)"


def test_apply_title_splits_various_artists_prefix():
    """场景命名的 Various Artists-Title 无空格连字符前缀应拆分为合辑署名。"""
    meta = parse_title("Various.Artists-Reply.1988.OST")

    assert meta.artists == ["Various Artists"]
    assert meta.title == "Reply 1988 OST"


def test_apply_title_splits_year_sandwich():
    """「艺术家 年份 专辑」三明治结构按中部年份拆分并提取年份。"""
    meta = parse_title("Leehom Wang 2010 The 18 Martial Arts")

    assert meta.artists == ["Leehom Wang"]
    assert meta.title == "The 18 Martial Arts"
    assert meta.year == 2010

    # 多艺术家分隔符与单词专辑名同样适用
    meta = parse_title("ASKA&SENS 1993 YAH YAH YAH")
    assert meta.artists == ["ASKA", "SENS"]
    assert meta.title == "YAH YAH YAH"
    assert meta.year == 1993


def test_apply_title_year_sandwich_guards():
    """三明治拆分的护栏：规格残留与长艺术家段不误拆。"""
    # 剩余段数字开头是规格（2.0）不是专辑名
    assert parse_title("K3 Kan Het S02 2014 2.0 -MINIBEL").artists == []
    # 剩余段连字符开头是发布组标签（-PTer）不是专辑名
    assert parse_title("Ashton Celebration 2013 -PTer").artists == []
    # 艺术家段超过 4 个词时拒绝拆分（含曲名与年份的完整标题）
    assert parse_title("Bee Gees One Night Only 1997 -ProfessorP").artists == []


def test_apply_title_restores_letter_abbrev():
    """单字母点号缩写还原不误伤合法缩写与单词。"""
    meta = parse_title("E.S.Posthumus - Ashielf Alpen FLAC")

    # 点分少于 3 处的合法缩写不被场景点分/还原逻辑破坏
    assert meta.artists == ["E.S.Posthumus"]
    assert meta.title == "Ashielf Alpen"


def test_apply_title_strips_release_group_tag():
    """格式标记后的发布组标签（大小写混合）应整体剔除。"""
    meta = parse_title("某某乐队 - 星空 FLAC-FHDMv")

    assert meta.title == "星空"


def test_apply_title_strips_date_prefix():
    """电视录制标题开头的日期前缀应剔除。"""
    meta = parse_title("2018.01.10 藤田麻衣子 思い続ければ FLAC")

    assert meta.title == "藤田麻衣子 思い続ければ"
    assert meta.audio_format == "FLAC"


def test_apply_title_date_prefix_with_time():
    """带时分秒的录制前缀（下划线分隔）同样应剔除。"""
    meta = parse_title("2024-01-27_20-00_ＷＯＷＯＷプライム_松任谷由実　５０ｔｈ　Ａｎｎｉｖｅｒｓａｒｙ")

    assert "2024" not in (meta.title or "")
    assert "松任谷由実" in (meta.title or "")
    assert "50th" in (meta.title or "")


def test_apply_title_keeps_song_named_with_year():
    """曲名自带的年份数字（非日期结构）不应被误剔除。"""
    meta = parse_title("2002年的第一场雪")

    assert meta.title == "2002年的第一场雪"


def test_apply_title_year_range_as_year():
    """全集标题尾部的年份区间应提取结束年并剥离出标题。"""
    meta = parse_title("天国的情人-邓丽君作品全集1967-1995")

    assert meta.title == "天国的情人"
    assert meta.artists == ["邓丽君"]
    assert meta.year == 1995


def test_apply_title_year_range_in_title_kept():
    """年份区间后随内容文字时属于标题本身，只提取年份不剥离。"""
    meta = parse_title("许茹芸 - 许茹芸1995-2000年光华真纪录 (2001)")

    assert meta.artists == ["许茹芸"]
    assert meta.title == "许茹芸1995-2000年光华真纪录"
    # 括号年份优先于区间提取的结束年
    assert meta.year == 2001


def test_apply_title_scene_dot_naming():
    """场景点分命名的点号应归一为空格，发布组标签剔除。"""
    meta = parse_title("Shan.Ge.Liao.Zai.2023.WEB-DL.FLAC-CMCTA")

    assert meta.title == "Shan Ge Liao Zai"
    assert meta.year == 2023
    assert meta.audio_format == "FLAC"


def test_apply_title_scene_dot_with_symbols():
    """点分命名中环绕符号的点（.&.、.-.）也应归一并支持艺术家拆分。"""
    meta = parse_title(
        "Deep.Purple.&.Orchestra.-.Live.At.Montreux.1999.2022.1080p.BluRay.AVC.DTS-HD.MA5.1"
    )

    assert meta.artists == ["Deep Purple", "Orchestra"]
    assert meta.title == "Live At Montreux 1999 2022"


def test_apply_title_keeps_artist_abbreviation_dots():
    """点分隔少于 3 处的艺术家缩写点号不应被归一。"""
    meta = parse_title("E.S.Posthumus - Maraboot")

    assert meta.artists == ["E.S.Posthumus"]
    assert meta.title == "Maraboot"


def test_apply_title_va_alias_artist():
    """合辑署名 VA 应归一为 MusicBrainz 规范署名 Various Artists。"""
    meta = parse_title("VA - Funky Jazz Saxophone 2024 FLAC")

    assert meta.artists == ["Various Artists"]
    assert meta.title == "Funky Jazz Saxophone"
    assert meta.year == 2024


def test_apply_title_va_scene_prefix():
    """场景命名 VA-Title 无空格连字符写法应按别名前缀拆分。"""
    meta = parse_title(
        "VA-Once.Upon.a.Time.in.Hollywood.Original.Motion.Picture.Soundtrack.2019.FLAC.24bit.96kHz"
    )

    assert meta.artists == ["Various Artists"]
    assert meta.title == "Once Upon a Time in Hollywood Original Motion Picture Soundtrack"
    assert meta.year == 2019


def test_apply_title_keeps_single_word_title():
    """无音质标记时「艺术家 - 单词曲名」的曲名不应被当发布组标签剔除。"""
    meta = parse_title("E.S.Posthumus - Maraboot")

    assert meta.title == "Maraboot"


def test_apply_title_keeps_title_with_quality_tokens():
    """存在音质标记时，空格连字符后的单词曲名也不应被当发布组标签剔除。"""
    meta = parse_title("Yes - Aurora [Bonus Tracks Edition, 24-bit Hi-Res] (2026) [FLAC]")

    assert meta.artists == ["Yes"]
    assert meta.title == "Aurora"
    assert meta.year == 2026


def test_apply_title_album_marker():
    """「歌手《专辑名》」书名号命名应提取艺术家、专辑与碟号。"""
    meta = parse_title("李宗盛《理性与感性作品音乐会-CD2》2006-FLAC-分轨")

    assert meta.artists == ["李宗盛"]
    assert meta.album == "理性与感性作品音乐会"
    assert meta.title == "理性与感性作品音乐会"
    assert meta.disc_number == 2
    assert meta.year == 2006


def test_apply_title_bilingual_album_marker_prefix():
    """双语原声命名应保留英文艺术家/标题，不把整段前缀当成艺术家。"""
    meta = parse_title(
        "Max Richter - Ad Astra Original Motion Picture Soundtrack "
        "马克斯·里希特 - 《星际探索》电影原声带 2019 FLAC-SeedPool"
    )

    assert meta.artists == ["Max Richter"]
    assert meta.title == "Ad Astra Original Motion Picture Soundtrack"
    assert meta.album == "星际探索"
    assert meta.year == 2019


def test_apply_title_strips_cue_and_plus():
    """APE+CUE 类格式联合写法应剔除，残留加号不阻断标题提取。"""
    meta = parse_title("世界著名古典大师名版收藏（15）RCA发烧古典系列-2007-FLAC-APE+CUE")

    assert meta.artists == []
    assert meta.title == "世界著名古典大师名版收藏(15)RCA发烧古典系列"
    assert meta.year == 2007


def test_apply_title_cjk_hyphen_artist_suffix():
    """CJK「曲名-歌手」无空格连字符写法应反向拆分艺术家。"""
    meta = parse_title("因为有你-毛阿敏")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "因为有你"


def test_apply_title_does_not_split_ascii_hyphen_inside_cjk_title():
    """CJK 标题中的 A-on 等拉丁复合词不能生成虚假的艺术家。"""
    meta = parse_title(
        "[250226] 重戦機エルガイム A-on STORE 連動特典"
        "「重戦機エルガイム(カセット版復刻CD)」 [FLAC+CUE]"
    )

    assert meta.artists == []
    assert meta.title.startswith("重戦機エルガイム A-on STORE")


def test_apply_title_double_em_dash_split():
    """双破折号分隔的「主题——歌手」写法应拆分艺术家。"""
    meta = parse_title("为你盛开——许巍 无尽光芒巡回演唱会 2025")

    assert meta.artists == ["许巍"]
    assert meta.title.startswith("为你盛开")


def test_apply_title_keeps_english_hyphen_title():
    """英文曲名中的连字符是标题组成部分，不做艺术家拆分。"""
    meta = parse_title("Dire Straits - Alchemy-Live 1983")

    assert meta.artists == ["Dire Straits"]
    assert meta.title == "Alchemy-Live"
    assert meta.year == 1983


def test_apply_title_lossless_declaration():
    """「无损」声明词应剥离出标题并推断无损音质。"""
    meta = parse_title("某某 - 晴天 FLAC 无损")

    assert meta.title == "晴天"
    assert meta.audio_lossless is True


def test_apply_title_track_prefix_in_title():
    """标题中的曲序前缀应提取为曲序并还原曲名。"""
    meta = parse_title("01.晴天")

    assert meta.track_number == 1
    assert meta.title == "晴天"


def test_apply_title_bracket_date_prefix():
    """发行日期方括号前缀应整体剔除。"""
    meta = parse_title("[250917] 某歌手 - 某曲名")

    assert meta.artists == ["某歌手"]
    assert meta.title == "某曲名"


def test_apply_title_spec_segments_do_not_shift_split():
    """尾部规格段（WEB-DL/位深/发布组）不应把拆分点推到艺术家与曲名的连字符上。"""
    meta = parse_title(
        "许茹芸 - 等得到 (电影《如影随心》主题曲 独唱版) (2019) - WEB-DL - 24bit ALAC-HHWEB")

    assert meta.artists == ["许茹芸"]
    # 含书名号的括号注释是版本说明，应拼回曲名而不触发专辑结构
    assert meta.title == "等得到 (电影《如影随心》主题曲 独唱版)"
    assert meta.year == 2019


def test_apply_title_album_marker_with_song_artist_prefix():
    """书名号前的「曲名-歌手」连字符段应反向拆分，首段作为曲名线索。"""
    meta = parse_title("为你盛开-许巍《无尽光芒》2019.FLAC")

    assert meta.artists == ["许巍"]
    assert meta.album == "无尽光芒"
    assert meta.title == "为你盛开"
    assert meta.year == 2019


def test_apply_title_album_marker_rest_disc_number():
    """书名号后仅剩碟号时应提取为碟号，曲名回退用专辑名。"""
    meta = parse_title("李宗盛《理性与感性作品音乐会》 CD2 (2006)")

    assert meta.artists == ["李宗盛"]
    assert meta.album == "理性与感性作品音乐会"
    assert meta.disc_number == 2
    assert meta.title == "理性与感性作品音乐会"
    assert meta.year == 2006


def test_apply_title_keeps_single_paren_disambiguation():
    """单层括号注释是条目消歧后缀，应保留在曲名中。"""
    meta = parse_title("周杰伦 - 晴天 (电影版)")

    assert meta.artists == ["周杰伦"]
    assert meta.title == "晴天 (电影版)"


def test_apply_title_collection_with_space_sample_rate():
    """合集/精选是发行形态标记，空格写法的采样率（44 1khz）也应剔除；
    剔除后仅剩悬空分隔符时艺术家仍需拆出。"""
    meta = parse_title("周杰伦 - 合集  2000-2022 - FLAC 16bit 44 1khz")

    assert meta.artists == ["周杰伦"]
    assert meta.title is None
    assert meta.year == 2022
    assert meta.audio_format == "FLAC"


@pytest.mark.parametrize("title", ["孙楠 - 楠得精选 2001", "[合集] 缘之空音乐合集 [FLAC]"])
def test_apply_title_keeps_collection_words_inside_work_name(title: str):
    """合集/精选嵌在作品名中时是有效文字，只清理独立发行标签。"""
    meta = parse_title(title)

    assert "精选" in (meta.title or "") or "合集" in (meta.title or "")
