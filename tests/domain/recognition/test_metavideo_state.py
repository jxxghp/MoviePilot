"""
验证影视元数据解析的历史词元状态与最终结果边界。
"""

import pytest

from app.domain.meta.metavideo import MetaVideo


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "Show S01E01 2026 1080p AMZN WEB DL H 265 10bit DDP 5 1 60FPS",
            {
                "begin_season": 1,
                "begin_episode": 1,
                "resource_type": "WEB-DL",
                "web_source": "Amazon",
                "video_encode": "H265 10bit",
                "video_bit": "10bit",
                "audio_encode": "DDP 5.1",
                "fps": 60,
            },
        ),
        (
            "Movie 2026 2160p UHD Blu Ray REMUX HEVC DTS HD MA 5 1",
            {
                "begin_season": None,
                "begin_episode": None,
                "resource_type": "UHD BluRay REMUX",
                "web_source": None,
                "video_encode": "HEVC",
                "video_bit": None,
                "audio_encode": "DTS-HD MA 5.1",
                "fps": None,
            },
        ),
    ],
)
def test_metavideo_preserves_cross_token_transitions(title, expected):
    """
    拆分资源和编码词元应通过显式历史状态连续识别。
    """
    meta = MetaVideo(title)

    actual = {field: getattr(meta, field) for field in expected}
    assert actual == expected


def test_metavideo_keeps_transient_token_state_out_of_result_model():
    """
    解析结果只承载媒体元数据，不泄漏词元管线的临时控制状态。
    """
    meta = MetaVideo("Show S01E01 2026 1080p WEB DL H 265 AAC 2 0")

    transient_fields = {
        "_continue_flag",
        "_effect",
        "_index",
        "_last_token",
        "_last_token_type",
        "_sources",
        "_stop_cnname_flag",
        "_stop_name_flag",
        "_unknown_name_str",
    }
    assert transient_fields.isdisjoint(vars(meta))


@pytest.mark.parametrize(
    ("title", "expected_season", "expected_ep", "expected_end"),
    [
        ("Mobile.Suit.Gundam.00.S01E01.2007.1080p.BluRay.x265.FLAC.2.0-FROGE", 1, 1, None),
        ("Mobile.Suit.Gundam.00.S01E25.2007.1080p.BluRay.x265.FLAC.2.0-FROGE", 1, 25, None),
        ("Mobile.Suit.Gundam.00.S02E24.2007.1080p.BluRay.x265.FLAC.2.0-FROGE", 2, 24, None),
        ("The.100.S02E05.1080p.WEB-DL.H264", 2, 5, None),
        ("Brooklyn.99.S05E12.1080p.WEB-DL.H264", 5, 12, None),
        ("Show.00.S01E01.S01E02.1080p", 1, 1, 2),
        ("Show.01.1080p.WEB-DL", 1, 1, None),
    ],
)
def test_explicit_season_episode_overrides_prior_bare_digit_guess(
    title: str,
    expected_season: int | None,
    expected_ep: int | None,
    expected_end: int | None,
) -> None:
    """
    片名中的裸数字（如 Gundam.00 / The.100 / Brooklyn.99）先误触发集号猜测时，
    后续显式 SxxEyy 必须覆盖前置猜测，而纯裸数字单集场景保持兼容。
    """
    meta = MetaVideo(title, isfile=True)
    assert meta.begin_season == expected_season
    assert meta.begin_episode == expected_ep
    assert meta.end_episode == expected_end
