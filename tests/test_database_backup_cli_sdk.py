from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from app import cli as cli_module
from app.application.backup import BackupArtifact, BackupVerification
from app.cli import cli
from app.sdk import database as database_sdk


NAME = "moviepilot_v3.0.0_sqlite_20260819_030000.db"


def _artifact(tmp_path: Path) -> BackupArtifact:
    path = tmp_path / NAME
    path.write_bytes(b"snapshot")
    return BackupArtifact(NAME, "sqlite", datetime(2026, 8, 19, 3, 0), path, 8)


def test_cli_backup_list_and_verify_use_local_governance(tmp_path: Path, monkeypatch) -> None:
    governance = Mock()
    governance.create_backup.return_value = _artifact(tmp_path)
    governance.list_backups.return_value = (governance.create_backup.return_value,)
    governance.verify_backup.return_value = BackupVerification(True, "integrity_check")
    monkeypatch.setattr(cli_module, "build_database_governance", lambda: governance)

    backup = CliRunner().invoke(cli, ["database", "backup"])
    listed = CliRunner().invoke(cli, ["database", "list"])
    verified = CliRunner().invoke(cli, ["database", "verify", NAME])

    assert backup.exit_code == 0, backup.output
    assert "name\tdb_type\tcreated_at\tsize\tpath" in backup.output
    assert NAME in backup.output
    assert listed.exit_code == 0 and NAME in listed.output
    assert verified.exit_code == 0 and "校验通过" in verified.output


def test_cli_restore_requires_explicit_offline_confirmation(tmp_path: Path, monkeypatch) -> None:
    governance = Mock()
    governance.restore_backup.return_value = _artifact(tmp_path)
    monkeypatch.setattr(cli_module, "build_database_governance", lambda: governance)

    rejected = CliRunner().invoke(cli, ["database", "restore", NAME])
    restored = CliRunner().invoke(cli, ["database", "restore", NAME, "--confirm"])

    assert rejected.exit_code == 1
    governance.restore_backup.assert_called_once_with(NAME)
    assert restored.exit_code == 0 and "还原完成" in restored.output


def test_sdk_exposes_backup_without_restore_or_policy_controls(tmp_path: Path, monkeypatch) -> None:
    governance = Mock()
    artifact = _artifact(tmp_path)
    verification = BackupVerification(True, "integrity_check")
    governance.create_backup.return_value = artifact
    governance.list_backups.return_value = (artifact,)
    governance.verify_backup.return_value = verification
    monkeypatch.setattr(database_sdk, "_get_database_governance", lambda: governance)

    assert database_sdk.create_backup() == artifact
    assert database_sdk.list_backups() == (artifact,)
    assert database_sdk.verify_backup(NAME) == verification
    assert not hasattr(database_sdk, "get_database_governance")
    assert not hasattr(database_sdk, "restore_backup")
    assert not hasattr(database_sdk, "delete_backup")


def test_sdk_rejects_invalid_name_at_host_boundary(monkeypatch) -> None:
    governance = Mock()
    governance.verify_backup.side_effect = ValueError("数据库备份文件名无效")
    monkeypatch.setattr(database_sdk, "_get_database_governance", lambda: governance)

    with pytest.raises(ValueError, match="文件名"):
        database_sdk.verify_backup("../../user.db")
