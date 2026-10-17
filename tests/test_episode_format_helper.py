"""
验证剧集定位模板推荐、样本筛选、原生识别校验及整理链参数传递。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.application.formatting import EpisodeFormatRuleHelper, FormatParser, _AutoRecommendSample
from app.chain.transfer.facade import TransferChain
from app.schemas.file import FileItem
from app.schemas.transfer import EpisodeFormatRule


def _make_file(name: str, size: int = 150 * 1024 * 1024) -> FileItem:
    suffix = Path(name).suffix.lstrip(".")
    return FileItem(
        storage="local",
        path=f"/downloads/{name}",
        type="file",
        name=name,
        extension=suffix,
        size=size,
    )


@pytest.fixture(autouse=True)
def _patch_media_exts(monkeypatch):
    monkeypatch.setattr(
        "app.application.formatting.get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(
            video_extensions=(".mkv", ".mp4"),
            subtitle_extensions=(".srt", ".ass"),
            audio_extensions=(".flac", ".mp3", ".mka"),
        ),
    )


@pytest.mark.parametrize(
    ("offset", "expected"),
    [("-1", 2), ("EP*2-1", 5), ("EP+EP-1", 5)],
)
def test_format_parser_applies_safe_episode_offset(offset: str, expected: int):
    """
    剧集偏移仅计算白名单算术表达式，并保留现有偏移语义。
    """
    parser = FormatParser(eformat="", offset=offset)
    file_meta = SimpleNamespace(begin_episode=3, end_episode=3)

    assert parser.split_episode("", file_meta) == (expected, expected, None)


def test_format_parser_applies_safe_episode_offset_to_template_episode():
    """
    带定位模板时也应使用白名单计算集数偏移。
    """
    parser = FormatParser(eformat="Show - {ep}.mkv", offset="EP*2-1")
    file_meta = SimpleNamespace(begin_episode=None, end_episode=None)

    assert parser.split_episode("Show - 03.mkv", file_meta) == (5, None, None)


def test_format_parser_applies_safe_episode_offset_to_explicit_range():
    """
    显式集数区间的起止集数都应使用安全偏移计算。
    """
    parser = FormatParser(eformat="", details="03-04", offset="EP+1")
    file_meta = SimpleNamespace(begin_episode=None, end_episode=None)

    assert parser.split_episode("", file_meta) == (4, 5, None)


def test_format_parser_rejects_code_execution_in_episode_offset():
    """
    剧集偏移中的函数调用必须被拒绝，不能执行系统命令。
    """
    parser = FormatParser(
        eformat="",
        offset="EP+__import__('os').system('touch /tmp/moviepilot-format-parser-rce')*0",
    )
    file_meta = SimpleNamespace(begin_episode=1, end_episode=1)

    with patch("os.system") as system_call, pytest.raises(ValueError):
        parser.split_episode("", file_meta)

    system_call.assert_not_called()


def test_rule_recommend_supports_range_episode_validation():
    """
    自定义规则推荐支持对连续剧集范围进行完整校验。
    """
    helper = EpisodeFormatRuleHelper()
    rule = EpisodeFormatRule(
        name="区间规则",
        pattern=r"Show\.S01E(?<ep>\d{2}-E\d{2})\.mkv",
    )
    sample = _make_file("Show.S01E01-E02.mkv")

    state, errmsg, data = helper.recommend([rule], [sample])

    assert state is True
    assert errmsg == ""
    assert data["episode_format"] == "Show.S01E{ep}.mkv"


def test_episode_matches_rejects_missing_range_end():
    """
    候选只识别出起始集时，不能通过多集范围匹配。
    """
    helper = EpisodeFormatRuleHelper()

    assert helper._episode_matches(1, None, "01-E02") is False


def test_locate_episode_ignores_version_suffix_digits():
    """
    定位集数时忽略版本后缀中的数字。
    """
    helper = EpisodeFormatRuleHelper()
    file_name = "Show - 01 v2.mkv"

    start, end = helper._locate_episode(file_name, "01")

    assert file_name[start:end] == "01"
    assert file_name[start - 1] == " "


def test_locate_episode_supports_hash_prefix():
    """
    集数定位支持带井号前缀的编号。
    """
    helper = EpisodeFormatRuleHelper()
    file_name = "[AI-Raws] 不滅のあなたへ #01 (BD HEVC 1920x1080 yuv444p10le FLAC)[DE0EC3BA].mkv"

    start, end = helper._locate_episode(file_name, "01")

    assert file_name[start:end] == "01"
    assert file_name[start - 1] == "#"


@pytest.mark.parametrize(
    "file_name",
    [
        "Show 01《Title》.mkv",
        "Show 01〈Title〉.mkv",
        "Show 01〔Title〕.mkv",
    ],
)
def test_locate_episode_supports_east_asian_title_quotes(file_name: str):
    """
    集数定位兼容东亚书名和标题引号。
    """
    helper = EpisodeFormatRuleHelper()

    start, end = helper._locate_episode(file_name, "01")

    assert file_name[start:end] == "01"


def test_auto_recommend_supports_episode_before_japanese_quote():
    """
    自动推荐支持日文引号前的集数，并将变化标题提取为变量。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("[U2-Rip]バカとテストと召喚獣 01「バカとクラスと召喚戦爭」(BD 1920x1080 x264 FLACx2).mkv"),
        _make_file("[U2-Rip]バカとテストと召喚獣 02「ユリとバラと保健体育」(BD 1920x1080 x264 FLACx2).mkv"),
        _make_file("[U2-Rip]バカとテストと召喚獣 03「食費とデートとスタンガン」(BD 1920x1080 x264 FLACx2).mkv"),
    ]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["episode_format"] == "[U2-Rip]バカとテストと召喚獣 {ep}「{a}」(BD 1920x1080 x264 FLACx2).mkv"
    assert data["sample_count"] == 3
    assert data["majority_count"] == 3


