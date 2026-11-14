"""
小组件授权与快照的真实隔离数据库、HTTP 边界及来源投影回归。
"""

import asyncio
import hashlib
import importlib
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.web.security.access import verify_token
from app.api.dependencies import dashboard as dashboard_dependencies
from app.api.dependencies import widget as dependencies
from app.api.dependencies.auth import get_current_active_user, get_current_user
from app.api.dependencies.widget import get_widget_auth_service, get_widget_snapshot_service
from app.api.endpoints import widget as endpoint
from app.application.configuration import ApiRuntimeConfig
from app.application.dashboard import DashboardQueryService
from app.application.history import HistoryQueryService
from app.application.query import DataQueryService
from app.application.security.widget import (
    WIDGET_CREDENTIAL_LIFETIME,
    WIDGET_REGISTRATION_LIMIT,
    WidgetAccessError,
    WidgetAuthService,
    available_widget_contents,
)
from app.application.widget import WIDGET_SNAPSHOT_MAX_BYTES, WidgetSnapshotService
from app.chain import dashboard as dashboard_chain
from app.chain.download.tasks import DownloadTaskOwner
from app.db.adapters.history.transfer import TransactionalTransferHistoryRepository
from app.db.adapters.query import SqlAlchemyDataQueryAdapter
from app.db.adapters.widget import SqlAlchemyWidgetUnitOfWork, widget_scope
from app.db.base import Base
from app.db.models import load_all_models
from app.db.models.subscribe import Subscribe
from app.db.models.transferhistory import TransferHistory
from app.db.models.user import User
from app.db.models.widget import WidgetRegistration
from app.schemas.dashboard import DashboardMemoryInfo, DownloaderInfo
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaType


class _ThreadExecutor:
    """
    在独立工作线程运行同步查询，验证不会跨任务复用会话。
    """

    async def run(self, operation):
        """
        等待使用自有短 Session 的查询完成。

        :param operation: 本次独占的同步查询回调
        :return: 回调已复制的查询结果
        """
        return await asyncio.to_thread(operation)


@pytest.fixture
def widget_case(tmp_path):
    """
    独立于用户及共享 harness 的真实 SQLite，每次测试创建两用户和全部表。
    """
    database = tmp_path / "widget.sqlite"
    engine = sa.create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False, "timeout": 15})

    @sa.event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        """
        让测试实际执行账号删除级联，不依赖 ORM 模拟。
        """
        connection.execute("PRAGMA foreign_keys=ON")

    load_all_models()
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async_factory = async_sessionmaker(async_engine)
    owners = (uuid4().hex, uuid4().hex)
    with factory() as session:
        for identity, name, owner in ((101, "alice", owners[0]), (102, "bob", owners[1])):
            session.add(
                User(
                    id=identity,
                    name=name,
                    notification_identity=owner,
                    is_active=True,
                    permissions={"manage": True, "subscribe": True},
                )
            )
        session.commit()
    clock = [1792850000.0]
    auth = WidgetAuthService(partial(widget_scope, factory), clock=lambda: clock[0])
    adapter = SqlAlchemyDataQueryAdapter(factory)
    history_repository = TransactionalTransferHistoryRepository(sync_session=factory, async_session=async_factory)
    data = DataQueryService(subscriptions=adapter, histories=adapter, async_executor=_ThreadExecutor())
    history = HistoryQueryService(download_repository=Mock(), transfer_repository=history_repository)
    system = Mock(return_value=(24.5, DashboardMemoryInfo(total=1000, available=400), [2048, 4096]))
    downloads = Mock(return_value=[])
    downloader = Mock(return_value=DownloaderInfo(download_speed=2048, upload_speed=1024, free_space=4096))
    snapshots = WidgetSnapshotService(
        data=data, history=history, system=system, downloads=downloads,
        downloader=downloader, clock=lambda: clock[0]
    )
    case = SimpleNamespace(
        auth=auth,
        factory=factory,
        clock=clock,
        owners=owners,
        snapshots=snapshots,
        system=system,
        downloads=downloads,
        downloader=downloader,
        data=data,
        history=history,
    )
    yield case
    asyncio.run(async_engine.dispose())
    engine.dispose()


def _register(case, user_id=101, installation_id=None):
    """
    用真实事务给指定测试账号登记，不模拟专用授权服务。
    """
    owner = case.owners[user_id - 101]
    return case.auth.register(user_id, owner, installation_id or uuid4())


