"""离线配置 CLI 的运行时端口契约测试。"""

import pytest
from click.testing import CliRunner

from app import cli as cli_module


def test_config_get_works_without_web_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置读取必须使用启动前可用的 runtime 端口，不能依赖 Web 组合根。"""
    monkeypatch.setattr(cli_module, "has_runtime_setting", lambda _key: True)
    monkeypatch.setattr(
        cli_module,
        "get_runtime_setting",
        lambda key, *_args: "admin" if key == "SUPERUSER" else None,
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["config", "get", "SUPERUSER"],
    )

    assert result.exit_code == 0
    assert result.output == "admin\n"


def test_config_set_works_without_web_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置写入必须走离线可用的 runtime 更新端口。"""
    updates: list[tuple[str, str]] = []

    def update_setting(key: str, value: str) -> tuple[bool, str]:
        """记录离线配置写入。"""
        updates.append((key, value))
        return True, ""

    monkeypatch.setattr(
        cli_module,
        "update_runtime_setting",
        update_setting,
    )
    monkeypatch.setattr(
        cli_module,
        "_managed_backend_status",
        lambda: ("stopped", None, None, None),
    )
    monkeypatch.setattr(
        cli_module,
        "_managed_frontend_status",
        lambda: ("stopped", None, None, None),
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["config", "set", "SUPERUSER", "admin"],
    )

    assert result.exit_code == 0
    assert updates == [("SUPERUSER", "admin")]
    assert result.output == "SUPERUSER 已更新\n"
