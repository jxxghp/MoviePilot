"""
验证季图片落盘开关从部署配置传入刮削链快照。
"""

import pytest

from app.runtime.config import Settings
from app.startup.composition.configuration import build_chain_runtime_config


@pytest.mark.parametrize("env_value,expected", [(None, True), ("true", True), ("false", False)])
def test_season_image_root_setting_reaches_chain_snapshot(monkeypatch, tmp_path, env_value, expected) -> None:
    """
    未配置时保持双写，环境变量可以显式启用或关闭根目录写入。
    """
    monkeypatch.delenv("SCRAP_SEASON_IMAGE_TO_ROOT", raising=False)
    if env_value is not None:
        monkeypatch.setenv("SCRAP_SEASON_IMAGE_TO_ROOT", env_value)
    settings = Settings(_env_file=None, CONFIG_DIR=str(tmp_path), API_TOKEN="0123456789abcdef")

    assert settings.SCRAP_SEASON_IMAGE_TO_ROOT is expected
    assert build_chain_runtime_config(settings).scrap_season_image_to_root is expected


@pytest.mark.parametrize("env_value,expected", [(None, True), ("true", True), ("false", False)])
@pytest.mark.parametrize("key,attribute", [
    ("SCRAP_IMAGE_ALIAS", "scrap_image_alias"),
    ("SCRAP_4K_BACKGROUND", "scrap_4k_background"),
])
def test_image_settings_reach_chain_snapshot(monkeypatch, tmp_path, key, attribute, env_value, expected) -> None:
    """
    图片别名和 4K 背景开关默认兼容现状，并由部署配置传入刮削链。
    """
    monkeypatch.delenv(key, raising=False)
    if env_value is not None:
        monkeypatch.setenv(key, env_value)
    settings = Settings(_env_file=None, CONFIG_DIR=str(tmp_path), API_TOKEN="0123456789abcdef")

    assert getattr(settings, key) is expected
    assert getattr(build_chain_runtime_config(settings), attribute) is expected