def test_auto_recommend_returns_low_confidence_for_single_sample():
    """
    只有一个有效样本时可以生成模板，但应报告低置信度。
    """
    helper = EpisodeFormatRuleHelper()
    sample = _make_file("[Seed-Raws] Tari Tari - 01 (BD 1280x720 AVC AAC).mp4")

    state, errmsg, data = helper.recommend([], [sample])

    assert state is True
    assert errmsg == ""
    assert data["confidence"] == "low"
    assert data["sample_count"] == 1
    assert data["majority_count"] == 1
    assert data["reason"] == "single_sample_only"
    assert "single_sample_only" in data["reasons"]
    assert "单文件" in data["message"]


def test_auto_recommend_relaxes_size_filter_for_small_media():
    """
    小体积媒体缺少足够样本时放宽大小过滤，并降低置信度。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show.S01E01.mkv", size=40 * 1024 * 1024),
        _make_file("Show.S01E02.mkv", size=42 * 1024 * 1024),
    ]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["size_filter_relaxed"] is True
    assert data["confidence"] == "low"
    assert "small_files_fallback" in data["reasons"]


def test_auto_recommend_rejects_without_clear_majority():
    """
    样本命名没有明确多数模式时拒绝自动生成模板。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("[A] Show [01].mkv"),
        _make_file("[B] Show [02].mkv"),
    ]

    state, errmsg, data = helper.recommend([], samples)

    assert state is False
    assert errmsg == "样本命名差异过大，建议补充集数定位规则"
    assert data is None


