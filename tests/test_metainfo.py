# -*- coding: utf-8 -*-
from pathlib import Path
from unittest.mock import patch

from app.core.metainfo import MetaInfo, MetaInfoPath, find_metainfo
from tests.cases.meta import meta_cases


def test_metainfo():
    """测试常见标题元数据识别结果。"""
    for info in meta_cases:
        if info.get("path"):
            meta_info = MetaInfoPath(path=Path(info.get("path")))
        else:
            meta_info = MetaInfo(
                title=info.get("title"),
                subtitle=info.get("subtitle"),
                custom_words=["#"],
            )
        target = {
            "type": meta_info.type.value,
            "cn_name": meta_info.cn_name or "",
            "en_name": meta_info.en_name or "",
            "year": meta_info.year or "",
            "part": meta_info.part or "",
            "season": meta_info.season,
            "episode": meta_info.episode,
            "restype": meta_info.edition,
            "pix": meta_info.resource_pix or "",
            "video_codec": meta_info.video_encode or "",
            "audio_codec": meta_info.audio_encode or "",
            "fps": meta_info.fps or None,
        }

        if info.get("target").get("tmdbid"):
            target["tmdbid"] = meta_info.tmdbid

        expected = info.get("target")
        if "fps" not in expected:
            target.pop("fps", None)
        assert target == expected


def test_emby_format_ids():
    """测试 Emby 格式 ID 识别。"""
    test_paths = [
        (
            "/movies/The Vampire Diaries (2009) [tmdbid=18165]/The.Vampire.Diaries.S01E01.1080p.mkv",
            18165,
        ),
        ("/movies/Inception (2010) [tmdbid-27205]/Inception.2010.1080p.mkv", 27205),
        (
            "/movies/Breaking Bad (2008) [tmdb=1396]/Season 1/Breaking.Bad.S01E01.1080p.mkv",
            1396,
        ),
        (
            "/tv/Game of Thrones (2011) {tmdb=1399}/Season 1/Game.of.Thrones.S01E01.1080p.mkv",
            1399,
        ),
        ("/movies/Avatar (2009) {tmdb-19995}/Avatar.2009.1080p.mkv", 19995),
    ]

    for path_str, expected_tmdbid in test_paths:
        meta = MetaInfoPath(Path(path_str))
        assert meta.tmdbid == expected_tmdbid, (
            f"路径 {path_str} 期望的tmdbid为 {expected_tmdbid}，实际识别为 {meta.tmdbid}"
        )


def test_metainfopath_with_custom_words():
    """测试 MetaInfoPath 使用自定义识别词。"""
    custom_words = ["测试替换 => "]
    path = Path("/movies/电影测试替换名称 (2024)/movie.mkv")
    meta = MetaInfoPath(path, custom_words=custom_words)
    if meta.cn_name:
        assert "测试替换" not in meta.cn_name


def test_metainfopath_without_custom_words():
    """测试 MetaInfoPath 不传入自定义识别词。"""
    path = Path("/movies/Normal Movie (2024)/movie.mkv")
    meta = MetaInfoPath(path)
    assert meta is not None


def test_metainfopath_with_empty_custom_words():
    """测试 MetaInfoPath 传入空的自定义识别词。"""
    path = Path("/movies/Test Movie (2024)/movie.mkv")
    meta = MetaInfoPath(path, custom_words=[])
    assert meta is not None


def test_custom_words_apply_words_recording():
    """测试 apply_words 记录功能。"""
    custom_words = ["替换词 => 新词"]
    title = "电影替换词.2024.mkv"
    meta = MetaInfo(title=title, custom_words=custom_words)
    assert hasattr(meta, "apply_words")
    if meta.apply_words:
        assert "替换词 => 新词" in meta.apply_words


def test_metainfo_preserves_original_name_when_custom_words_applied():
    """测试应用识别词后仍保留未应用识别词时识别出的名称。"""
    custom_words = ["测试替换 => "]
    meta = MetaInfo(title="电影测试替换名称 (2024)", custom_words=custom_words)
    assert meta.name == "电影名称"
    assert meta.original_name == "电影测试替换名称"


def test_custom_words_replace_then_episode_offset():
    """测试复杂识别词仍按先替换、后集数偏移的顺序处理。"""
    custom_words = ["旧名 => 新名 && 第 <> 集 >> EP+1"]
    meta = MetaInfo(title="旧名 第03集", custom_words=custom_words)
    assert meta.name == "新名"
    assert meta.episode == "E04"
    assert meta.apply_words == custom_words


def test_custom_words_episode_offset_supports_multiplication_expression():
    """测试集数偏移表达式支持乘法和连续运算。"""
    custom_words = [
        r"Ha.Ha.Ha.Ha.Ha.2026.S06E([0-1][0-9]).Part1 => 哈哈哈哈哈 (2020){[tmdbid=112732;type=tv]} S06E\1.Part1 && S06 <> .Part1 >> 2*EP-1"
    ]

    with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
        meta = MetaInfo(
            title="Ha.Ha.Ha.Ha.Ha.2026.S06E03.Part1",
            custom_words=custom_words,
        )

    assert meta.name == "哈哈哈哈哈"
    assert meta.tmdbid == 112732
    assert meta.begin_season == 6
    assert meta.episode == "E05"
    assert meta.apply_words == custom_words


def test_custom_words_episode_offset_supports_repeated_ep_expression():
    """测试集数偏移表达式支持重复使用 EP 占位符。"""
    custom_words = ["旧名 => 新名 && 第 <> 集 >> EP+EP-1"]

    with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
        meta = MetaInfo(title="旧名 第03集", custom_words=custom_words)

    assert meta.name == "新名"
    assert meta.episode == "E05"
    assert meta.apply_words == custom_words


