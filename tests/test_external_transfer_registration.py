"""
外部整理登记的请求、只读文件证明和原子历史回执回归。
"""

import asyncio
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from functools import partial
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models.transferhistory import TransferHistory
from app.schemas import history as history_schema
from app.schemas.file import FileItem


def _request(**changes):
    """
    构造管理员明确提供的媒体和路径声明。
    """
    request_type = getattr(history_schema, "TransferHistoryRegisterRequest", None)
    assert request_type is not None, "缺少外部整理登记请求"
    payload = {
        "src": "/downloads/a.mkv", "dest": "/library/a.mkv",
        "src_storage": "local", "dest_storage": "local", "mode": "copy",
        "type": "电影", "title": "A", "year": "2026",
        "media_source": "tmdb", "media_id": "42",
    }
    return request_type.model_validate(payload | changes)


@pytest.mark.parametrize("mode", ["copy", "link", "move"])
def test_registration_request_accepts_only_explicit_metadata(mode):
    """
    三种登记模式保留规范身份和路径，写入用例另行冻结声明。
    """
    request = _request(mode=mode, media_id=" 42 ", src="/downloads//a.mkv")
    assert request.media_id == "42"
    assert request.src == "/downloads/a.mkv"


@pytest.mark.parametrize("changes", [
    {"src": "relative.mkv"}, {"src": "/downloads/../a.mkv"},
    {"src": "/bad\x00.mkv"}, {"dest": "/downloads/a.mkv"},
    {"src": "/downloads/a.mkv\n"}, {"src_storage": "\tlocal"},
    {"dest": "/library/\u0085a.mkv"},
    {"src_storage": ""}, {"title": ""}, {"title": "x" * 513},
    {"src": "/" + "x" * 4096}, {"dest_storage": "x" * 65},
    {"media_id": "x" * 129}, {"download_hash": "x" * 129},
    {"mode": "softlink"}, {"type": "音乐"}, {"year": "tomorrow"},
    {"media_id": "0"}, {"media_source": None}, {"seasons": "season 1"},
    {"episodes": "E00"}, {"actor_id": 99}, {"origin": "external"},
    {"status": True}, {"src_fileitem": {}}, {"transfer_task_id": "task"},
])
def test_registration_request_rejects_ambiguous_or_injected_fields(changes):
    """
    无法证明的路径和服务端拥有的身份、状态字段不得由请求注入。
    """
    with pytest.raises(ValidationError):
        _request(**changes)


@pytest.fixture
def registration_modules():
    """
    加载待实现的显式登记用例与适配器。
    """
    names = [
        "app.application.history.registration",
        "app.db.adapters.history.registration",
        "app.adapters.system.registration",
    ]
    for name in names:
        assert importlib.util.find_spec(name) is not None, f"缺少登记模块 {name}"
    return tuple(importlib.import_module(name) for name in names)