def test_validate_auto_template_checks_expected_episode_consistency():
    """
    模板校验必须匹配样本预期集数，不能只验证文件名形状。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _AutoRecommendSample(
            file_name="Show - 01.mkv",
            ep_span=(7, 9),
            expected_episode="02",
        )
    ]

    assert helper._validate_auto_template("Show - {ep}.mkv", samples) is False


def test_insert_variable_placeholder_preserves_existing_placeholders():
    """
    插入新变量占位符时保留已有占位符。
    """
    helper = EpisodeFormatRuleHelper()
    all_file_names = [
        "Show - 01 [x265_flac][1080p].mkv",
        "Show - 02 [x265_flac][720p].mkv",
    ]
    after_ep_list = [
        " [x265_flac][1080p].mkv",
        " [x265_flac][720p].mkv",
    ]
    template = "Show - {ep} [x265{a}][1080p].mkv"

    updated = helper._insert_variable_placeholder(
        template=template,
        failed_files=[all_file_names[1]],
        after_ep_list=after_ep_list,
        all_file_names=all_file_names,
        placeholder="b",
    )

    assert "{a}" in updated
    assert "{b}" in updated


def test_insert_variable_placeholder_does_not_double_escape_braces():
    """
    变量替换不得对已经转义的花括号重复转义。
    """
    helper = EpisodeFormatRuleHelper()
    all_file_names = [
        "Show - 01 {v1}.mkv",
        "Show - 02 {v2}.mkv",
    ]
    after_ep_list = [
        " {v1}.mkv",
        " {v2}.mkv",
    ]
    template = "Show - {ep} {{v1}}.mkv"

    updated = helper._insert_variable_placeholder(
        template=template,
        failed_files=[all_file_names[1]],
        after_ep_list=after_ep_list,
        all_file_names=all_file_names,
        placeholder="a",
    )

    assert "{{{{" not in updated
    assert "{a}" in updated


def test_auto_recommend_validates_all_majority_samples():
    """
    生成模板后必须校验所有多数模式样本，不能忽略失败样本。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file(f"Show - {index:02d}.mkv")
        for index in range(1, 11)
    ]
    samples.append(_make_file("Show - 11 [WEB].mkv"))

    state, errmsg, data = helper.recommend([], samples)

    assert state is False
    assert errmsg == "无匹配自定义定位规则，智能生成失败"
    assert data is None


def test_auto_recommend_returns_false_when_parse_raises(monkeypatch):
    """
    模板解析抛出异常时返回失败结果及说明。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show - 01 [1080p].mkv"),
        _make_file("Show - 02 [720p].mkv"),
    ]

    def _raise_parse(*args, **kwargs):
        raise ValueError("broken parse")

    monkeypatch.setattr("app.application.formatting._match_template", _raise_parse)

    state, errmsg, data = helper.recommend([], samples)

    assert state is False
    assert errmsg == "无匹配自定义定位规则，智能生成失败"
    assert data is None


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("Show 第01話.mkv", "01"),
        ("Show 第01话.mkv", "01"),
        ("Show 第01集.mkv", "01"),
        ("Show。01 1080p.mkv", "01"),
    ],
)
def test_extract_episode_fallback_supports_cjk_patterns(file_name: str, expected: str):
    """
    集数回退提取支持中日韩命名格式。
    """
    helper = EpisodeFormatRuleHelper()

    assert helper._extract_episode_fallback(file_name) == expected


def test_auto_recommend_is_stable_when_sample_order_changes():
    """
    调整样本输入顺序不应改变自动推荐结果。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show - 01 [1080p].mkv"),
        _make_file("Show - 02 [720p].mkv"),
        _make_file("Show - 03 [1080p].mkv"),
    ]

    state1, errmsg1, data1 = helper.recommend([], samples)
    state2, errmsg2, data2 = helper.recommend([], list(reversed(samples)))

    assert state1 is True
    assert errmsg1 == ""
    assert state2 is True
    assert errmsg2 == ""
    assert data1["episode_format"] == data2["episode_format"]
    assert data1["sample_file"] == data2["sample_file"]
    assert data1["sample_count"] == data2["sample_count"]
    assert data1["majority_count"] == data2["majority_count"]


