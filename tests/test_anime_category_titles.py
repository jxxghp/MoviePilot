"""
验证分类标签和包含分类词的真实动漫片名在 Python 回退中的边界。
"""

from typing import Optional
from unittest.mock import patch

import pytest

from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType
from tests.cases.meta import meta_cases


@pytest.mark.parametrize("subtitle", ["CHS", "CHT"])
def test_python_fallback_preserves_movie_title_after_release_group(subtitle: str) -> None:
    """
    完整发布名中的 Movie 属于片名，回退解析不能把字幕组当作标题。
    """
    title = (
        "[黑白字幕组]我心里危险的东西剧场版 / "
        "Boku no Kokoro no Yabai Yatsu Movie "
        f"[2026] [WEB-DL 1080p AVC-8bit AAC][{subtitle}]"
        "(检索用：我内心的糟糕念头 剧场版)"
    )
    with patch("app.adapters.system.rust.parse_metainfo", return_value=None):
        meta = MetaInfo(title)

    assert meta.en_name == "Boku No Kokoro No Yabai Yatsu Movie"
    assert meta.original_name == "Boku No Kokoro No Yabai Yatsu Movie"
    assert meta.cn_name != "黑白字幕组"
    assert meta.year == "2026"
    assert meta.resource_pix == "1080p"
    assert meta.audio_encode == "AAC"
    assert meta.resource_team == "黑白字幕组"
    assert meta.begin_season is None
    assert meta.begin_episode is None


@pytest.mark.parametrize(
    ("body", "expected_name", "episode"),
    [
        ("[星空电影 / Starry Sky Movie][2026]", "Starry Sky Movie", None),
        ("[Starry Sky Movie / 星空电影][2026]", "Starry Sky Movie", None),
        ("[Starry Sky Movie][2026]", "Starry Sky Movie", None),
        ("[动画之旅][2026]", "动画之旅", None),
        ("[Movie][星空之旅 / Starry Sky][2026]", "Starry Sky", None),
        ("[TV Series][动画之旅 / Animation Journey][2026][01]", "Animation Journey", 1),
        ("[TV Animation Club][2026][01]", "Tv Animation Club", 1),
        ("[My Anime Story][2026][01]", "My Anime Story", 1),
    ],
)
@pytest.mark.parametrize("subtitle", ["CHS", "CHT"])
def test_python_fallback_distinguishes_categories_from_titles(
    body: str, expected_name: str, episode: Optional[int], subtitle: str,
) -> None:
    """
    双语和中文标题应保留分类词，独立 Movie 和 TV Series 标签应跳过。
    """
    title = f"[测试字幕组]{body}[1080p][AVC][AAC][{subtitle}]"
    with patch("app.adapters.system.rust.parse_metainfo", return_value=None):
        meta = MetaInfo(title)

    assert meta.name == expected_name
    assert meta.original_name == expected_name
    assert meta.year == "2026"
    assert meta.begin_season is None
    assert meta.begin_episode == episode
    assert meta.resource_pix == "1080p"
    assert meta.video_encode == "AVC"
    assert meta.audio_encode == "AAC"
    assert meta.resource_team == "测试字幕组"
    if episode is not None:
        assert meta.type == MediaType.TV


@pytest.mark.parametrize(
    "label",
    [
        "Movie", "TV", "TV Series", "TV 01-26 Fin", "Animation", "Animations(动画片)",
        "Anime", "7月新番", "月番", "★7月新番", "★04月新番★", "2022年7月番", "22年日剧", "动画",
    ],
)
def test_python_fallback_skips_complete_category_labels(label: str) -> None:
    """
    月份、星号和 TV 集数等完整分类标签仍不能覆盖后续真实片名。
    """
    title = f"[测试字幕组][{label}][Starry Sky][2026][01][1080p][AVC][AAC][CHS]"
    with patch("app.adapters.system.rust.parse_metainfo", return_value=None):
        meta = MetaInfo(title)

    assert meta.name == "Starry Sky"
    assert meta.year == "2026"


@pytest.mark.parametrize("noise", ["[WEB-DL 1080p AVC-8bit AAC]", "[2026][01-26]"])
def test_python_fallback_skips_resource_brackets_before_title(noise: str) -> None:
    """
    名称恢复应跨过资源规格和集数标签，不能将其当作真实标题。
    """
    title = f"[Group]{noise}[动画之旅][2026][01][1080p][AVC][AAC][CHS]"
    with patch("app.adapters.system.rust.parse_metainfo", return_value=None):
        meta = MetaInfo(title)

    assert meta.name == "动画之旅"
    assert meta.year == "2026"
    assert meta.begin_episode == 1
    assert meta.resource_pix == "1080p"
    assert meta.audio_encode == "AAC"
    assert meta.resource_team == "Group"


def test_python_fallback_does_not_use_group_or_category_as_missing_title() -> None:
    """
    没有真实片名时保留空名称，不用字幕组、分类或资源规格伪造片名。
    """
    title = "[Group][Movie][TV Series][2026][1080p][AVC][AAC][CHS]"
    with patch("app.adapters.system.rust.parse_metainfo", return_value=None):
        meta = MetaInfo(title)

    assert not meta.name
    assert meta.year == "2026"
    assert meta.resource_team == "Group"


@pytest.mark.parametrize(
    ("prefix", "expected_name"),
    [
        ("[Animations(动画片)]", "Jade Dynasty"),
        ("[GM-Team][国漫][寻剑", "Sword Quest"),
        ("[GM-Team][国漫][斗破苍穹", "Fights Break Sphere V"),
        ("[Gal to Kyouryuu]", "Gal To Kyouryuu"),
    ],
)
def test_python_fallback_preserves_existing_bracket_title_recovery(
    prefix: str, expected_name: str,
) -> None:
    """
    名称恢复继续兼容嵌套方括号、国漫分类和被误识别为发布组的唯一片名。
    """
    title = next(case["title"] for case in meta_cases if case.get("title", "").startswith(prefix))
    with patch("app.adapters.system.rust.parse_metainfo", return_value=None):
        meta = MetaInfo(title)

    assert meta.name == expected_name
