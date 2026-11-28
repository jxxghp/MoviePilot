"""
订阅与工作流附加过滤枚举短标签的规范化与种子标题匹配回归测试。
"""

import pytest

from app.application.torrent.download import TorrentHelper
from app.domain.context import TorrentInfo
from app.domain.torrent import normalize_filter_pattern, normalize_filter_payload


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("resolution", "4K", "4K|2160p|x2160"),
        ("resolution", " 4k ", "4K|2160p|x2160"),
        ("resolution", "1080p", "1080[pi]|x1080"),
        ("resolution", "720P", "720[pi]|x720"),
        ("quality", "蓝光原盘", "Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD"),
        ("quality", "BluRay", "Blu-?Ray"),
        ("quality", "UHD", "UHD|UltraHD"),
        ("quality", "WEB-DL", "WEB-?DL|WEB-?RIP"),
        ("quality", "H265", "[Hx].?265|HEVC"),
        ("quality", "H.264", "[Hx].?264|AVC"),
        ("effect", "杜比视界", r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+"),
        ("effect", "杜比全景声", r"Dolby[\s.]*\+?Atmos|Atmos"),
        ("effect", "HDR", r"[\s.]+HDR[\s.]+|HDR10|HDR10\+"),
        ("effect", "SDR", r"[\s.]+SDR[\s.]+"),
    ],
)
def test_normalize_filter_pattern_expands_ui_short_labels(field: str, raw: str, expected: str) -> None:
    """
    验证质量、分辨率、特效下拉短标签均展开为与前端一致的等价正则。

    :param field: 筛选字段名
    :param raw: 界面或客户端提交的短标签
    :param expected: 规范化后的正则表达式
    """
    assert normalize_filter_pattern(field, raw) == expected


@pytest.mark.parametrize(
    ("field", "custom_pattern"),
    [
        ("resolution", "4K|2160p|x2160"),
        ("resolution", "2160p"),
        ("quality", "WEB-DL.*H265"),
        ("effect", "HDR10\\+"),
        ("include", "4K"),
        ("exclude", "HDR"),
    ],
)
def test_normalize_filter_pattern_preserves_custom_regex_and_other_fields(
    field: str, custom_pattern: str
) -> None:
    """
    验证已展开表达式、用户自定义正则以及非枚举字段保持原样。

    :param field: 筛选字段名
    :param custom_pattern: 自定义正则表达式
    """
    assert normalize_filter_pattern(field, custom_pattern) == custom_pattern


@pytest.mark.parametrize(
    ("filter_params", "matching_title", "rejected_title", "expected_reason"),
    [
        (
            {"resolution": "4K"},
            "Fold.the.Moon.S01E12.2160p.WEB-DL.H265.AAC-Group",
            "Fold.the.Moon.S01E12.1080p.WEB-DL.H265.AAC-Group",
            "不符合分辨率条件：4K",
        ),
        (
            {"resolution": "1080p"},
            "Show.S01E01.1080i.HDTV.H264-Group",
            "Show.S01E01.720p.HDTV.H264-Group",
            "不符合分辨率条件：1080p",
        ),
        (
            {"quality": "WEB-DL"},
            "Show.S01E01.2160p.WEBRip.x265-Group",
            "Show.S01E01.2160p.HDTV.x265-Group",
            "不符合质量条件：WEB-DL",
        ),
        (
            {"quality": "H265"},
            "Show.S01E01.2160p.WEB-DL.HEVC-Group",
            "Show.S01E01.2160p.WEB-DL.x264-Group",
            "不符合质量条件：H265",
        ),
        (
            {"effect": "杜比视界"},
            "Fold.the.Moon.S01E01.2160p.WEB-DL.DV.Atmos.H265-Group",
            "Fold.the.Moon.S01E01.2160p.WEB-DL.SDR.H265-Group",
            "不符合特效条件：杜比视界",
        ),
        (
            {"effect": "杜比全景声"},
            "Fold.the.Moon.S01E01.2160p.WEB-DL.Dolby.Atmos.H265-Group",
            "Fold.the.Moon.S01E01.2160p.WEB-DL.AAC2.0.H265-Group",
            "不符合特效条件：杜比全景声",
        ),
        (
            {"effect": "HDR"},
            "Movie 1080p HDR",
            "Movie.1080p.HDRip.x264-Group",
            "不符合特效条件：HDR",
        ),
        (
            {"effect": r"[\s.]+HDR[\s.]+|HDR10|HDR10\+"},
            "Movie 2160p HDR",
            "Movie.2160p.SDR.WEB-DL",
            r"不符合特效条件：[\s.]+HDR[\s.]+|HDR10|HDR10\+",
        ),
        (
            {"effect": "杜比视界"},
            "Movie 2160p DV",
            "Movie.DVDRip.x264-Group",
            "不符合特效条件：杜比视界",
        ),
        (
            {"effect": "SDR"},
            "Movie 1080p SDR",
            "Movie.1080p.HDR10.WEB-DL",
            "不符合特效条件：SDR",
        ),
    ],
)
def test_torrent_helper_matches_short_ui_labels_against_real_torrent_titles(
    filter_params: dict[str, str],
    matching_title: str,
    rejected_title: str,
    expected_reason: str,
) -> None:
    """
    验证存量或第三方传入的短标签可匹配常见种子命名，不满足时仍给出准确拒绝原因。

    :param filter_params: 待测筛选条件
    :param matching_title: 应当通过筛选的种子标题
    :param rejected_title: 应当被拒绝的种子标题
    :param expected_reason: 预期返回的用户可见拒绝原因
    """
    accepted = TorrentInfo(title=matching_title)
    rejected = TorrentInfo(title=rejected_title)

    assert TorrentHelper.filter_torrent(accepted, filter_params) is True
    assert TorrentHelper.filter_torrent_reason(accepted, filter_params) is None
    assert TorrentHelper.filter_torrent(rejected, filter_params) is False
    assert TorrentHelper.filter_torrent_reason(rejected, filter_params) == expected_reason


def test_normalize_filter_payload_expands_only_nonempty_enum_fields() -> None:
    """
    验证写入载荷规范化仅展开非空枚举短标签，保留空值清空语义与自定义字段。
    """
    original = {
        "quality": "蓝光原盘",
        "resolution": "4K",
        "effect": None,
        "include": "4K",
    }
    normalized = normalize_filter_payload(original)

    assert original["resolution"] == "4K"
    assert normalized == {
        "quality": "Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD",
        "resolution": "4K|2160p|x2160",
        "effect": None,
        "include": "4K",
    }