def test_auto_recommend_supports_hash_episode_names():
    """
    多个井号集数样本可生成高置信度模板。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("[AI-Raws] 不滅のあなたへ #01 (BD HEVC 1920x1080 yuv444p10le FLAC)[DE0EC3BA].mkv"),
        _make_file("[AI-Raws] 不滅のあなたへ #02 (BD HEVC 1920x1080 yuv444p10le FLAC)[8CE75F1B].mkv"),
        _make_file("[AI-Raws] 不滅のあなたへ #03 (BD HEVC 1920x1080 yuv444p10le FLAC)[986E42F9].mkv"),
    ]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["confidence"] == "high"
    assert data["sample_count"] == 3
    assert data["majority_count"] == 3
    assert data["episode_format"] == "[AI-Raws] 不滅のあなたへ #{ep} (BD HEVC 1920x1080 yuv444p10le FLAC)[{a}].mkv"


def test_auto_recommend_ignores_episode_zero_specials():
    """
    第零集特别篇不参与正片模板的多数模式统计。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("[VCB] Show [00][1080p][x264_2flac].mkv"),
        _make_file("[VCB] Show [01][1080p][x264_2flac].mkv"),
        _make_file("[VCB] Show [02][1080p][x264_2flac].mkv"),
        _make_file("[VCB] Show [12][1080p][x264_3flac].mkv"),
    ]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["sample_count"] == 3
    assert data["majority_count"] == 3
    assert data["episode_format"] == "[VCB] Show [{ep}][1080p][x264_{a}flac].mkv"


def test_auto_recommend_validates_media_subtitle_and_audio_together():
    """
    媒体、字幕和音轨样本共同参与模板验证。
    """
    helper = EpisodeFormatRuleHelper()
    sample_names = [
        "Show - 01.mkv",
        "Show - 02.mkv",
        "Show - 01.ass",
        "Show - 02.ass",
        "Show - 01.mka",
        "Show - 02.mka",
    ]
    samples = [_make_file(name) for name in sample_names]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["sample_count"] == 6
    parser = FormatParser(eformat=data["episode_format"])
    for sample_name in sample_names:
        assert parser.match(sample_name) is True


def test_auto_recommend_ignores_non_integer_episode_samples():
    """
    非整数集数样本不纳入正片模板统计。
    """
    helper = EpisodeFormatRuleHelper()
    sample_names = [
        "[T.H.X&VCB-Studio] Hyouka [01][Ma10p_1080p][x265_flac_aac].mkv",
        "[T.H.X&VCB-Studio] Hyouka [02][Ma10p_1080p][x265_flac_aac].mkv",
        "[T.H.X&VCB-Studio] Hyouka [01][Ma10p_1080p][x265_flac_aac].chs.ass",
        "[T.H.X&VCB-Studio] Hyouka [02][Ma10p_1080p][x265_flac_aac].chs.ass",
        "[T.H.X&VCB-Studio] Hyouka [01][Ma10p_1080p][x265_flac_aac].cht.ass",
        "[T.H.X&VCB-Studio] Hyouka [02][Ma10p_1080p][x265_flac_aac].cht.ass",
        "[T.H.X&VCB-Studio] Hyouka [11.5][Ma10p_1080p][x265_flac_aac].mkv",
        "[T.H.X&VCB-Studio] Hyouka [11.5][Ma10p_1080p][x265_flac_aac].chs.ass",
        "[T.H.X&VCB-Studio] Hyouka [11.5][Ma10p_1080p][x265_flac_aac].cht.ass",
    ]
    samples = [_make_file(name) for name in sample_names]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["sample_count"] == 6
    assert data["majority_count"] == 6
    assert data["episode_format"] == (
        "[T.H.X&VCB-Studio] Hyouka [{ep}][Ma10p_1080p][x265_flac_aac].{a}"
    )


def test_auto_recommend_ignores_special_sp_samples():
    """
    SP 特别篇样本不干扰正片模板推荐。
    """
    helper = EpisodeFormatRuleHelper()
    sample_names = [
        "[Tonikaku Kawaii S2][01][BDRIP][1080P][H264_FLAC].mkv",
        "[Tonikaku Kawaii S2][02][BDRIP][1080P][H264_FLAC].mkv",
        "[Tonikaku Kawaii S2][03][BDRIP][1080P][H264_FLAC].mkv",
        "[Tonikaku Kawaii S2][BD-BOX][Disc 02][SP01][NCOP Ver.1][BDRIP][1080P][H264_FLAC].mkv",
        "[Tonikaku Kawaii S2][BD-BOX][Disc 02][SP03][NCED Ver.1][BDRIP][1080P][H264_FLAC].mkv",
        "[Tonikaku Kawaii S2][BD-BOX][Disc 02][SP04][NCED Ver.2][BDRIP][1080P][H264_FLAC].mkv",
    ]
    samples = [_make_file(name) for name in sample_names]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["sample_count"] == 3
    assert data["majority_count"] == 3
    assert data["episode_format"] == "[Tonikaku Kawaii S2][{ep}][BDRIP][1080P][H264_FLAC].mkv"


