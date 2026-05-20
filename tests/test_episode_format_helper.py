from pathlib import Path

import pytest

from app.chain.transfer import TransferChain
from app.helper.format import FormatParser
from app.helper.episode_format import EpisodeFormatRuleHelper, _AutoRecommendSample
from app.schemas import EpisodeFormatRule, FileItem


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
        "app.helper.episode_format.settings.RMT_MEDIAEXT",
        [".mkv", ".mp4"],
    )


def test_rule_recommend_supports_range_episode_validation():
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
    helper = EpisodeFormatRuleHelper()

    assert helper._episode_matches(1, None, "01-E02") is False


def test_locate_episode_ignores_version_suffix_digits():
    helper = EpisodeFormatRuleHelper()
    file_name = "Show - 01 v2.mkv"

    start, end = helper._locate_episode(file_name, "01")

    assert file_name[start:end] == "01"
    assert file_name[start - 1] == " "


def test_locate_episode_supports_hash_prefix():
    helper = EpisodeFormatRuleHelper()
    file_name = "[AI-Raws] 不滅のあなたへ #01 (BD HEVC 1920x1080 yuv444p10le FLAC)[DE0EC3BA].mkv"

    start, end = helper._locate_episode(file_name, "01")

    assert file_name[start:end] == "01"
    assert file_name[start - 1] == "#"


def test_auto_recommend_returns_low_confidence_for_single_sample():
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
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show - 01 [1080p].mkv"),
        _make_file("Show - 02 [720p].mkv"),
    ]

    def _raise_parse(*args, **kwargs):
        raise ValueError("broken parse")

    monkeypatch.setattr("app.helper.episode_format.parse.parse", _raise_parse)

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
    helper = EpisodeFormatRuleHelper()

    assert helper._extract_episode_fallback(file_name) == expected


def test_auto_recommend_is_stable_when_sample_order_changes():
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


def test_auto_recommend_uses_native_episode_as_fallback(monkeypatch):
    helper = EpisodeFormatRuleHelper()
    samples = [
        _make_file("Show - 01.mkv"),
        _make_file("Show - 02.mkv"),
    ]

    monkeypatch.setattr(
        "app.helper.episode_format.anitopy.parse",
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


def test_auto_recommend_marks_native_verified_samples(monkeypatch):
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
        "_TransferChain__resolve_episode_format_directory",
        lambda item: directory,
    )
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_episode_format_rules",
        lambda: [],
    )
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_episode_format_sample_files",
        lambda item: [sample],
    )
    monkeypatch.setattr(
        "app.chain.transfer.EpisodeFormatRuleHelper.recommend",
        lambda self, rules, sample_files: (True, "", helper_data),
    )

    state, errmsg, data = TransferChain.recommend_episode_format(chain, file_item)

    assert state is True
    assert errmsg == ""
    assert data == helper_data


def test_transfer_chain_episode_format_samples_include_extra_files(monkeypatch):
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
        "app.chain.transfer.StorageChain.list_files",
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

    sample_files = TransferChain._TransferChain__get_episode_format_sample_files(
        chain,
        directory,
    )

    assert [item.name for item in sample_files] == [
        media_item.name,
        subtitle_item.name,
        audio_item.name,
    ]
