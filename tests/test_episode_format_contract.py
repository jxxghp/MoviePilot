"""
集数范围筛选、零集偏移与缺失文件名的回归边界。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.formatting import EpisodeFormatRuleHelper, FormatParser
from app.chain.transfer.format import EpisodeFormatMixin
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.transfer import EpisodeFormatRule


@pytest.mark.parametrize("episode,expected", [(0, False), (2, False), (3, True), (4, True), (5, False)])
def test_hyphen_range_matches_numeric_boundaries_before_offset(episode, expected):
    """
    连字符区间按数字边界筛选，集数偏移不应改变筛选范围或原始属性。
    """
    parser = FormatParser(eformat="Show - {ep}.mkv", details="03-04", offset="EP+10")
    assert parser.start_ep == parser.end_ep == "03-04"
    assert parser.match(f"Show - {episode:02d}.mkv") is expected
    assert parser.split_episode("Show - 03.mkv", MetaBase(title="")) == (13, 14, None)


@pytest.mark.parametrize("details,episode,expected", [
    ("3", 2, False), ("3", 3, True), ("3,4", 3, True), ("3,4", 4, True),
    ("4,3", 3, False), ("4,3", 4, True), ("4-3", 3, False), ("4-3", 4, False),
])
def test_existing_single_and_comma_range_rules_keep_boundaries(details, episode, expected):
    """
    单集与逗号区间保留既有范围规则，倒序连字符区间不自动交换边界。
    """
    parser = FormatParser(eformat="Show - {ep}.mkv", details=details)
    assert parser.match(f"Show - {episode:02d}.mkv") is expected


@pytest.mark.parametrize("begin,end,offset,expected", [
    (0, None, "+1", (1, None, "A")),
    (1, None, "-1", (0, None, "A")),
    (0, None, "-1", (-1, None, "A")),
    (0, 1, "+1", (1, 2, "A")),
    (1, 2, "-1", (0, 1, "A")),
    (1, 1, "-1", (0, 0, "A")),
    (0, 0, None, (0, 0, "A")),
    (None, 0, "+1", (None, 1, "A")),
    (None, None, "+1", (None, None, "A")),
])
def test_offset_only_preserves_zero_and_missing_episode_boundaries(begin, end, offset, expected) -> None:
    """
    仅填偏移时，源集号和结果中的零都应参与计算，缺失边界保留 None。
    """
    meta = MetaBase(title="Show")
    meta.begin_episode = begin
    meta.end_episode = end
    parser = FormatParser(eformat="", offset=offset, part="A")

    assert parser.split_episode("", meta) == expected


@pytest.mark.parametrize("episodes,offset,expected", [
    ("00", "+1", (1, None, None)),
    ("01", "-1", (0, None, None)),
    ("00-E01", "+1", (1, 2, None)),
    ("01-E02", "-1", (0, 1, None)),
    ("01-E01", "-1", (0, 0, None)),
    ("00", None, (0, None, None)),
    ("unknown", "+1", (None, None, None)),
])
def test_template_episode_offsets_preserve_zero(episodes, offset, expected) -> None:
    """
    定位模板提取的零集及偏移结果为零的起止集数应完整返回。
    """
    parser = FormatParser(eformat="Show.S01E{ep}.mkv", offset=offset)

    assert parser.split_episode(f"Show.S01E{episodes}.mkv", MetaBase(title="")) == expected


@pytest.mark.parametrize("details,expected", [
    (None, True), ("0", True), ("0-1", True), ("0,1", True), ("1", False), ("1-2", False),
])
def test_template_matches_zero_with_episode_range_constraints(details, expected) -> None:
    """
    第零集可以命中定位模板，再按指定集数范围决定是否保留。
    """
    parser = FormatParser(eformat="Show.S01E{ep}.mkv", details=details)

    assert parser.match("Show.S01E00.mkv") is expected
    assert parser.match("Show.S01Eunknown.mkv") is False


@pytest.mark.parametrize("details,offset,expected", [
    ("0", "+1", (1, None, None)),
    ("1", "-1", (0, None, None)),
    ("0-1", "+1", (1, 2, None)),
    ("0,1", "+1", (1, 2, None)),
])
def test_explicit_episode_offsets_keep_zero_compatible(details, offset, expected) -> None:
    """
    指定集数与区间继续支持零集及偏移为零的现有语义。
    """
    parser = FormatParser(eformat="", details=details, offset=offset)

    assert parser.split_episode("", MetaBase(title="")) == expected


def test_missing_file_name_cannot_crash_template_recommendation(monkeypatch):
    """
    正则允许空集数时，缺少文件名的样本仍应正常返回推荐失败。
    """
    monkeypatch.setattr(
        "app.application.formatting.get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(video_extensions=(".mkv",), subtitle_extensions=(), audio_extensions=()),
    )
    sample = FileItem(storage="local", path="/downloads/", type="file", name=None, extension="mkv")
    rule = EpisodeFormatRule(name="empty", pattern=r"(?<ep>\d*)")
    state, message, data = EpisodeFormatRuleHelper().recommend([rule], [sample])
    assert state is False
    assert message
    assert data is None


@pytest.mark.parametrize("path", [None, ""])
def test_missing_sample_path_does_not_resolve_current_directory(monkeypatch, path):
    """
    无路径的样本不能被当成当前目录，也不能触发存储查询。
    """
    storage = Mock(side_effect=AssertionError("缺少路径时不得访问存储"))
    monkeypatch.setattr("app.chain.transfer.format.StorageChain", storage)
    source = FileItem(storage="local", path=path, type="file", name="show.mkv")
    owner = object.__new__(EpisodeFormatMixin)
    assert owner._resolve_episode_format_directory(source) is None
    storage.assert_not_called()