def test_auto_recommend_ignores_promotional_special_samples():
    """
    宣传与特别内容样本不干扰正片模板推荐。
    """
    helper = EpisodeFormatRuleHelper()
    sample_names = [
        "[VCB-Studio] Show [01][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [02][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [03][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [PV01][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [CM01][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [Trailer][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [Web Preview 02][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [Series Review][Ma10p_1080p][x265_flac].mkv",
        "[VCB-Studio] Show [Table Game][Ma10p_1080p][x265_flac].mkv",
    ]
    samples = [_make_file(name) for name in sample_names]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["sample_count"] == 3
    assert data["majority_count"] == 3
    assert data["episode_format"] == "[VCB-Studio] Show [{ep}][Ma10p_1080p][x265_flac].mkv"


def test_auto_recommend_rejects_when_valid_media_coverage_is_too_low():
    """
    有效正片样本覆盖率不足时拒绝自动模板。
    """
    helper = EpisodeFormatRuleHelper()
    sample_names = [
        "Show - 01.mkv",
        "Show - 02.mkv",
        "Show bonus.mkv",
        "Show extra.mkv",
    ]
    samples = [_make_file(name) for name in sample_names]

    state, errmsg, data = helper.recommend([], samples)

    assert state is False
    assert errmsg == "有效正片样本覆盖率不足，建议补充集数定位规则"
    assert data is None


def test_auto_recommend_uses_native_episode_as_fallback(monkeypatch):
    """
    模板识别不足时允许使用原生集数结果补充样本。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show - 01.mkv"),
        _make_file("Show - 02.mkv"),
    ]

    monkeypatch.setattr(
        "app.application.formatting.anitopy.parse",
        lambda _: {},
    )
    monkeypatch.setattr(
        helper,
        "_extract_episode_fallback",
        lambda _: None,
    )
    monkeypatch.setattr(
        helper,
        "_extract_native_episode",
        lambda item: "01" if item.name.endswith("01.mkv") else "02",
    )

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["native_fallback_count"] == 2
    assert data["native_verified_count"] == 0
    assert "native_meta_fallback" in data["reasons"]
    assert data["episode_format"] == "Show - {ep}.mkv"


def test_auto_recommend_rejects_when_native_episode_conflicts(monkeypatch):
    """
    模板与可信原生集数识别冲突时拒绝推荐。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show - 01.mkv"),
        _make_file("Show - 02.mkv"),
    ]

    native_values = iter(["02", "02"])
    monkeypatch.setattr(
        helper,
        "_extract_native_episode",
        lambda item: "02",
    )

    state, errmsg, data = helper.recommend([], samples)

    assert state is False
    assert errmsg == "样本命名与原生识别结果冲突，建议补充集数定位规则"
    assert data is None


