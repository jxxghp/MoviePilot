from typing import Any

from app.core.config import settings


def test_update_float_setting_accepts_json_integer(monkeypatch) -> None:
    """浮点配置应接受 JSON 整数并按浮点值持久化。"""
    persisted: dict[str, Any] = {}

    def persist_setting(
        field_name: str,
        original_value: Any,
        converted_value: Any,
    ) -> tuple[bool, None]:
        """记录待持久化配置，避免测试写入真实配置文件。"""
        persisted.update(
            field_name=field_name,
            original_value=original_value,
            converted_value=converted_value,
        )
        return True, None

    monkeypatch.setattr(settings, "LLM_TEMPERATURE", 0.3)
    monkeypatch.setattr(
        type(settings),
        "update_env_config",
        staticmethod(persist_setting),
    )

    success, message = settings.update_setting("LLM_TEMPERATURE", 1)

    assert success is True
    assert message is None
    assert settings.LLM_TEMPERATURE == 1.0
    assert isinstance(settings.LLM_TEMPERATURE, float)
    assert persisted == {
        "field_name": "LLM_TEMPERATURE",
        "original_value": 1,
        "converted_value": 1.0,
    }