@pytest.fixture
def registration_store(tmp_path, registration_modules):
    """
    为并发和回滚回归创建真实文件数据库和独立会话工厂。
    """
    _, database, _ = registration_modules
    engine = create_engine(
        f"sqlite:///{tmp_path / 'registration.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    TransferHistory.__table__.create(engine)
    sessions = sessionmaker(engine)
    yield sessions, database.TransferRegistrationScopeFactory(sessions)
    engine.dispose()


def _local_query(storage, path):
    """
    用真实文件系统模拟严格 provider 查询，不执行任何写操作。
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return FileItem(
        path=str(path), storage=storage, type="file" if path.is_file() else "dir",
        size=info.st_size, modify_time=info.st_mtime,
        fileid=f"{info.st_dev}:{info.st_ino}",
        url="https://private.invalid/?passkey=secret",
    )


def _command(modules, scope, query=_local_query):
    """
    只注入只读查询和短事务能力，缺少删除、整理、下载和事件能力。
    """
    application, _, local = modules
    return application.TransferRegistrationCommand(
        scope_factory=scope, query_file=query,
        local_entry=partial(local.probe_local_entry, entry_factory=application.LocalRegistrationEntry),
    )


def _actor(modules, name="admin"):
    """
    模拟认证依赖返回的服务端管理员身份。
    """
    return modules[0].RegistrationActor(id=1, name=name)


def test_copy_registers_frozen_facts_and_returns_original_receipt(
    tmp_path, registration_modules, registration_store,
):
    """
    登记只记录存在事实，重复请求保留最初审计人和时间且不重新探测文件。
    """
    sessions, scope = registration_store
    src, dest = tmp_path / "a.mkv", tmp_path / "b.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    request = _request(src=str(src), dest=str(dest))
    command = _command(registration_modules, scope)
    result = command.execute(request, _actor(registration_modules))
    assert result.created
    assert result.history.origin == "external"
    assert result.history.transfer_task_id is None
    assert result.history.transfer_settlement_revision is None
    assert result.history.registration["verification"] == "matching_sizes"
    assert result.history.registration["actor_name"] == "admin"
    assert result.history.src_fileitem["size"] == 4
    assert "url" not in result.history.src_fileitem
    with pytest.raises(TypeError):
        result.history.registration["actor_name"] = "changed"
    src.unlink()
    dest.unlink()
    repeated = command.execute(request, _actor(registration_modules, "another"))
    assert not repeated.created
    assert repeated.history == result.history
    with sessions() as session:
        assert len(session.scalars(select(TransferHistory)).all()) == 1


def test_move_records_only_destination_facts(tmp_path, registration_modules, registration_store):
    """
    移动后的源确实不存在时不伪造源大小、标识或内容一致证据。
    """
    _, scope = registration_store
    dest = tmp_path / "b.mkv"
    dest.write_bytes(b"film")
    src = tmp_path / "missing.mkv"
    result = _command(registration_modules, scope).execute(
        _request(src=str(src), dest=str(dest), mode="move"), _actor(registration_modules),
    )
    assert result.history.src_fileitem == {"path": str(src), "storage": "local"}
    assert result.history.registration["verification"] == "destination_only"
    assert result.history.registration["source_state"] == "absent"


@pytest.mark.parametrize("case", ["missing", "size", "directory", "symlink", "dangling_move", "present_move"])
def test_invalid_file_facts_cannot_create_history(
    case, tmp_path, registration_modules, registration_store,
):
    """
    缺失、不等大小、目录和符号链接不得变成成功历史。
    """
    sessions, scope = registration_store
    src, dest = tmp_path / "a.mkv", tmp_path / "b.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    mode = "copy"
    if case == "missing":
        dest.unlink()
    elif case == "size":
        dest.write_bytes(b"different")
    elif case == "directory":
        dest.unlink()
        dest.mkdir()
    elif case == "symlink":
        dest.unlink()
        dest.symlink_to(src)
    elif case == "dangling_move":
        src.unlink()
        src.symlink_to(tmp_path / "missing")
        mode = "move"
    elif case == "present_move":
        mode = "move"
    with pytest.raises(registration_modules[0].RegistrationValidationError):
        _command(registration_modules, scope).execute(
            _request(src=str(src), dest=str(dest), mode=mode), _actor(registration_modules),
        )
    with sessions() as session:
        assert session.scalars(select(TransferHistory)).all() == []


@pytest.mark.parametrize("mode", ["copy", "link"])
def test_copy_and_link_reject_confirmed_missing_source(
    mode, tmp_path, registration_modules, registration_store,
):
    """
    目标普通文件有效时，复制或硬链接仍须因源确认缺失而拒绝，且不创建文件或历史。
    """
    sessions, scope = registration_store
    src, dest = tmp_path / "missing.mkv", tmp_path / "dest.mkv"
    dest.write_bytes(b"film")
    before = dest.stat()
    with pytest.raises(
        registration_modules[0].RegistrationValidationError,
        match="^源文件不存在，不能核验复制或硬链接$",
    ):
        _command(registration_modules, scope).execute(
            _request(src=str(src), dest=str(dest), mode=mode), _actor(registration_modules),
        )
    assert not src.exists()
    assert dest.read_bytes() == b"film"
    after = dest.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns,
    )
    with sessions() as session:
        assert session.scalars(select(TransferHistory)).all() == []


def test_local_link_rejects_equal_size_files_with_distinct_inodes(
    tmp_path, registration_modules, registration_store,
):
    """
    两个等长普通文件具有不同 inode 时必须进入原生硬链接拒绝分支，保留原文件且不写历史。
    """
    sessions, scope = registration_store
    src, dest = tmp_path / "source.mkv", tmp_path / "dest.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    before = [path.stat() for path in (src, dest)]
    assert before[0].st_size == before[1].st_size
    assert (before[0].st_dev, before[0].st_ino) != (before[1].st_dev, before[1].st_ino)
    with pytest.raises(
        registration_modules[0].RegistrationValidationError,
        match="^源文件与目标文件不是同一原生硬链接$",
    ):
        _command(registration_modules, scope).execute(
            _request(src=str(src), dest=str(dest), mode="link"), _actor(registration_modules),
        )
    for path, original in zip((src, dest), before):
        assert path.read_bytes() == b"film"
        after = path.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
            original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns,
        )
    with sessions() as session:
        assert session.scalars(select(TransferHistory)).all() == []


@pytest.mark.parametrize("enabled", [False, True])
def test_local_entry_proof_uses_lstat_in_both_proxy_modes(
    enabled, tmp_path, monkeypatch, registration_modules,
):
    """
    开启和关闭代理都能识别最终符号链接及不存在状态。
    """
    from app.adapters.system.fsproxy import FileSystemProxy

    monkeypatch.setattr(FileSystemProxy, "_enabled", staticmethod(lambda: enabled))
    src, alias = tmp_path / "a.mkv", tmp_path / "alias.mkv"
    src.write_bytes(b"film")
    alias.symlink_to(src)
    probe = partial(
        registration_modules[2].probe_local_entry, entry_factory=registration_modules[0].LocalRegistrationEntry,
    )
    assert probe(str(src)).is_file
    assert probe(str(alias)).is_symlink
    assert probe(str(tmp_path / "missing")) is None


def test_real_hardlink_is_accepted_but_aliases_are_rejected(
    tmp_path, registration_modules, registration_store,
):
    """
    硬链接须为不同目录项，父目录别名和仅大小写不同的同项均拒绝。
    """
    _, scope = registration_store
    folder = tmp_path / "real"
    folder.mkdir()
    src = folder / "Entry.mkv"
    dest = folder / "other.mkv"
    src.write_bytes(b"film")
    os.link(src, dest)
    command = _command(registration_modules, scope)
    result = command.execute(
        _request(src=str(src), dest=str(dest), mode="link"), _actor(registration_modules),
    )
    assert result.history.registration["verification"] == "native_hardlink"
    alias = tmp_path / "alias"
    alias.symlink_to(folder, target_is_directory=True)
    for path in (alias / src.name, folder / "entry.mkv"):
        with pytest.raises(registration_modules[0].RegistrationValidationError):
            # 换一个独立 source identity，避免已登记记录提前返回冲突。
            command._verify_files(_request(src=str(src), dest=str(path), mode="link"))


@pytest.mark.parametrize("mode", ["copy", "link"])
def test_unicode_equivalent_names_cannot_register_the_same_entry(
    mode, tmp_path, registration_modules, registration_store,
):
    """
    NFC/NFD 地址在支持规范等价的文件系统上必须作为同一目录项拒绝。
    """
    application, _, local = registration_modules
    sessions, scope = registration_store
    src, alias = tmp_path / "caf\u00e9.mkv", tmp_path / "cafe\u0301.mkv"
    src.write_bytes(b"film")
    supports_alias = alias.exists()
    if supports_alias:
        assert src.samefile(alias)
    probe = partial(local.probe_local_entry, entry_factory=application.LocalRegistrationEntry)

    def local_entry(path):
        """
        优先读取真实路径；区分规范形式的平台模拟同一目录项的第二种拼写。
        """
        if supports_alias or path == str(src):
            return probe(path)
        return replace(probe(str(src)), name=Path(path).name)

    def query(storage, path):
        """
        让不支持规范等价的平台也提供同文件、同父目录的可复现查询事实。
        """
        if supports_alias or path == src:
            return _local_query(storage, path)
        return _local_query(storage, src).model_copy(update={"path": str(path)})

    command = application.TransferRegistrationCommand(
        scope_factory=scope, query_file=query, local_entry=local_entry,
    )
    with pytest.raises(application.RegistrationValidationError):
        command.execute(_request(src=str(src), dest=str(alias), mode=mode), _actor(registration_modules))
    with sessions() as session:
        assert session.scalars(select(TransferHistory)).all() == []


def test_concurrent_registration_returns_one_created_receipt(
    tmp_path, registration_modules, registration_store,
):
    """
    两线程独立 Session 必须由数据库唯一约束仲裁为同一回执。
    """
    sessions, scope = registration_store
    src, dest = tmp_path / "a.mkv", tmp_path / "b.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    request = _request(src=str(src), dest=str(dest))
    barrier = Barrier(2)

    def query(storage, path):
        """
        两个独立读会话都确认无历史后才开始文件核验，强制覆盖原子插入竞争。
        """
        if path == src:
            barrier.wait(timeout=10)
        return _local_query(storage, path)

    command = _command(registration_modules, scope, query)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _: command.execute(request, _actor(registration_modules)), range(2),
        ))
    assert sorted(result.created for result in results) == [False, True]
    assert results[0].history.id == results[1].history.id
    with sessions() as session:
        assert len(session.scalars(select(TransferHistory)).all()) == 1


@pytest.mark.parametrize("change", [
    {"dest": "/other.mkv"}, {"title": "Other"}, {"mode": "move"},
])
def test_existing_source_is_never_replaced(
    change, tmp_path, registration_modules, registration_store,
):
    """
    同源的不同目标或媒体声明必须冲突，不能覆盖原审计。
    """
    sessions, scope = registration_store
    src, dest = tmp_path / "a.mkv", tmp_path / "b.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    request = _request(src=str(src), dest=str(dest))
    command = _command(registration_modules, scope)
    first = command.execute(request, _actor(registration_modules))
    with pytest.raises(registration_modules[0].RegistrationConflictError):
        command.execute(request.model_copy(update=change), _actor(registration_modules))
    with sessions() as session:
        assert session.get(TransferHistory, first.history.id).title == "A"


@pytest.mark.parametrize("active,superuser,manage,expected", [
    (True, True, False, 200), (True, False, False, 403),
    (True, False, True, 403), (False, True, True, 401),
])
def test_registration_route_requires_active_superuser(
    active, superuser, manage, expected, tmp_path, registration_modules, registration_store,
):
    """
    路由调用真实认证依赖；仅 manage 用户和未激活管理员均不能登记。
    """
    from app.api.dependencies import auth
    from app.api.dependencies import history as dependencies
    from app.api.endpoints import history

    dependency = getattr(dependencies, "get_transfer_registration_command", None)
    assert dependency is not None, "缺少登记依赖"
    _, scope = registration_store
    src, dest = tmp_path / "a.mkv", tmp_path / "b.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    application = FastAPI()
    application.include_router(history.router, prefix="/api/v1/history")
    application.dependency_overrides[auth.get_current_user] = lambda: SimpleNamespace(
        id=7, name="server-admin", is_active=active, is_superuser=superuser, permissions={"manage": manage},
    )
    application.dependency_overrides[dependency] = lambda: _command(registration_modules, scope)
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/history/transfer",
            json=_request(src=str(src), dest=str(dest)).model_dump(mode="json"),
        )
    assert response.status_code == expected
    if expected == 200:
        body = response.json()["data"]
        assert body["created"]
        assert body["history"]["registration"]["actor_id"] == 7
        assert body["history"]["registration"]["actor_name"] == "server-admin"


@pytest.mark.parametrize("name,display", [
    ("n" * 128, "n" * 128), ("名" * 129, "名" * 127 + "…"),
    ("", "#7"), ("admin\n", "admin\n"),
], ids=["maximum", "long", "empty", "control"])
def test_registration_command_preserves_identity_with_bounded_actor_display(
    name, display, tmp_path, registration_modules, registration_store,
):
    """
    既有合法用户名用有界显示快照登记，真实身份和重试回执均保持最初值。
    """
    from app.schemas.user import UserCreate

    user = UserCreate(name=name, is_active=True, is_superuser=True)
    _, scope = registration_store
    src, dest = tmp_path / "source.mkv", tmp_path / "dest.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    request = _request(src=str(src), dest=str(dest))
    command = _command(registration_modules, scope)
    first = command.execute(request, registration_modules[0].RegistrationActor(id=7, name=user.name))
    assert first.created
    assert first.history.registration["actor_id"] == 7
    assert first.history.registration["actor_name"] == display
    assert 1 <= len(first.history.registration["actor_name"]) <= 128
    src.unlink()
    dest.unlink()
    repeated = command.execute(request, registration_modules[0].RegistrationActor(id=8, name="another"))
    assert not repeated.created
    assert repeated.history == first.history


@pytest.mark.parametrize("name,display", [
    ("n" * 128, "n" * 128), ("名" * 129, "名" * 127 + "…"),
    ("", "#7"), ("admin\n", "admin\n"),
], ids=["maximum", "long", "empty", "control"])
def test_registration_http_accepts_existing_admin_name_contract(
    name, display, tmp_path, registration_modules, registration_store,
):
    """
    真实认证依赖接受的长名和空名不得导致 500，HTTP 重试保留首次身份和显示快照。
    """
    from app.api.dependencies import auth
    from app.api.dependencies import history as dependencies
    from app.api.endpoints import history
    from app.schemas.user import UserCreate

    user = UserCreate(name=name, is_active=True, is_superuser=True)
    actor = SimpleNamespace(id=7, name=user.name, is_active=user.is_active, is_superuser=user.is_superuser)
    sessions, scope = registration_store
    src, dest = tmp_path / "source.mkv", tmp_path / "dest.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    payload = _request(src=str(src), dest=str(dest)).model_dump(mode="json")
    application = FastAPI()
    application.include_router(history.router, prefix="/api/v1/history")
    application.dependency_overrides[auth.get_current_user] = lambda: actor
    application.dependency_overrides[dependencies.get_transfer_registration_command] = lambda: _command(
        registration_modules, scope,
    )
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post("/api/v1/history/transfer", json=payload)
        assert response.status_code == 200
        first = response.json()["data"]
        assert first["created"]
        assert first["history"]["registration"]["actor_id"] == 7
        assert first["history"]["registration"]["actor_name"] == display
        assert 1 <= len(first["history"]["registration"]["actor_name"]) <= 128
        src.unlink()
        dest.unlink()
        actor.id, actor.name = 8, "another"
        response = client.post("/api/v1/history/transfer", json=payload)
        assert response.status_code == 200
        repeated = response.json()["data"]
        assert not repeated["created"]
        assert repeated["history"] == first["history"]
    with sessions() as session:
        rows = session.scalars(select(TransferHistory)).all()
        assert len(rows) == 1
        assert rows[0].registration["actor_id"] == 7
        assert rows[0].registration["actor_name"] == display


@pytest.mark.parametrize("title", [None, "Movie", "*.mkv"])
@pytest.mark.parametrize("status", [None, True, False])
def test_external_origin_filter_count_and_page_agree(title, status, registration_store):
    """
    来源过滤贯穿普通标题、通配标题、状态和分页，旧查询仍包含未记录来源行。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.history import HistoryQueryService
    from app.db.adapters.history.transfer import TransactionalTransferHistoryRepository

    sessions, _ = registration_store
    with sessions() as session:
        for number, (origin, successful) in enumerate([
            (None, True), ("external", True), ("external", True), ("external", False),
        ]):
            session.add(TransferHistory(
                src=f"/src/{number}.mkv", src_storage="local", dest=f"/dest/{number}.mkv",
                title="Movie", origin=origin, status=successful, date=f"2026-10-0{number + 1}",
            ))
        session.commit()
    engine = create_async_engine(str(sessions.kw["bind"].url).replace("sqlite:", "sqlite+aiosqlite:"))
    async_sessions = async_sessionmaker(engine)
    repository = TransactionalTransferHistoryRepository(sync_session=sessions, async_session=async_sessions)
    service = HistoryQueryService(download_repository=None, transfer_repository=repository)

    async def check():
        """
        在同一事件循环验证 count、page 和未过滤查询。
        """
        try:
            result = await service.list_transfer(title=title, status=status, origin="external", page=1, count=1)
            assert result.total == (3 if status is None else 2 if status else 1)
            assert len(result.list) == 1
            assert result.list[0].origin == "external"
            ordinary = await service.list_transfer(title=title, status=status)
            assert ordinary.total == (4 if status is None else 3 if status else 1)
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("origin", [None, "external"])
def test_history_route_passes_only_explicit_origin_to_query(origin):
    """
    真实路由保留旧查询的参数省略形态，显式外部来源仍完整传给查询服务。
    """
    from app.api.dependencies import history as dependencies
    from app.api.endpoints import history

    query = SimpleNamespace(list_transfer=AsyncMock(return_value={"list": [], "total": 0}))
    application = FastAPI()
    application.include_router(history.router, prefix="/api/v1/history")
    application.dependency_overrides[history.verify_token] = lambda: None
    application.dependency_overrides[dependencies.get_history_query_service] = lambda: query
    parameters = {"title": "Movie*", "page": 2, "count": 5, "status": "true"}
    expected = {"title": "Movie*", "page": 2, "count": 5, "status": True}
    if origin is not None:
        parameters["origin"] = origin
        expected["origin"] = origin
    with TestClient(application) as client:
        response = client.get("/api/v1/history/transfer", params=parameters)
    assert response.status_code == 200
    assert response.json()["data"] == {"list": [], "total": 0}
    query.list_transfer.assert_awaited_once_with(**expected)


def test_registration_mcp_route_is_admin_idempotent_private_write():
    """
    MCP 只提供登记业务入口，声明私人结果和可重试的管理员数据库写操作。
    """
    from app.agent.policy.api import API_OPERATION_ROUTES, API_OPERATION_SPECS
    from app.agent.policy.contracts import ActionEffect, PrincipalRole, RecoveryMode

    route = API_OPERATION_ROUTES.get("transfer.history.register")
    assert route is not None
    assert route.method == "POST"
    assert route.path == "/api/v1/history/transfer"
    spec = next(spec for spec in API_OPERATION_SPECS if spec.operation_id == "transfer.history.register")
    assert spec.effect == ActionEffect.REVERSIBLE_WRITE
    assert spec.recovery == RecoveryMode.IDEMPOTENT
    assert spec.required_role == PrincipalRole.SYSTEM_ADMIN


def test_registration_atomic_insert_rejects_unknown_dialect(monkeypatch, registration_store):
    """
    未明确支持的数据库不能退化为先查后插或替换写入。
    """
    from app.db.oper.transferhistory import TransferHistoryOper

    sessions, _ = registration_store
    with sessions() as session:
        monkeypatch.setattr(session.get_bind().dialect, "name", "unsupported")
        with pytest.raises(RuntimeError, match="不支持原子"):
            TransferHistoryOper(session).stage_insert_if_source_absent({
                "src": "/a.mkv", "src_storage": "local",
            })


def test_registration_accepts_canonical_series_identity():
    """
    电视剧的季集范围与非 TMDB 原生媒体身份完整保留。
    """
    request = _request(type="电视剧", media_source="imdb", media_id="tt1234567", seasons="S01", episodes="E01-E12")
    assert request.media_id == "tt1234567"
    assert request.seasons == "S01"
    assert request.episodes == "E01-E12"


def test_move_accepts_missing_source_parent(tmp_path, registration_modules, registration_store):
    """
    移动后源父目录已清理也属于确认不存在，不要求收集该父目录的身份。
    """
    _, scope = registration_store
    src, dest = tmp_path / "removed-parent" / "source.mkv", tmp_path / "dest.mkv"
    dest.write_bytes(b"film")
    result = _command(registration_modules, scope).execute(
        _request(src=str(src), dest=str(dest), mode="move"), _actor(registration_modules),
    )
    assert result.history.registration["source_state"] == "absent"


@pytest.mark.parametrize("phase", ["stage", "commit"])
def test_registration_rolls_back_failed_write(
    phase, tmp_path, monkeypatch, registration_modules, registration_store,
):
    """
    写入或提交异常后数据库没有半条成功历史，调用方也拿不到成功回执。
    """
    from app.db.uow import SqlAlchemyUnitOfWork

    sessions, scope = registration_store
    src, dest = tmp_path / "a.mkv", tmp_path / "b.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    if phase == "stage":
        repository = registration_modules[1].SessionTransferRegistrationRepository
        original = repository.insert_if_absent

        def fail_after_insert(self, write):
            """
            在真实 INSERT 之后模拟后续投影失败。
            """
            original(self, write)
            raise RuntimeError("stage failed")

        monkeypatch.setattr(repository, "insert_if_absent", fail_after_insert)
    else:
        def fail_commit(_self):
            """
            在真实事务提交前模拟失败。
            """
            raise RuntimeError("commit failed")

        monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)
    with pytest.raises(RuntimeError, match=f"{phase} failed"):
        _command(registration_modules, scope).execute(
            _request(src=str(src), dest=str(dest)), _actor(registration_modules),
        )
    with sessions() as session:
        assert session.scalars(select(TransferHistory)).all() == []