def _update_user(case, user_id=101, **fields):
    """
    在独立事务修改账号事实，验证快照不会使用过期权限缓存。
    """
    with case.factory() as session:
        session.execute(sa.update(User).where(User.id == user_id).values(**fields))
        session.commit()


def _expect_error(status, operation, *args):
    """
    同时检查安全失败状态，不能只匹配异常类型掩盖越权原因。
    """
    with pytest.raises(WidgetAccessError) as caught:
        operation(*args)
    assert caught.value.status == status


def test_widget_context_reads_current_permissions_without_issuing_or_rotating(widget_case):
    """
    未启用也可核对当前账号；上下文不登记安装、轮换密钥或延长已有授权。
    """
    case = widget_case
    result = case.auth.context(101, case.owners[0])
    assert result.model_dump() == {
        "version": 1,
        "owner": case.owners[0],
        "availableContents": ["downloads", "imports", "subscriptions"],
    }
    with case.factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(WidgetRegistration)) == 0
    issued = _register(case)
    _update_user(case, permissions={"subscribe": True, "features": {"subscribe.movie": False, "subscribe.music": False}})
    case.clock[0] += 120
    assert case.auth.context(101, issued.owner).availableContents == ["subscriptions"]
    current = case.auth.authenticate(issued.secret)
    assert current.registration.registration_id == str(issued.registrationId)
    assert current.registration.expires_at == issued.expiresAt
    with case.factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(WidgetRegistration)) == 1
    _update_user(case, permissions={})
    assert case.auth.context(101, issued.owner).availableContents == []


@pytest.mark.parametrize("change", ["inactive", "deleted", "identity"])
def test_widget_context_rejects_stale_main_account_identity(widget_case, change):
    """
    主登录校验后的并发禁用、删除和身份重建不能被上下文读取掩盖。
    """
    case = widget_case
    if change == "deleted":
        with case.factory() as session:
            session.execute(sa.delete(User).where(User.id == 101))
            session.commit()
    elif change == "identity":
        _update_user(case, notification_identity=uuid4().hex)
    else:
        _update_user(case, is_active=False)
    _expect_error(401, case.auth.context, 101, case.owners[0])


def test_widget_issue_rotation_owner_and_self_revoke(widget_case):
    """
    同安装隔离用户、轮换只撤销旧凭据、自撤销只影响自身，数据库没有原文。
    """
    case = widget_case
    installation = uuid4()
    first = _register(case, installation_id=installation)
    other = _register(case, 102, installation)
    second_install = _register(case)
    token = first.secret
    assert len(token.rsplit(".", 1)[1]) == 43
    assert token not in repr(first)
    assert first.owner == case.owners[0]
    assert first.expiresAt == case.clock[0] + WIDGET_CREDENTIAL_LIFETIME
    with case.factory() as session:
        row = session.scalar(
            sa.select(WidgetRegistration).where(WidgetRegistration.registration_id == str(first.registrationId))
        )
        assert row.secret_digest == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(row.to_dict())
    _expect_error(404, case.auth.revoke, 102, other.owner, first.registrationId)
    replacement = _register(case, installation_id=installation)
    assert replacement.registrationId != first.registrationId
    _expect_error(401, case.auth.authenticate, token)
    assert case.auth.authenticate(other.secret).user.name == "bob"
    case.auth.revalidate(case.auth.authenticate(replacement.secret))
    case.auth.revoke_self(case.auth.authenticate(replacement.secret))
    _expect_error(401, case.auth.authenticate, replacement.secret)
    assert case.auth.authenticate(second_install.secret).user.name == "alice"


@pytest.mark.parametrize("change", ["inactive", "identity", "deleted", "expired", "permissions"])
def test_widget_current_account_and_expiry_are_always_checked(widget_case, change):
    """
    账号禁用、身份重建、删除、到期和权限撤回立即失效，不等待缓存过期。
    """
    case = widget_case
    issued = _register(case)
    grant = case.auth.authenticate(issued.secret)
    if change == "expired":
        case.clock[0] = issued.expiresAt
    elif change == "deleted":
        with case.factory() as session:
            session.execute(sa.delete(User).where(User.id == 101))
            session.commit()
            assert session.scalar(sa.select(sa.func.count()).select_from(WidgetRegistration)) == 0
            session.add(User(id=101, name="alice", is_active=True, notification_identity=uuid4().hex))
            session.commit()
    else:
        fields = {
            "inactive": {"is_active": False},
            "identity": {"notification_identity": uuid4().hex},
            "permissions": {"permissions": {"subscribe": False, "manage": False, "admin": True}},
        }
        _update_user(case, **fields[change])
    _expect_error(403 if change == "permissions" else 401, case.auth.authenticate, issued.secret)
    _expect_error(403 if change == "permissions" else 401, case.auth.revalidate, grant)


