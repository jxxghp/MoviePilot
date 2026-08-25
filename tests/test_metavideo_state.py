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
    """拆分资源和编码词元应通过显式历史状态连续识别。"""
    meta = MetaVideo(title)

    actual = {field: getattr(meta, field) for field in expected}
    assert actual == expected


def test_metavideo_keeps_transient_token_state_out_of_result_model():
    """解析结果只承载媒体元数据，不泄漏词元管线的临时控制状态。"""
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
