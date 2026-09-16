from app.domain.meta import customization as customization_module
from app.domain.meta.customization import CustomizationMatcher
from app.sdk.media import set_custom_separator


def test_match_uses_latest_customization_setting(monkeypatch):
    """自定义占位符修改后，下一次识别应直接使用新配置。"""
    matcher = CustomizationMatcher()
    values = [["GROUP"], ["TEAM"]]
    monkeypatch.setattr(
        customization_module,
        "_customization_provider",
        lambda: values[0],
    )

    assert matcher.match("[GROUP][TEAM] Movie") == "GROUP"
    values[0] = ["TEAM"]
    assert matcher.match("[GROUP][TEAM] Movie") == "TEAM"


def test_sdk_set_custom_separator_updates_matcher_and_restores_default(monkeypatch):
    """插件只能通过 SDK 设置分隔符，清空后恢复宿主默认值。"""
    matcher = CustomizationMatcher()
    monkeypatch.setattr(
        customization_module,
        "_customization_provider",
        lambda: ["GROUP", "TEAM"],
    )

    set_custom_separator("#")
    assert matcher.match("[GROUP][TEAM] Movie") == "GROUP#TEAM"

    set_custom_separator(None)
    assert matcher.match("[GROUP][TEAM] Movie") == "GROUP@TEAM"