def test_widget_latest_features_rename_and_forged_secrets(widget_case):
    """
    普通用户的 admin 不提权；功能缺省继承分类，改名后按新 owner 名读取。
    """
    case = widget_case
    issued = _register(case)
    grant = case.auth.authenticate(issued.secret)
    _update_user(
        case,
        name="renamed",
        permissions={"subscribe": True, "admin": True, "features": {"subscribe.movie": False, "subscribe.tv": False}},
    )
    current = case.auth.authenticate(issued.secret)
    assert current.user.name == "renamed"
    assert available_widget_contents(current.user) == ["subscriptions"]
    _expect_error(403, case.auth.revalidate, grant)
    for invalid in ("", "jwt.token.parts", issued.secret[:-2] + "xx", f"mpw_{'-' * 36}.{'a' * 43}"):
        _expect_error(401, case.auth.authenticate, invalid)
    _update_user(case, notification_identity="")
    _expect_error(401, case.auth.register, 101, "", uuid4())


def test_widget_no_content_cannot_create_or_renew_authorization(widget_case):
    """
    没有分类权限时不颁发空授权，禁止通过自续期恢复被撤销的功能。
    """
    case = widget_case
    _update_user(case, permissions={})
    _expect_error(403, _register, case)
    _expect_error(401, case.auth.register, 999, uuid4().hex, uuid4())


def test_widget_limit_concurrent_claims_and_expired_slot_reuse(widget_case):
    """
    真实并发事务不能绕过名额；同安装轮换不占新名额，过期记录可以清理。
    """
    case = widget_case
    first = _register(case)
    for _ in range(WIDGET_REGISTRATION_LIMIT - 2):
        _register(case)
    barrier = Barrier(2)

    def claim():
        """
        同时争取最后一个名额，返回安全失败或已提交凭据。
        """
        barrier.wait(timeout=10)
        try:
            return _register(case)
        except WidgetAccessError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result == 409 for result in results) == 1
    replacement = _register(case, installation_id=first.installationId)
    assert case.auth.authenticate(replacement.secret)
    case.clock[0] = replacement.expiresAt
    assert _register(case)
    with case.factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(WidgetRegistration)) == 1


def test_widget_commit_failure_does_not_replace_live_authorization(widget_case, monkeypatch):
    """
    轮换提交失败必须整体回滚，不能发布未落地凭据或破坏现有安装。
    """
    case = widget_case
    first = _register(case)
    commit = Mock(side_effect=RuntimeError("模拟事务失败"))
    monkeypatch.setattr(SqlAlchemyWidgetUnitOfWork, "commit", commit)
    with pytest.raises(RuntimeError, match="模拟事务失败"):
        _register(case, installation_id=first.installationId)
    assert case.auth.authenticate(first.secret)