def test_auto_recommend_degrades_native_conflict_for_title_sequence_number(monkeypatch):
    """
    原生识别误命中标题序号时降低其冲突权重。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("[VCB-Studio] Getsuyoubi no Tawawa 2 [01][Ma10p_1080p][x265_flac].mkv"),
        _make_file("[VCB-Studio] Getsuyoubi no Tawawa 2 [02][Ma10p_1080p][x265_flac].mkv"),
    ]

    monkeypatch.setattr(
        helper,
        "_extract_native_episode",
        lambda item: "2",
    )

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["native_conflict_count"] == 0
    assert data["episode_format"] == (
        "[VCB-Studio] Getsuyoubi no Tawawa 2 [{ep}][Ma10p_1080p][x265_flac].mkv"
    )


def test_auto_recommend_prefers_bracket_episode_over_title_sequence_native(monkeypatch):
    """
    括号中的真实集数优先于原生识别误取的标题序号。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 [01][Ma10p_1080p][x265_flac_aac].mkv"),
        _make_file("[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 [02][Ma10p_1080p][x265_flac_aac].mkv"),
    ]

    monkeypatch.setattr(
        "app.application.formatting.anitopy.parse",
        lambda _: {},
    )
    monkeypatch.setattr(
        helper,
        "_extract_native_episode",
        lambda item: "3",
    )

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["native_conflict_count"] == 0
    assert data["episode_format"] == (
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 [{ep}][Ma10p_1080p][x265_flac_aac].mkv"
    )


