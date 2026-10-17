"""
验证繁体字幕识别与默认字幕语言标记。
"""

from pathlib import Path

import pytest

from app.modules.filemanager.transhandler import TransHandler
from app.runtime.config import settings
from app.schemas.file import FileItem


@pytest.fixture
def _restore_subtitle_setting():
    """
    在字幕命名用例结束后恢复默认字幕语言，避免影响后续测试。
    """
    original = settings.DEFAULT_SUB
    try:
        yield
    finally:
        settings.DEFAULT_SUB = original


def _rename_subtitle(sub_name: str, default_sub: str) -> Path:
    """
    直接调用字幕重命名逻辑，覆盖语言标签识别与默认字幕标记。
    """
    settings.DEFAULT_SUB = default_sub
    sub_item = FileItem(
        storage="local",
        type="file",
        path=f"/source/{sub_name}.srt",
        name=sub_name,
        extension="srt",
    )
    target_file = Path("/target/24 Hours.2001.S02E01.[tmdbid=14064].srt")
    return TransHandler._TransHandler__rename_subtitles(sub_item, target_file)


@pytest.mark.usefixtures("_restore_subtitle_setting")
def test_traditional_chinese_subtitle_is_not_misclassified_as_simplified() -> None:
    """
    issue #5703: “繁体中文” 不应命中简中兜底规则，也不应被打上默认简中标签。
    """
    renamed = _rename_subtitle("24.Hours.S02E01.繁体中文", default_sub="zh-cn")
    assert renamed.name == "24 Hours.2001.S02E01.[tmdbid=14064].zh-tw.srt"


@pytest.mark.usefixtures("_restore_subtitle_setting")
def test_traditional_chinese_subtitle_can_be_marked_as_default() -> None:
    """
    当默认字幕设置为繁中时，仍应保留正确的繁中语言后缀并追加 default 标记。
    """
    renamed = _rename_subtitle("24.Hours.S02E01.繁体中文", default_sub="zh-tw")
    assert renamed.name == "24 Hours.2001.S02E01.[tmdbid=14064].default.zh-tw.srt"