def test_widget_snapshot_uses_real_owner_type_counts_and_success_history(widget_case):
    """
    查询真实 SQLite 验证 owner、类型、稳定顺序、六行上限和准确总数。
    """
    case = widget_case
    with case.factory() as session:
        for index in range(9):
            for name, kind in (("alice", MediaType.MOVIE), ("alice", MediaType.TV), ("bob", MediaType.MOVIE)):
                session.add(
                    Subscribe(
                        name=f"{name}-{kind.value}-{index}",
                        username=name,
                        type=kind.value,
                        date=f"2026-10-{index + 1:02d} 12:00:00",
                        media_source="tmdb",
                        media_id=f"{index}-{kind.value}",
                        save_path="/private/path",
                    )
                )
            session.add(TransferHistory(title=f"成功-{index}", status=True, date=f"2026-10-{index + 1:02d} 12:00:00"))
        session.add(TransferHistory(title="失败记录不能作为入库", status=False))
        session.commit()
    case.downloads.return_value = [
        DownloaderTorrent(
            title="alice 下载", userid="alice", progress=25, dlspeed="2 MB/s", hash="secret-hash", save_path="/secret"
        ),
        DownloaderTorrent(title="bob 下载", userid="bob", progress=55),
        DownloaderTorrent(title="无归属不泄露", progress=10),
        DownloaderTorrent(title="用户名精确匹配", username="alice", progress=0),
    ]
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    sections = {section.content: section for section in result.sections}
    assert "overview" not in sections
    assert {metric.id: metric.value for metric in sections["subscriptions"].metrics} == {
        "count": "18", "typeMOVIE": "9", "typeTV": "9", "typeMUSIC": "0",
        "stateN": "18", "stateR": "0", "stateS": "0", "stateP": "0",
    }
    assert len(sections["subscriptions"].items) == 6
    assert sections["subscriptions"].items[0].title.startswith("alice-")
    assert sections["subscriptions"].items[0].title.endswith("-8")
    assert sections["subscriptions"].state == "ready"
    assert sections["imports"].metrics[0].value == "9"
    assert len(sections["imports"].items) == 6
    assert sections["downloads"].metrics[0].value == "2"
    assert sections["downloads"].items[0].progress == 0.25
    case.downloader.assert_not_called()
    encoded = result.model_dump_json(exclude_none=True)
    assert all(secret not in encoded for secret in ("bob", "secret-hash", "/secret", "/private/path", "失败记录"))
    assert result.expiresAt - result.generatedAt == 86400


def test_widget_partial_failures_and_utf8_budget(widget_case):
    """
    来源错误独立 unavailable，成功空结果保留真实时间，长中文不能越界或输出控制字符。
    """
    case = widget_case
    issued = _register(case)
    case.data.async_list_subscriptions = AsyncMock(side_effect=RuntimeError("secret-url?apikey=private"))
    case.downloads.return_value = [
        DownloaderTorrent(title="🙂" * 300 + "\n\x00", userid="alice", dlspeed="速" * 600, progress=50)
    ]
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(issued.secret)))
    sections = {section.content: section for section in result.sections}
    assert sections["subscriptions"].state == "unavailable"
    assert sections["subscriptions"].updatedAt is None
    assert not sections["subscriptions"].items and not sections["subscriptions"].metrics
    assert sections["imports"].state == "empty" and sections["imports"].updatedAt == case.clock[0]
    item = sections["downloads"].items[0]
    assert len(item.title.encode()) <= 240 and len(item.subtitle.encode()) <= 512
    assert "\n" not in item.title and "\x00" not in item.title
    assert "secret-url" not in result.model_dump_json()
    assert len(result.model_dump_json().encode()) < WIDGET_SNAPSHOT_MAX_BYTES
    case.downloads.return_value = None
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(issued.secret)))
    assert next(section for section in result.sections if section.content == "downloads").state == "unavailable"


@pytest.mark.parametrize("source_failure", [False, True])
def test_widget_admin_overview_and_all_download_owners(widget_case, source_failure):
    """
    管理员沿用全局来源，统计或容量无效时不显示成功的零；其它来源继续有效。
    """
    case = widget_case
    _update_user(case, is_superuser=True)
    issued = _register(case)
    case.downloads.return_value = [DownloaderTorrent(title="bob 下载", userid="bob")]
    if source_failure:
        case.system.return_value = (24.5, DashboardMemoryInfo(total=100, available=200), [1, 2])
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(issued.secret)))
    overview = result.sections[0]
    assert len(result.sections) == 4
    if source_failure:
        assert overview.state == "unavailable" and overview.updatedAt is None
    else:
        assert overview.metrics[0].value == "24.5%"
        assert overview.metrics[1].value == "60.0%"
        assert overview.metrics[2].value == "4.00 KB/s"
        assert len(overview.metrics) == 6
    assert result.sections[1].items[0].title == "bob 下载"
    assert [(metric.id, metric.value) for metric in result.sections[1].metrics] == [
        ("downloadSpeed", "2.00 KB/s"), ("uploadSpeed", "1.00 KB/s"), ("freeSpace", "4.00 KB"), ("count", "1"),
        ("statedownloading", "1"), ("statepaused", "0")
    ]
    case.downloader.assert_called_once_with()
    case.system.assert_called_once_with()


