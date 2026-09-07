from typing import Any

from app.runtime.config import Settings, settings


def test_update_float_setting_accepts_json_integer(monkeypatch) -> None:
    """浮点配置应接受 JSON 整数并按浮点值持久化。"""
    persisted: dict[str, Any] = {}

    def persist_setting(
        field_name: str,
        original_value: Any,
        converted_value: Any,
    ) -> tuple[bool, str]:
        """记录待持久化配置，避免测试写入真实配置文件。"""
        persisted.update(
            field_name=field_name,
            original_value=original_value,
            converted_value=converted_value,
        )
        return True, ""

    monkeypatch.setattr(settings, "LLM_TEMPERATURE", 0.3)
    monkeypatch.setattr(
        type(settings),
        "update_env_config",
        staticmethod(persist_setting),
    )

    success, message = settings.update_setting("LLM_TEMPERATURE", 1)

    assert success is True
    assert message == ""
    assert settings.LLM_TEMPERATURE == 1.0
    assert isinstance(settings.LLM_TEMPERATURE, float)
    assert persisted == {
        "field_name": "LLM_TEMPERATURE",
        "original_value": 1,
        "converted_value": 1.0,
    }


def test_short_api_token_update_does_not_log_token(monkeypatch) -> None:
    """短 API_TOKEN 自动替换时日志不得包含令牌原文。"""
    config = Settings(API_TOKEN="0123456789abcdef")
    messages: list[str] = []
    monkeypatch.setattr(Settings, "update_env_config", lambda *_args: (True, ""))
    monkeypatch.setattr(
        "app.runtime.config.logger.warning",
        messages.append,
    )

    success, message = config.update_setting("API_TOKEN", "short-token")

    assert success is True
    assert message == ""
    assert config.API_TOKEN != "short-token"
    assert messages
    assert "short-token" not in messages[0]


def test_rust_accel_update_uses_field_policy(monkeypatch) -> None:
    """free-threaded 运行时的 Rust 加速约束由字段策略执行。"""
    config = Settings(RUST_ACCEL=True)
    monkeypatch.setattr("app.runtime.config.is_free_threaded_runtime", lambda: True)

    success, message = config.update_setting("RUST_ACCEL", False)

    assert success is False
    assert message == "free-threaded 运行时必须启用 Rust 加速"
    assert config.RUST_ACCEL is True