@pytest.mark.parametrize("existing", [
    {"origin": None, "status": True},
    {"origin": "external", "status": False},
    {"origin": "external", "status": True, "transfer_task_id": "durable-task"},
])
def test_existing_internal_failure_and_task_history_are_protected(
    existing, registration_modules, registration_store,
):
    """
    无论目标事实如何，都不得替换内部、失败或持久任务绑定历史。
    """
    sessions, scope = registration_store
    with sessions() as session:
        record = TransferHistory(src="/downloads/a.mkv", src_storage="local", title="original", **existing)
        session.add(record)
        session.commit()
        original_id = record.id
    with pytest.raises(registration_modules[0].RegistrationConflictError):
        _command(registration_modules, scope).execute(_request(), _actor(registration_modules))
    with sessions() as session:
        rows = session.scalars(select(TransferHistory)).all()
        assert len(rows) == 1
        assert rows[0].id == original_id
        assert rows[0].title == "original"


@pytest.mark.parametrize("case", ["unsupported", "error", "wrong_path", "wrong_storage", "size", "nan", "remote_link"])
def test_unknown_or_inconsistent_provider_facts_fail_safely(
    case, registration_modules, registration_store,
):
    """
    远端 None、异常和错误文件项不能混同，云盘相同 fileid 也不能证明原生硬链接。
    """
    from app.schemas.exception import StorageQueryError

    sessions, scope = registration_store

    def query(storage, path):
        """
        模拟有明确失败或不一致回执的远端提供方。
        """
        if case in ("unsupported", "error"):
            raise StorageQueryError("private URL https://secret.invalid/?apikey=secret")
        changes = {
            "wrong_path": {"path": "/other.mkv"}, "wrong_storage": {"storage": "other"},
            "size": {"size": -1}, "nan": {"modify_time": float("nan")},
        }.get(case, {})
        return FileItem.model_validate({
            "path": str(path), "storage": storage, "type": "file", "size": 4, "fileid": "same",
        } | changes)

    with pytest.raises(registration_modules[0].RegistrationValidationError) as error:
        _command(registration_modules, scope, query).execute(
            _request(src_storage="u115", dest_storage="u115", mode="link" if case == "remote_link" else "copy"),
            _actor(registration_modules),
        )
    assert "secret" not in str(error.value)
    with sessions() as session:
        assert session.scalars(select(TransferHistory)).all() == []


