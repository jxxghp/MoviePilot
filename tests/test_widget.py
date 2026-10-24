"""
小组件授权与快照的真实隔离数据库、HTTP 边界及来源投影回归。
"""

import asyncio
import hashlib
import importlib
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier
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
from app.api.dependencies import widget as dependencies
from app.api.dependencies.auth import get_current_active_user
from app.api.dependencies.widget import get_widget_auth_service, get_widget_snapshot_service
from app.api.endpoints import widget as endpoint
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
from app.schemas.dashboard import Statistic, Storage
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
    dashboard = DashboardQueryService(repository=Mock(), media_statistics=Mock(return_value=[Statistic(movie_count=7)]))
    dashboard.statistic = Mock(return_value=Statistic(movie_count=7, tv_count=3, episode_count=None, music_count=2))
    downloads = Mock(return_value=[])
    storage = Mock(return_value=Storage(total_storage=1000, used_storage=600))
    snapshots = WidgetSnapshotService(
        data=data, history=history, dashboard=dashboard, downloads=downloads, storage=storage, clock=lambda: clock[0]
    )
    case = SimpleNamespace(
        auth=auth,
        factory=factory,
        clock=clock,
        owners=owners,
        snapshots=snapshots,
        dashboard=dashboard,
        downloads=downloads,
        storage=storage,
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
    assert available_widget_contents(current.user) == ["musicSubscriptions"]
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
    assert sections["movieSubscriptions"].metrics[0].value == "9"
    assert sections["tvSubscriptions"].metrics[0].value == "9"
    assert len(sections["movieSubscriptions"].items) == 6
    assert sections["movieSubscriptions"].items[0].title == "alice-电影-8"
    assert sections["musicSubscriptions"].state == "empty"
    assert sections["imports"].metrics[0].value == "9"
    assert len(sections["imports"].items) == 6
    assert sections["downloads"].metrics[0].value == "2"
    assert sections["downloads"].items[0].progress == 0.25
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
    assert sections["movieSubscriptions"].state == "unavailable"
    assert sections["movieSubscriptions"].updatedAt is None
    assert not sections["movieSubscriptions"].items and not sections["movieSubscriptions"].metrics
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
        case.storage.return_value = Storage(total_storage=100, used_storage=200)
    result = asyncio.run(case.snapshots.snapshot(case.auth.authenticate(issued.secret)))
    overview = result.sections[0]
    assert len(result.sections) == 6
    if source_failure:
        assert overview.state == "unavailable" and overview.updatedAt is None
    else:
        assert overview.metrics[0].value == "7"
        assert overview.metrics[2].value == "—"
        assert len(overview.metrics) == 6
    assert result.sections[1].items[0].title == "bob 下载"
    case.dashboard.statistic.assert_called_once_with(require_source=True)


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
    dashboard_chain = Mock()
    dashboard_chain.storage.return_value = Storage(total_storage=1, used_storage=0)
    monkeypatch.setattr(dependencies, "DownloadChain", lambda: download_chain)
    monkeypatch.setattr(dependencies, "DashboardChain", lambda: dashboard_chain)
    snapshots = dependencies.get_widget_snapshot_service(case.dashboard, case.history)
    result = asyncio.run(snapshots.snapshot(case.auth.authenticate(_register(case).secret)))
    assert result.sections[0].state == "empty"
    download_chain.downloading.assert_called_once_with(require_source=True)


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
