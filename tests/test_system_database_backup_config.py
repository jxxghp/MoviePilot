"""系统数据库备份策略配置测试。"""

import asyncio
from unittest.mock import patch

import pytest

from app.api.endpoints import system as system_endpoint
from app.runtime.config import settings


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DB_BACKUP_CRON": "0 3 * *"}, "数据库备份周期格式不正确"),
        ({"DB_BACKUP_PATH": 123}, "数据库备份目录必须是路径字符串"),
        ({"DB_BACKUP_RETENTION_DAYS": -1}, "数据库备份过期天数"),
        ({"DB_BACKUP_RETENTION_DAYS": 1.5}, "数据库备份过期天数"),
        ({"DB_BACKUP_MAX_COUNT": True}, "数据库备份最大保留份数"),
        ({"DB_BACKUP_MAX_COUNT": "many"}, "数据库备份最大保留份数"),
    ],
)
def test_database_backup_policy_rejects_invalid_values(
    env: dict,
    message: str,
) -> None:
    """无效策略必须在批量配置写入前被拒绝。"""
    env["DB_BACKUP_ENABLE"] = True
    assert message in str(system_endpoint._validate_database_backup_config(env))


@pytest.mark.parametrize(
    "env",
    [
        {"DB_BACKUP_CRON": ""},
        {"DB_BACKUP_CRON": "0 3 * * *"},
        {"DB_BACKUP_ON_UPGRADE": True},
        {"DB_BACKUP_ON_UPGRADE": False},
        {"DB_BACKUP_PATH": None},
        {"DB_BACKUP_PATH": ""},
        {"DB_BACKUP_PATH": "database_backup"},
        {"DB_BACKUP_RETENTION_DAYS": 0},
        {"DB_BACKUP_MAX_COUNT": "0"},
    ],
)
def test_database_backup_policy_accepts_supported_boundaries(env: dict) -> None:
    """空目录使用默认路径，两个保留值的零均表示不限制。"""
    env["DB_BACKUP_ENABLE"] = True
    assert system_endpoint._validate_database_backup_config(env) is None


@pytest.mark.parametrize("disabled", [False, "false", "0", "off"])
def test_database_backup_policy_rejects_invalid_hidden_values_when_disabled(disabled) -> None:
    """关闭总开关只暂停调度，不能把无效策略写入持久配置。"""
    error = system_endpoint._validate_database_backup_config({
        "DB_BACKUP_ENABLE": disabled,
        "DB_BACKUP_CRON": "invalid",
        "DB_BACKUP_PATH": 123,
        "DB_BACKUP_RETENTION_DAYS": -1,
        "DB_BACKUP_MAX_COUNT": 1.5,
    })

    assert error is not None


def test_set_env_rejects_invalid_database_backup_policy_without_partial_write() -> None:
    """备份策略校验失败时不得调用 Settings 的批量写入。"""
    env = {
        "DB_BACKUP_ENABLE": True,
        "DB_BACKUP_CRON": "invalid",
        "DB_BACKUP_RETENTION_DAYS": 30,
        "DB_BACKUP_MAX_COUNT": 30,
    }

    with patch.object(
        system_endpoint,
        "_validate_llm_server_tool_config",
        return_value=None,
    ), patch.object(type(settings), "update_settings") as update_settings:
        response = asyncio.run(system_endpoint.set_env_setting(env=env, _=object()))

    assert response.success is False
    assert "数据库备份周期格式不正确" in response.message
    update_settings.assert_not_called()


def test_database_backup_default_path_tracks_config_directory(tmp_path, monkeypatch) -> None:
    """未显式配置目录时应跟随当前配置根，而不是写死 Docker 路径。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DB_BACKUP_PATH", None)

    assert settings.DATABASE_BACKUP_PATH == tmp_path / "database_backup"