def test_auto_recommend_corrects_anitopy_title_sequence_bias(monkeypatch):
    """
    自动推荐纠正 anitopy 对标题序号的偏差，并保留真实多数样本。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file(f"[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 [{index:02d}][Ma10p_1080p][x265_flac_aac].mkv")
        for index in range(1, 12)
    ]

    def _mock_parse(file_name: str):
        episode_text = file_name.split("[", 2)[2].split("]", 1)[0]
        if episode_text in {"01", "02"}:
            return {"episode_number": episode_text}
        return {"episode_number": "3"}

    monkeypatch.setattr(
        "app.application.formatting.anitopy.parse",
        _mock_parse,
    )
    monkeypatch.setattr(
        helper,
        "_extract_native_episode",
        lambda item: "3",
    )

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["majority_count"] == 11
    assert data["episode_format"] == (
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 [{ep}][Ma10p_1080p][x265_flac_aac].mkv"
    )


def test_auto_recommend_supports_optional_finale_suffix():
    """
    自动模板支持只在部分样本出现的完结后缀。
    """
    helper = EpisodeFormatRuleHelper()
    sample_names = [
        "[SHIGURE] Rakudai Kishi no Cavalry - 01 [1080p][AVC_Hi10p_FLAC].mkv",
        "[SHIGURE] Rakudai Kishi no Cavalry - 02 [1080p][AVC_Hi10p_FLAC].mkv",
        "[SHIGURE] Rakudai Kishi no Cavalry - 12 Fin [1080p][AVC_Hi10p_FLAC].mkv",
    ]
    samples = [_make_file(name) for name in sample_names]

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["episode_format"] == (
        "[SHIGURE] Rakudai Kishi no Cavalry - {ep} {a}[1080p][AVC_Hi10p_FLAC].mkv"
    )
    parser = FormatParser(eformat=data["episode_format"])
    for sample_name in sample_names:
        assert parser.match(sample_name) is True


def test_should_prefer_fallback_episode_when_anitopy_hits_title_sequence():
    """
    anitopy 命中标题序号时优先使用后续真实集数的回退结果。
    """
    helper = EpisodeFormatRuleHelper()

    assert helper._should_prefer_fallback_episode(
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 [04][Ma10p_1080p][x265_flac_aac].mkv",
        "3",
        "04",
    ) is True
    assert helper._should_prefer_fallback_episode(
        "Show - 04.mkv",
        "04",
        "04",
    ) is False


def test_should_prefer_fallback_episode_preserves_anitopy_multi_episode_list():
    """
    原生识别返回多集列表时，不以单个回退集数覆盖。
    """
    helper = EpisodeFormatRuleHelper()

    assert helper._should_prefer_fallback_episode(
        "Show 3 [01][02].mkv",
        ["01", "02"],
        "02",
    ) is False


def test_extract_episode_with_native_fallback_keeps_anitopy_range_list(monkeypatch):
    """
    原生多集列表归一化为范围，并保留未使用回退的标志。
    """
    helper = EpisodeFormatRuleHelper()
    item = _make_file("Show - 01-02 [02].mkv")

    monkeypatch.setattr(
        "app.application.formatting.anitopy.parse",
        lambda _: {"episode_number": ["01", "02"]},
    )
    monkeypatch.setattr(
        helper,
        "_extract_native_episode",
        lambda _: None,
    )

    normalized_episode, native_episode, used_native_fallback, native_verified = (
        helper._extract_episode_with_native_fallback(item)
    )

    assert normalized_episode == "01-02"
    assert native_episode is None
    assert used_native_fallback is False
    assert native_verified is False


def test_should_degrade_native_conflict_only_for_preceding_title_number():
    """
    仅对位于真实集数前方的标题数字降低原生冲突权重。
    """
    helper = EpisodeFormatRuleHelper()

    assert helper._should_degrade_native_conflict(
        "[VCB-Studio] Getsuyoubi no Tawawa 2 [01][Ma10p_1080p][x265_flac].mkv",
        "01",
        "2",
    ) is True
    assert helper._should_degrade_native_conflict(
        "Show - 01.mkv",
        "01",
        "02",
    ) is False


def test_calculate_variable_span_keeps_optional_base_suffix_span():
    """
    可选基础后缀在其他样本缺失时仍保留完整变量区间。
    """
    helper = EpisodeFormatRuleHelper()

    assert helper._calculate_variable_span(" Fin ", [""]) == (0, 5)


def test_auto_recommend_marks_native_verified_samples(monkeypatch):
    """
    自动推荐分别统计原生校验通过与原生回退的样本数量。
    """
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show.S01E01.mkv"),
        _make_file("Show.S01E02.mkv"),
    ]

    monkeypatch.setattr(
        helper,
        "_extract_native_episode",
        lambda item: "01" if item.name.endswith("01.mkv") else "02",
    )

    state, errmsg, data = helper.recommend([], samples)

    assert state is True
    assert errmsg == ""
    assert data["native_verified_count"] == 2
    assert data["native_fallback_count"] == 0
    assert "native_meta_verified" in data["reasons"]


def test_transfer_chain_recommend_episode_format_passes_helper_data(monkeypatch):
    """
    整理链保留剧集模板辅助服务返回的数据和状态。
    """
    chain = object.__new__(TransferChain)
    file_item = FileItem(
        storage="local",
        path="/downloads/Show/Show - 01.mkv",
        type="file",
        name="Show - 01.mkv",
        extension="mkv",
    )
    directory = FileItem(
        storage="local",
        path="/downloads/Show",
        type="dir",
        name="Show",
    )
    sample = _make_file("Show - 01.mkv")
    helper_data = {
        "rule_name": "智能分析",
        "episode_format": "Show - {ep}.mkv",
        "sample_file": "Show - 01.mkv",
        "pattern": None,
        "sample_count": 1,
        "majority_count": 1,
        "confidence": "low",
        "size_filter_relaxed": False,
        "native_verified_count": 0,
        "native_fallback_count": 0,
        "native_conflict_count": 0,
        "reason": "single_sample_only",
        "reasons": ["single_sample_only"],
        "message": "样本不足，仅基于单文件智能生成（仅供参考）",
    }

    monkeypatch.setattr(
        chain,
        "_resolve_episode_format_directory",
        lambda item: directory,
    )
    monkeypatch.setattr(
        chain,
        "_get_episode_format_rules",
        lambda: [],
    )
    monkeypatch.setattr(
        chain,
        "_get_episode_format_sample_files",
        lambda item: [sample],
    )
    monkeypatch.setattr(
        "app.chain.transfer.format.EpisodeFormatRuleHelper.recommend",
        lambda self, rules, sample_files: (True, "", helper_data),
    )

    state, errmsg, data = TransferChain.recommend_episode_format(chain, file_item)

    assert state is True
    assert errmsg == ""
    assert data == helper_data


def test_transfer_chain_recommend_episode_format_uses_selected_fileitems(monkeypatch):
    """
    显式选择文件时，模板推荐使用这些文件作为样本。
    """
    chain = object.__new__(TransferChain)
    monkeypatch.setattr(chain, "_media_exts", [".mkv", ".mp4"], raising=False)
    monkeypatch.setattr(chain, "_subtitle_exts", [".ass", ".ssa"], raising=False)
    monkeypatch.setattr(chain, "_audio_exts", [".mka", ".aac"], raising=False)
    selected_fileitems = [
        FileItem(
            storage="local",
            path="/downloads/Show/Show - 01.mkv",
            type="file",
            name="Show - 01.mkv",
            extension="mkv",
        ),
        FileItem(
            storage="local",
            path="/downloads/Show/Show - 01.ass",
            type="file",
            name="Show - 01.ass",
            extension="ass",
        ),
    ]
    helper_data = {
        "rule_name": "智能分析",
        "episode_format": "Show - {ep}.{a}",
        "sample_file": "Show - 01.mkv",
        "pattern": None,
        "sample_count": 2,
        "majority_count": 2,
        "confidence": "high",
        "size_filter_relaxed": False,
        "native_verified_count": 0,
        "native_fallback_count": 0,
        "native_conflict_count": 0,
        "reason": "selected_samples",
        "reasons": ["selected_samples"],
        "message": "已基于选中文件生成集数定位模板",
    }

    monkeypatch.setattr(
        chain,
        "_get_episode_format_rules",
        lambda: [],
    )
    monkeypatch.setattr(
        "app.chain.transfer.format.EpisodeFormatRuleHelper.recommend",
        lambda self, rules, sample_files: (True, "", {
            **helper_data,
            "received_samples": [item.name for item in sample_files],
        }),
    )

    state, errmsg, data = TransferChain.recommend_episode_format(
        chain,
        fileitem=None,
        fileitems=selected_fileitems,
    )

    assert state is True
    assert errmsg == ""
    assert data["received_samples"] == [item.name for item in selected_fileitems]


def test_transfer_chain_recommend_episode_format_rejects_invalid_selected_fileitems():
    """
    选中的文件不满足智能识别条件时拒绝模板推荐。
    """
    chain = object.__new__(TransferChain)
    chain._media_exts = [".mkv", ".mp4"]
    chain._subtitle_exts = [".ass", ".ssa"]
    chain._audio_exts = [".mka", ".aac"]
    selected_fileitems = [
        FileItem(
            storage="local",
            path="/downloads/Show/Show - 01.mkv",
            type="file",
            name="Show - 01.mkv",
            extension="mkv",
        ),
        FileItem(
            storage="local",
            path="/downloads/Other/Show - 02.mkv",
            type="file",
            name="Show - 02.mkv",
            extension="mkv",
        ),
    ]

    state, errmsg, data = TransferChain.recommend_episode_format(
        chain,
        fileitem=None,
        fileitems=selected_fileitems,
    )

    assert state is False
    assert errmsg == "当前选择不满足智能识别条件"
    assert data is None


def test_transfer_chain_episode_format_samples_include_extra_files(monkeypatch):
    """
    整理链模板样本包含相关字幕和音轨文件。
    """
    chain = object.__new__(TransferChain)
    directory = FileItem(
        storage="local",
        path="/downloads/Show",
        type="dir",
        name="Show",
    )
    media_item = _make_file("Show - 01.mkv")
    subtitle_item = _make_file("Show - 01.ass")
    audio_item = _make_file("Show - 01.mka")
    other_item = _make_file("Show - 01.txt")

    monkeypatch.setattr(
        "app.chain.transfer.request.StorageChain.list_files",
        lambda self, item, recursion=False: [
            media_item,
            subtitle_item,
            audio_item,
            other_item,
        ],
    )
    monkeypatch.setattr(chain, "_media_exts", [".mkv", ".mp4"], raising=False)
    monkeypatch.setattr(chain, "_subtitle_exts", [".ass", ".ssa"], raising=False)
    monkeypatch.setattr(chain, "_audio_exts", [".mka", ".aac"], raising=False)

    sample_files = TransferChain._get_episode_format_sample_files(
        chain,
        directory,
    )

    assert [item.name for item in sample_files] == [
        media_item.name,
        subtitle_item.name,
        audio_item.name,
    ]
