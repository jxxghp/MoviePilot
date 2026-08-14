from app.domain.meta import customization as customization_module
from app.domain.meta.customization import CustomizationMatcher


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
