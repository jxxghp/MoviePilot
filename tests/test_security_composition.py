"""认证组合根的部署兼容修复测试。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.startup.composition import security as security_composition


def test_backfill_superuser_setting_from_existing_database_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V2 数据库已有管理员而配置缺失时，应持久化稳定管理员用户名。"""
    updates: list[tuple[str, str]] = []
    users = SimpleNamespace(
        get_active_superuser=Mock(
            return_value=SimpleNamespace(name="legacy-admin")
        )
    )
    monkeypatch.setattr(
        security_composition,
        "get_runtime_setting",
        lambda _key: "",
    )

    def update_setting(key: str, value: str) -> tuple[bool, str]:
        """记录启动兼容修复写入。"""
        updates.append((key, value))
        return True, ""

    monkeypatch.setattr(
        security_composition,
        "update_runtime_setting",
        update_setting,
    )

    security_composition._backfill_superuser_setting(users)

    assert updates == [("SUPERUSER", "legacy-admin")]
    users.get_active_superuser.assert_called_once_with()


def test_backfill_superuser_setting_preserves_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 SUPERUSER 必须保持原值，不得被数据库顺序覆盖。"""
    users = SimpleNamespace(get_active_superuser=Mock())
    update = Mock()
    monkeypatch.setattr(
        security_composition,
        "get_runtime_setting",
        lambda _key: "configured-admin",
    )
    monkeypatch.setattr(security_composition, "update_runtime_setting", update)

    security_composition._backfill_superuser_setting(users)

    users.get_active_superuser.assert_not_called()
    update.assert_not_called()