@pytest.mark.parametrize("free_space, expected", [(0, "0 B"), (None, "—"), (-1, "—"), (float("nan"), "—")])
def test_widget_download_metrics_without_tasks_and_unknown_disk(widget_case, free_space, expected):
    """
    无下载仍展示真实零下载和做种上传，容量未知不误显示磁盘满。
    """
    case = widget_case
    _update_user(case, is_superuser=True)
    main_thread = get_ident()

    def sample():
        """
        断言磁盘和下载器采样离开事件循环线程，再返回原始字节。
        """
        assert get_ident() != main_thread
        return DownloaderInfo(download_speed=0, upload_speed=1048576, free_space=free_space)

    case.downloader.side_effect = sample
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    section = result.sections[1]
    assert section.state == "ready" and section.updatedAt == case.clock[0] and not section.items
    assert [metric.value for metric in section.metrics] == ["0 B/s", "1.00 MB/s", expected, "0", "0", "0"]


@pytest.mark.parametrize("field", ["download_speed", "upload_speed"])
@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf")])
def test_widget_invalid_global_speed_is_independent_unavailable(widget_case, field, value):
    """
    非法全局速率只使下载节不可用，不产生新鲜零值或影响其它成功来源。
    """
    case = widget_case
    _update_user(case, is_superuser=True)
    case.downloader.return_value = DownloaderInfo(**{field: value})
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    section = result.sections[1]
    assert section.state == "unavailable" and section.updatedAt is None
    assert not section.metrics and not section.items
    assert result.sections[0].state == "ready" and result.sections[2].state == "empty"


def test_widget_global_speed_source_failure_does_not_leak_to_ordinary_user(widget_case):
    """
    普通用户只看到本人任务，管理员全局采样失败不影响这个权限边界。
    """
    case = widget_case
    case.downloader.side_effect = RuntimeError("private-download-host")
    case.downloads.return_value = [
        DownloaderTorrent(title="本人", userid="alice"), DownloaderTorrent(title="其他用户", userid="bob")
    ]
    grant = case.auth.authenticate(_register(case).secret)
    result = asyncio.run(case.snapshots.snapshot(grant))
    assert [metric.id for metric in result.sections[0].metrics] == ["count", "statedownloading", "statepaused"]
    assert [item.title for item in result.sections[0].items] == ["本人"]
    case.downloader.assert_not_called()
    _update_user(case, is_superuser=True)
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    assert result.sections[1].state == "unavailable"
    assert result.sections[0].state == "ready"
    assert "private-download-host" not in result.model_dump_json()


def test_widget_snapshot_expiry_and_invalid_source_progress(widget_case):
    """
    采样跨过凭据到期不能延长展示期，非有限或越界进度不能进入 Swift 模型。
    """
    case = widget_case
    issued = _register(case)
    grant = case.auth.authenticate(issued.secret)
    for progress in (float("nan"), -1, 101):
        case.downloads.return_value = [DownloaderTorrent(title="下载", username="alice", progress=progress)]
        result = asyncio.run(case.snapshots.snapshot(grant))
        assert result.sections[0].state == "unavailable"
    case.clock[0] = issued.expiresAt
    with pytest.raises(ValueError, match="到期"):
        asyncio.run(case.snapshots.snapshot(grant))


