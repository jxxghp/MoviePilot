"""系统数据库备份 Web 管理端点测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI
from fastapi import HTTPException

from app.api.endpoints import system as system_endpoint
from app.api.dependencies.auth import get_current_active_superuser_async
from app.application.backup import (
    BackupVerification,
    DatabaseBackupInProgressError,
)


class _Governance:
    """提供端点测试所需的最小数据库治理合同。"""

    def __init__(self) -> None:
        self.artifact = SimpleNamespace(
            name="moviepilot_v3.0.0_sqlite_20260825_120000.db",
            db_type="sqlite",
            created_at=datetime(2026, 8, 25, 12, 0, 0),
            path=Path("/private/database_backup/moviepilot_v3.0.0_sqlite_20260825_120000.db"),
            size=4096,
        )

    def list_backups(self):
        return (self.artifact,)

    def create_backup(self):
        return self.artifact

    def verify_backup(self, name: str):
        if "/" in name:
            raise ValueError("数据库备份文件名不能包含路径")
        return BackupVerification(True, "PRAGMA integrity_check", "private detail")

    def delete_backup(self, name: str):
        if "/" in name:
            raise ValueError("数据库备份文件名不能包含路径")
        if name == "missing.db":
            raise FileNotFoundError(name)


@pytest.mark.asyncio
async def test_list_database_backups_maps_public_fields(monkeypatch) -> None:
    """列表不得把内部备份路径投影到 Web 响应。"""
    monkeypatch.setattr(system_endpoint, "get_database_governance", _Governance)

    result = await system_endpoint.list_database_backups(_=object())

    assert [item.model_dump() for item in result] == [
        {
            "name": "moviepilot_v3.0.0_sqlite_20260825_120000.db",
            "db_type": "sqlite",
            "created_at": datetime(2026, 8, 25, 12, 0, 0),
            "size": 4096,
        }
    ]


@pytest.mark.asyncio
async def test_create_database_backup_reports_busy_state(monkeypatch) -> None:
    """重复创建应返回冲突，不排队创建第二份备份。"""
    governance = _Governance()
    monkeypatch.setattr(
        governance,
        "create_backup",
        lambda: (_ for _ in ()).throw(
            DatabaseBackupInProgressError("已有数据库备份任务正在执行")
        ),
    )
    monkeypatch.setattr(system_endpoint, "get_database_governance", lambda: governance)

    with pytest.raises(HTTPException) as error:
        await system_endpoint.create_database_backup(_=object())

    assert error.value.status_code == 409
    assert error.value.detail == "已有数据库备份任务正在执行"


@pytest.mark.asyncio
async def test_verify_database_backup_rejects_path_input(monkeypatch) -> None:
    """校验端点只接受受管文件名。"""
    monkeypatch.setattr(system_endpoint, "get_database_governance", _Governance)

    with pytest.raises(HTTPException) as error:
        await system_endpoint.verify_database_backup("../user.db", _=object())

    assert error.value.status_code == 400
    assert error.value.detail == "数据库备份文件名无效"


@pytest.mark.asyncio
async def test_delete_database_backup_accepts_only_managed_name(monkeypatch) -> None:
    governance = _Governance()
    monkeypatch.setattr(system_endpoint, "get_database_governance", lambda: governance)

    response = await system_endpoint.delete_database_backup(
        governance.artifact.name,
        _=object(),
    )

    assert response.success is True

    with pytest.raises(HTTPException) as invalid:
        await system_endpoint.delete_database_backup("../user.db", _=object())
    assert invalid.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_database_backup_uses_host_response_envelope(monkeypatch) -> None:
    """Web 客户端必须收到统一响应，不能把成功删除误判为空响应错误。"""
    governance = _Governance()
    monkeypatch.setattr(system_endpoint, "get_database_governance", lambda: governance)
    app = FastAPI()
    app.include_router(system_endpoint.router, prefix="/api/v1/system")
    app.dependency_overrides[get_current_active_superuser_async] = lambda: object()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.delete(
            f"/api/v1/system/database/backups/{governance.artifact.name}"
        )

    assert response.status_code == 200
    assert response.json() == {"success": True, "message": "", "data": None}


@pytest.mark.asyncio
async def test_list_database_backups_does_not_block_event_loop(monkeypatch) -> None:
    """文件系统或数据库工具等待必须在线程边界内执行。"""
    started = Event()
    release = Event()
    governance = _Governance()

    def blocking_list():
        started.set()
        if not release.wait(timeout=2):
            raise TimeoutError("测试未释放数据库备份列表")
        return (governance.artifact,)

    monkeypatch.setattr(governance, "list_backups", blocking_list)
    monkeypatch.setattr(system_endpoint, "get_database_governance", lambda: governance)

    task = asyncio.create_task(system_endpoint.list_database_backups(_=object()))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()
    assert task.done() is False
    release.set()

    assert len(await task) == 1