def test_external_declaration_is_frozen_before_file_io(
    tmp_path, registration_modules, registration_store,
):
    """
    文件查询回调改变原请求也不能篡改已经冻结的媒体声明。
    """
    _, scope = registration_store
    src, dest = tmp_path / "a.mkv", tmp_path / "b.mkv"
    src.write_bytes(b"film")
    dest.write_bytes(b"film")
    request = _request(src=str(src), dest=str(dest))
    declaration = registration_modules[0].TransferRegistrationDeclaration.from_request(request)
    with pytest.raises(FrozenInstanceError):
        declaration.title = "changed"

    def query(storage, path):
        """
        模拟调用方在 I/O 期间仍持有并改动请求对象。
        """
        request.title = "changed"
        return _local_query(storage, path)

    result = _command(registration_modules, scope, query).execute(request, _actor(registration_modules))
    assert result.history.title == "A"


def test_registration_migration_preserves_old_rows_and_unique_source(tmp_path):
    """
    迁移可重放且保留旧行 NULL 来源、源地址唯一约束，降级不丢历史行。
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import Column, Index, Integer, MetaData, String, Table, inspect
    from sqlalchemy.exc import IntegrityError

    path = Path("database/versions/b849e613a72f_external_history_registration.py")
    spec = importlib.util.spec_from_file_location("registration_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    metadata = MetaData()
    table = Table(
        "transferhistory", metadata, Column("id", Integer, primary_key=True),
        Column("src", String), Column("src_storage", String),
        Index("ux_transferhistory_src_storage", "src", "src_storage", unique=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(src="/a.mkv", src_storage="local"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        reflected = Table("transferhistory", MetaData(), autoload_with=connection)
        row = connection.execute(select(reflected)).one()
        assert row.origin is None and row.registration is None
        connection.execute(reflected.insert().values(
            src="/b.mkv", src_storage="local", origin="external", registration={"actor_id": 1},
        ))
        with pytest.raises(IntegrityError):
            connection.execute(reflected.insert().values(src="/a.mkv", src_storage="local"))
        migration.downgrade()
        assert {column["name"] for column in inspect(connection).get_columns("transferhistory")} == {
            "id", "src", "src_storage",
        }
        assert len(connection.execute(select(table)).all()) == 2
    engine.dispose()


@pytest.mark.parametrize("locale", ["en-US", "zh-TW"])
def test_registration_errors_have_translations(locale):
    """
    登记的权限外错误也提供英文和繁体中文，不能向调用方泄漏存储原始异常。
    """
    from app.runtime.localization import LocaleHelper

    for message in (
        "该源地址已有内部整理或失败历史，不能覆盖",
        "该源地址的历史不属于可重复的外部登记",
        "该源地址已有不同目标或媒体声明的历史，不能覆盖",
        "无法确认存储文件状态，请检查存储后重试",
        "外部登记只接受普通文件，不接受符号链接或目录",
        "移动登记要求源文件已经不存在",
        "当前仅支持核验本地存储的原生硬链接",
    ):
        assert LocaleHelper.translate_text(message, locale=locale) != message