def test_widget_dependency_assembly_and_absent_capability(widget_case, monkeypatch):
    """
    验证真实依赖工厂只连接既有业务能力，缺少显式存储端口时安全拒绝。
    """
    case = widget_case
    runtime = SimpleNamespace(authentication=SimpleNamespace(widget_scope=partial(widget_scope, case.factory)))
    service = dependencies.get_widget_auth_service(runtime)
    assert isinstance(service, WidgetAuthService)
    runtime.authentication.widget_scope = None
    with pytest.raises(HTTPException) as caught:
        dependencies.get_widget_auth_service(runtime)
    assert caught.value.status_code == 503
    monkeypatch.setattr(dependencies, "get_configured_data_query_service", lambda: case.data)
    download_chain = Mock()
    download_chain.downloading.return_value = []
    monkeypatch.setattr(dependencies, "DownloadChain", lambda: download_chain)
    monkeypatch.setattr(dependencies, "sample_system", case.system)
    sampler = Mock(return_value=DownloaderInfo())
    monkeypatch.setattr(dependencies, "sample_downloader", sampler)
    snapshots = dependencies.get_widget_snapshot_service(case.history, ApiRuntimeConfig(60, True, False))
    result = asyncio.run(snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    assert result.sections[0].state == "empty"
    download_chain.downloading.assert_called_once_with(require_source=True)
    sampler.assert_not_called()
    _update_user(case, is_superuser=True)
    asyncio.run(snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    sampler.assert_called_once_with(btrfs_fsid_dedup=True, require_source=True)


def test_widget_strict_sources_preserve_regular_page_fallbacks(monkeypatch):
    """
    原页面保留空来源兼容行为，只有小组件显式严格读取才报告不可用。
    """
    statistics = DashboardQueryService(repository=Mock(), media_statistics=lambda _name: None)
    assert statistics.statistic().movie_count == 0
    with pytest.raises(ValueError, match="暂不可用"):
        statistics.statistic(require_source=True)
    monkeypatch.setattr(dashboard_chain.DirectoryHelper, "get_dirs", lambda _self: [])
    assert dashboard_chain.DashboardChain().storage().total_storage == 0
    with pytest.raises(ValueError, match="尚未配置"):
        dashboard_chain.DashboardChain().storage(require_source=True)
    directory = SimpleNamespace(storage="local", download_path="/download", library_storage=None, library_path=None)
    monkeypatch.setattr(dashboard_chain.DirectoryHelper, "get_dirs", lambda _self: [directory])
    monkeypatch.setattr(dashboard_chain.StorageChain, "manage_storage", lambda _self, **_kw: {"success": False})
    assert dashboard_chain.DashboardChain().storage().total_storage == 0
    with pytest.raises(ValueError, match="暂不可用"):
        dashboard_chain.DashboardChain().storage(require_source=True)
    owner = SimpleNamespace(
        list_torrents=Mock(return_value=None),
        download_history_repository=SimpleNamespace(get_by_hashes=Mock(return_value={})),
        start_torrents=Mock(),
        stop_torrents=Mock(),
        remove_torrents=Mock(),
    )
    assert DownloadTaskOwner._download_task_service(owner).downloading() == []
    with pytest.raises(ValueError, match="暂不可用"):
        DownloadTaskOwner._download_task_service(owner, require_source=True).downloading()
    owner.list_torrents.return_value = []
    assert DownloadTaskOwner._download_task_service(owner, require_source=True).downloading() == []


def _client(case):
    """
    构造真实专用路由，主认证仅在登记测试注入，普通令牌验证保持原实现。
    """
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/api/v1/widget")
    app.dependency_overrides[get_widget_auth_service] = lambda: case.auth
    app.dependency_overrides[get_widget_snapshot_service] = lambda: case.snapshots

    @app.get("/ordinary")
    def ordinary(_principal=Depends(verify_token)):
        """
        保留真实普通令牌依赖，验证专用 Bearer 不能调用业务 API。
        """
        return {"success": True}

    return TestClient(app)


def test_widget_http_context_requires_main_login_and_never_exports_credentials(widget_case):
    """
    真实路由拒绝组件凭据，只返回当前账号最小上下文，错误及成功均不泄露密钥。
    """
    case = widget_case
    issued = _register(case)
    with _client(case) as client:
        def main_user(_token=Depends(verify_token)):
            """
            隔离宿主数据库装配，但保留真实主令牌验证及原激活账号检查。
            """
            return SimpleNamespace(id=101, notification_identity=case.owners[0], is_active=True)

        client.app.dependency_overrides[get_current_user] = main_user
        for headers in ({}, {"Authorization": f"Bearer {issued.secret}"}):
            response = client.get("/api/v1/widget/context", headers=headers)
            assert response.status_code == 401
            assert issued.secret not in response.text
        client.app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
            id=101, notification_identity=case.owners[0]
        )
        response = client.get(f"/api/v1/widget/context?owner={case.owners[1]}")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store, private"
        assert response.headers["Vary"] == "Authorization"
        assert response.json()["data"] == case.auth.context(101, issued.owner).model_dump()
        assert set(response.json()["data"]) == {"version", "owner", "availableContents"}
        assert issued.secret not in response.text
        assert str(issued.registrationId) not in response.text
        assert case.auth.authenticate(issued.secret).registration.expires_at == issued.expiresAt
        _update_user(case, notification_identity=uuid4().hex)
        response = client.get("/api/v1/widget/context")
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store, private"
        assert "owner" not in response.json()


def test_widget_http_header_only_no_cache_and_no_general_api_access(widget_case):
    """
    真实 HTTP 拒绝查询、Cookie、通用 API Key 和登录 JWT；只读凭据不能转成业务认证。
    """
    case = widget_case
    issued = _register(case)
    with _client(case) as client:
        assert client.get("/ordinary", headers={"Authorization": f"Bearer {issued.secret}"}).status_code == 401
        for headers in (
            {},
            {"X-API-KEY": issued.secret},
            {"Cookie": f"token={issued.secret}"},
            {"Authorization": "Bearer main.jwt.token"},
        ):
            response = client.get(
                f"/api/v1/widget/snapshot?token={issued.secret}&apikey={issued.secret}", headers=headers
            )
            assert response.status_code == 401
            assert response.headers["Cache-Control"] == "no-store, private"
            assert issued.secret not in response.text
        headers = {"Authorization": f"Bearer {issued.secret}"}
        result = client.get("/api/v1/widget/snapshot", headers=headers)
        assert result.status_code == 200 and result.json()["owner"] == issued.owner
        assert "data" not in result.json()
        assert result.headers["Cache-Control"] == "no-store, private"
        assert len(result.content) <= WIDGET_SNAPSHOT_MAX_BYTES
        assert client.delete("/api/v1/widget/registration", headers=headers).status_code == 200
        assert client.get("/api/v1/widget/snapshot", headers=headers).status_code == 401


def test_widget_http_registration_validation_and_revoke_while_loading(widget_case):
    """
    不允许指定他人 owner；来源等待期间的撤销必须阻止数据响应。
    """
    case = widget_case
    issued = _register(case)
    grant = case.auth.authenticate(issued.secret)
    with _client(case) as client:
        client.app.dependency_overrides[get_current_active_user] = lambda: grant.user
        response = client.post(
            "/api/v1/widget/registrations", json={"installationId": str(uuid4()), "owner": case.owners[1]}
        )
        assert response.status_code == 422
        response = client.post("/api/v1/widget/registrations", json={"installationId": str(uuid4())})
        assert response.status_code == 200
        new_id = response.json()["data"]["registrationId"]
        assert client.delete(f"/api/v1/widget/registrations/{new_id}").status_code == 200
        assert client.delete(f"/api/v1/widget/registrations/{uuid4()}").status_code == 404
        original = case.snapshots.snapshot

        async def revoke_during_snapshot(current):
            """
            模拟正在采样时前台关闭授权，保留已读数据等待最终鉴权丢弃。
            """
            result = await original(current)
            case.auth.revoke_self(current)
            return result

        case.snapshots.snapshot = revoke_during_snapshot
        response = client.get("/api/v1/widget/snapshot", headers={"Authorization": f"Bearer {issued.secret}"})
        assert response.status_code == 401
        assert "sections" not in response.json()


def test_widget_http_errors_never_publish_unvalidated_snapshot(widget_case):
    """
    登记权限失败与快照体积错误均安全拒绝，错误不携带内容或一次性凭据。
    """
    case = widget_case
    issued = _register(case)
    grant = case.auth.authenticate(issued.secret)
    with _client(case) as client:
        client.app.dependency_overrides[get_current_active_user] = lambda: grant.user
        _update_user(case, permissions={})
        assert client.post("/api/v1/widget/registrations", json={"installationId": str(uuid4())}).status_code == 403
        assert client.delete(f"/api/v1/widget/registrations/{uuid4()}").status_code == 404
        _update_user(case, permissions={"manage": True})
        case.snapshots.snapshot = AsyncMock(side_effect=ValueError("secret internal error"))
        response = client.get("/api/v1/widget/snapshot", headers={"Authorization": f"Bearer {issued.secret}"})
        assert response.status_code == 503
        assert "secret internal error" not in response.text


@pytest.mark.parametrize("already_created", [False, True])
def test_widget_migration_is_replay_safe_and_preserves_users(monkeypatch, already_created):
    """
    迁移支持旧库与新 ORM 已建表，升级和回滚都不改变已有用户内容。
    """
    migration = importlib.import_module("database.versions.4f62a73e81c9_3_1_9")
    engine = sa.create_engine("sqlite://")
    User.__table__.create(engine)
    if already_created:
        WidgetRegistration.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(sa.insert(User).values(id=1, name="existing", notification_identity=uuid4().hex))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        assert sa.inspect(connection).has_table("widgetregistration")
        assert {
            tuple(item["column_names"]) for item in sa.inspect(connection).get_unique_constraints("widgetregistration")
        } == {
            ("registration_id",),
            ("user_id", "installation_id"),
        }
        migration.downgrade()
        migration.downgrade()
        assert not sa.inspect(connection).has_table("widgetregistration")
        assert connection.execute(sa.select(User.name)).scalar_one() == "existing"
    engine.dispose()


def test_widget_combined_subscription_counts_all_rows_and_excludes_disabled_types(widget_case):
    """
    合并组件的类别和状态数量覆盖全库作用域，权限关闭的类型与他人记录不能影响统计。
    """
    case = widget_case
    with case.factory() as session:
        for index in range(12):
            session.add(Subscribe(name=f"剧集-{index}", type=MediaType.TV.value, username="alice",
                                  state=("N", "R", "S", "P")[index % 4], season=2, total_episode=20,
                                  lack_episode=7, quality="WEB-DL", resolution="1080p"))
        session.add(Subscribe(name="无权限的电影", type=MediaType.MOVIE.value, username="alice"))
        session.add(Subscribe(name="另一用户", type=MediaType.TV.value, username="bob"))
        session.commit()
    _update_user(case, permissions={"subscribe": True, "features": {"subscribe.movie": False}})
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    assert [section.content for section in result.sections] == ["subscriptions"]
    section = result.sections[0]
    assert {metric.id: metric.value for metric in section.metrics} == {
        "count": "12", "typeTV": "12", "typeMUSIC": "0",
        "stateN": "3", "stateR": "3", "stateS": "3", "stateP": "3",
    }
    assert len(section.items) == 6
    assert section.items[0].progress == 0.65
    assert "S02" in section.items[0].subtitle and "13/20 集" in section.items[0].subtitle
    assert "WEB-DL" in section.items[0].subtitle
    assert "无权限" not in result.model_dump_json() and "另一用户" not in result.model_dump_json()


def test_widget_import_type_totals_are_not_the_six_recent_rows(widget_case):
    """
    入库类型统计使用完整成功历史，失败记录不计入；大卡片仍只投影最近六项。
    """
    case = widget_case
    with case.factory() as session:
        for kind, count in ((MediaType.MOVIE, 8), (MediaType.TV, 7), (MediaType.MUSIC, 3)):
            for index in range(count):
                session.add(TransferHistory(title=f"{kind.value}-{index}", type=kind.value, status=True))
            session.add(TransferHistory(title="失败", type=kind.value, status=False))
        session.commit()
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    section = next(section for section in result.sections if section.content == "imports")
    assert {metric.id: metric.value for metric in section.metrics} == {
        "count": "18", "typeMOVIE": "8", "typeTV": "7", "typeMUSIC": "3",
    }
    assert len(section.items) == 6


@pytest.mark.parametrize("cpu,total,available,network", [
    (float("nan"), 100, 10, [1, 2]), (-1, 100, 10, [1, 2]),
    (10, 0, 0, [1, 2]), (10, 100, 101, [1, 2]), (10, 100, 10, [-1, 2]), (10, 100, 10, [1]),
])
def test_widget_invalid_system_samples_do_not_fabricate_values(widget_case, cpu, total, available, network):
    """
    系统无效采样必须独立失败，不能用媒体数量或伪造零值代替。
    """
    case = widget_case
    _update_user(case, is_superuser=True)
    case.system.return_value = (cpu, DashboardMemoryInfo(total=total, available=available), network)
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    overview = result.sections[0]
    assert overview.state == "unavailable" and not overview.metrics and overview.updatedAt is None
    assert result.sections[1].state == "ready"


def test_widget_system_sampler_reuses_existing_sources_in_order(monkeypatch):
    """
    CPU 两次读取围绕同一网络采样区间，系统内存沿用仪表盘来源而非进程内存比例。
    """
    source = Mock()
    source.cpu_usage.side_effect = [0.0, 42.0]
    source.network_usage.return_value = [1024, 2048]
    source.memory_usage.return_value = DashboardMemoryInfo(total=100, available=30, used=5, usage=5)
    monkeypatch.setattr(dashboard_dependencies, "SystemUtils", source)
    cpu, memory, network = dashboard_dependencies.sample_system()
    assert cpu == 42 and network == [1024, 2048] and memory.available == 30
    assert [call[0] for call in source.mock_calls] == ["cpu_usage", "network_usage", "cpu_usage", "memory_usage"]