def test_custom_words_episode_offset_rejects_implicit_ep_expression():
    """测试集数偏移表达式不把 2EP 当作隐式乘法或字符串拼接。"""
    custom_words = ["旧名 => 新名 && 第 <> 集 >> 2EP"]

    with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
        meta = MetaInfo(title="旧名 第03集", custom_words=custom_words)

    assert meta.name == "新名"
    assert meta.episode == "E03"
    assert meta.apply_words == []


def test_custom_words_support_episode_group_parameter():
    """测试自定义识别词替换结果中的 g 参数会写入剧集组。"""
    group_id = "5ad0ec240e0a26303f00d84d"
    custom_words = [
        f"Bakemonogatari => 物语系列 {{[tmdbid=46195;type=tv;g={group_id};s=1]}}"
    ]
    meta = MetaInfo(title="Bakemonogatari 01", custom_words=custom_words)
    assert meta.tmdbid == 46195
    assert meta.type.value == "电视剧"
    assert meta.begin_season == 1
    assert meta.episode_group == group_id
    assert meta.apply_words == custom_words


def test_custom_words_support_special_season_zero_parameter():
    """显式媒体标签中的 s=0 应作为特别季写入元数据。"""
    custom_words = [
        "Test Show => 测试剧 {[tmdbid=12345;type=tv;s=0]}"
    ]

    with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
        meta = MetaInfo(title="Test Show 01", custom_words=custom_words)

    assert meta.tmdbid == 12345
    assert meta.type.value == "电视剧"
    assert meta.begin_season == 0


def test_find_metainfo_supports_episode_group_parameter():
    """测试显式媒体标签支持 g 剧集组参数。"""
    group_id = "5ad0ec240e0a26303f00d84d"
    title, metainfo = find_metainfo(f"物语系列 {{[tmdbid=46195;type=tv;g={group_id};s=1]}}")
    assert metainfo["episode_group"] == group_id
    assert "g=" not in title


def test_find_metainfo_does_not_support_episode_group_alias():
    """测试 e_group 不会被当作剧集组参数识别。"""
    group_id = "5ad0ec240e0a26303f00d84d"
    with patch("app.core.metainfo.rust_accel.find_metainfo", return_value=None):
        _, metainfo = find_metainfo(f"物语系列 {{[tmdbid=46195;type=tv;e_group={group_id};s=1]}}")
    assert metainfo["episode_group"] is None


def test_video_bit_extracted_for_video_title():
    """测试普通影视标题中的视频位深可单独识别。"""
    meta = MetaInfo(title="The 355 2022 BluRay 1080p DTS-HD MA5.1 X265.10bit-BeiTai")
    assert meta.video_encode == "x265 10bit"
    assert meta.video_bit == "10bit"


def test_hdr_vivid_effect_extracted_for_video_title():
    """测试合并写法 HDRVivid 可识别为资源效果。"""
    with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
        meta = MetaInfo(
            title="Never-Ending Summer 2026 S01E18-S01E19 2160p WEB-DL 50Fps "
                  "HDRVivid H265 10bit AAC-XXWEB"
        )

    assert meta.resource_type == "WEB-DL"
    assert meta.resource_effect == "HDRVivid"
    assert meta.fps == 50


def test_video_bit_extracted_for_anime_title():
    """测试动漫标题中的视频位深可单独识别。"""
    meta = MetaInfo(
        title="[云歌字幕组][7月新番][欢迎来到实力至上主义的教室 第二季][01]"
              "[X264 10bit][1080p][简体中文].mp4"
    )
    assert meta.video_encode == "X264"
    assert meta.video_bit == "10bit"


def test_streaming_platform_word_kept_in_movie_title():
    """测试正式片名中的流媒体平台词不会被预置清理规则移除。"""
    with patch("app.core.metainfo.rust_accel.parse_metainfo", return_value=None):
        meta = MetaInfo(title="Amazon Forever 2004 1080p WEB-DL")
    assert meta.name == "Amazon Forever"
    assert meta.year == "2004"


def test_emby_tmdbid_overrides_braced_metainfo_tmdbid():
    """测试 Emby [tmdbid] 标签保持历史优先级。"""
    title, metainfo = find_metainfo("Movie {[tmdbid=111;type=movies]} [tmdbid=222]")
    assert metainfo["tmdbid"] == "222"
    assert "[tmdbid=222]" not in title


def test_metainfopath_auxiliary_chinese_stem_uses_parent_title():
    """测试辅助文件名合并父目录标题与年份。"""
    path = Path(
        "/Marty Supreme 2025 2160p DoVi HDR Atmos TrueHD 7.1 x265-PbK/简英双语特效.mp4"
    )
    meta = MetaInfoPath(path)
    assert meta.en_name == "Marty Supreme"
    assert meta.year == "2025"
    assert meta.original_name == "Marty Supreme"


def test_metainfopath_chinese_parent_not_replaced_by_auxiliary_rule():
    """测试纯中文父目录不触发辅助文件名规则。"""
    path = Path("/movies/流浪地球 (2023)/简体中字.mkv")
    meta = MetaInfoPath(path)
    assert meta.cn_name
    assert "简体" in meta.cn_name


def test_metainfopath_cn_title_containing_keyword_not_cleared():
    """测试中文片名包含辅助关键词子串时不应被清空。"""
    path = Path("/Some Movie 2024/粤语残片.mkv")
    meta = MetaInfoPath(path)
    assert "粤语残片" in meta.cn_name
