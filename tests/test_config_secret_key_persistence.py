"""SECRET_KEY / RESOURCE_SECRET_KEY 首次启动持久化到 app.env 的契约测试。"""

import pytest
from dotenv import dotenv_values

from app.runtime import config as config_module
from app.runtime.config import Settings

SECRET_FIELDS = ("SECRET_KEY", "RESOURCE_SECRET_KEY")


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    """隔离 app.env 路径并清除进程环境中的密钥变量。"""
    path = tmp_path / "app.env"
    path.write_text("", encoding="utf-8")
    for name in SECRET_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("app.runtime.config.get_env_path", lambda: path)
    return path


def _build(env_path, **overrides) -> Settings:
    """按测试惯例构造指向临时目录的配置实例。"""
    return Settings(
        _env_file=env_path,
        CONFIG_DIR=str(env_path.parent),
        API_TOKEN="0123456789abcdef",
        **overrides,
    )


def test_generated_secrets_are_written_on_first_start(env_path) -> None:
    """未配置密钥时首次构造应生成并写入 app.env，且与实例值一致。"""
    config = _build(env_path)

    persisted = dotenv_values(env_path)
    for name in SECRET_FIELDS:
        assert persisted[name] == getattr(config, name)
        assert len(persisted[name]) >= 32


def test_second_start_reuses_persisted_secrets(env_path) -> None:
    """再次构造应读回已写入的密钥，文件内容不变。"""
    first = _build(env_path)
    content_after_first = env_path.read_text(encoding="utf-8")

    second = _build(env_path)

    for name in SECRET_FIELDS:
        assert getattr(second, name) == getattr(first, name)
    assert env_path.read_text(encoding="utf-8") == content_after_first


def test_configured_secret_in_env_file_is_not_overwritten(env_path) -> None:
    """app.env 已有的密钥保持原值，仅补写缺失的那个。"""
    env_path.write_text("SECRET_KEY='fixed-secret-key-value'\n", encoding="utf-8")

    config = _build(env_path)

    persisted = dotenv_values(env_path)
    assert config.SECRET_KEY == "fixed-secret-key-value"
    assert persisted["SECRET_KEY"] == "fixed-secret-key-value"
    assert persisted["RESOURCE_SECRET_KEY"] == config.RESOURCE_SECRET_KEY


def test_secret_from_process_environment_is_not_written(env_path, monkeypatch) -> None:
    """环境变量提供的密钥不写入 app.env，避免与部署来源不一致。"""
    monkeypatch.setenv("SECRET_KEY", "env-provided-secret-key-value")

    config = _build(env_path)

    persisted = dotenv_values(env_path)
    assert config.SECRET_KEY == "env-provided-secret-key-value"
    assert "SECRET_KEY" not in persisted
    assert persisted["RESOURCE_SECRET_KEY"] == config.RESOURCE_SECRET_KEY


def test_write_failure_only_warns_and_keeps_runtime_secret(env_path, monkeypatch) -> None:
    """app.env 不可写时记录告警，本次运行仍持有可用密钥，且日志不含密钥值。"""
    warnings: list[str] = []
    monkeypatch.setattr(config_module.logger, "warning", warnings.append)

    def _deny(**_kwargs):
        raise PermissionError("read-only config dir")

    monkeypatch.setattr(config_module, "set_key", _deny)

    config = _build(env_path)

    assert len(warnings) == 2
    for name, message in zip(SECRET_FIELDS, warnings):
        assert name in message
        assert getattr(config, name)
        assert getattr(config, name) not in message
    assert env_path.read_text(encoding="utf-8") == ""
