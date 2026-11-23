"""
验证浏览器订阅的账号隔离、可信登记、重启恢复及失效清理竞争。
"""

import importlib
from functools import partial
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.dependencies.auth import get_current_active_superuser, get_current_active_user
from app.api.endpoints import message as endpoint
from app.application.webpush import WebPushConflictError, WebPushService
from app.db.adapters.webpush import webpush_scope
from app.db.models.user import User
from app.db.models.webpush import WebPushSubscription as SubscriptionRow
from app.modules.webpush import WebPushModule
from app.runtime.webpush import webpush_registry
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel
from app.schemas.webpush import WebPushSubscription


@pytest.fixture
def subscriptions(tmp_path, monkeypatch):
    """
    创建启用外键的真实临时数据库和独立服务，不接触运行配置目录数据库。

    :param tmp_path: 用例独占目录
    :param monkeypatch: 自动恢复进程服务装配的夹具
    :yields: 服务、会话工厂、两位用户及不可变 owner
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'webpush.sqlite'}")

    @sa.event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        """
        启用 SQLite 外键，以真实验证用户删除时的级联清理。

        :param connection: 当前数据库连接
        :param _record: 未使用的连接池记录
        """
        connection.execute("PRAGMA foreign_keys=ON")

    User.__table__.create(engine)
    SubscriptionRow.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        alice = User(name="alice", is_active=True, account_type="local")
        bob = User(name="bob", is_active=True, account_type="local")
        session.add_all([alice, bob])
        session.flush()
        identities = (alice.id, alice.notification_identity, bob.id, bob.notification_identity)
    service = WebPushService(partial(webpush_scope, factory))
    monkeypatch.setattr("app.application.webpush._service", service)
    try:
        yield SimpleNamespace(service=service, factory=factory, identities=identities)
    finally:
        engine.dispose()


def _subscription(endpoint="one", *, key="browser-key", auth="browser-secret"):
    """
    构造不包含客户端 owner 的完整浏览器订阅。

    :param endpoint: 测试端点尾部
    :param key: 浏览器公钥
    :param auth: 浏览器持有的认证密钥
    :return: 严格请求合同
    """
    return WebPushSubscription(endpoint=f"https://push.example/{endpoint}", keys={"p256dh": key, "auth": auth})


def test_accounts_are_isolated_and_service_restart_preserves_subscriptions(subscriptions):
    """
    同账号多设备与 Bob 隔离，重建服务后依然读取数据库登记。

    :param subscriptions: 当前用例独立服务和账号
    """
    case = subscriptions
    alice, owner, bob, bob_owner = case.identities
    case.service.register(alice, _subscription())
    case.service.register(alice, _subscription("two"))
    case.service.register(bob, _subscription("bob"))
    restarted = WebPushService(partial(webpush_scope, case.factory))
    assert {item.endpoint for item in restarted.for_owners((owner,))} == {
        "https://push.example/one", "https://push.example/two",
    }
    assert len(restarted.for_owners((bob_owner,))) == 1
    assert len(restarted.management_targets()) == 3
    assert restarted.for_owners(()) == ()
    assert all(set(item.subscription()) == {"endpoint", "keys"} for item in restarted.management_targets())


def test_endpoint_reassignment_requires_full_existing_keys_and_stale_cleanup_is_safe(subscriptions):
    """
    换号须持有完整旧密钥；旧账号撤销和旧 410 失败不能删除新账号登记。

    :param subscriptions: 当前用例独立服务和账号
    """
    service = subscriptions.service
    alice, owner, bob, bob_owner = subscriptions.identities
    sub = _subscription()
    service.register(alice, sub)
    old = service.for_owners((owner,))[0]
    assert service.is_current(old)
    with pytest.raises(WebPushConflictError):
        service.register(bob, _subscription(auth="forged-secret"))
    assert len(service.for_owners((owner,))) == 1
    service.register(bob, sub)
    assert not service.is_current(old)
    service.unregister(alice, sub)
    assert not service.remove_expired(old)
    assert service.for_owners((owner,)) == ()
    current = service.for_owners((bob_owner,))[0]
    service.register(bob, sub)
    assert not service.remove_expired(current)
    latest = service.for_owners((bob_owner,))[0]
    assert service.remove_expired(latest)
    assert service.for_owners((bob_owner,)) == ()


@pytest.mark.parametrize("state", ["disabled", "channel", "deleted", "recreated"])
def test_unavailable_accounts_never_receive_or_register_devices(subscriptions, state):
    """
    停用、内部账号、删除和同主键重建均不能继承旧订阅。

    :param subscriptions: 当前用例独立服务和账号
    :param state: 本次要验证的账号状态变化
    """
    case = subscriptions
    alice, owner, _, _ = case.identities
    case.service.register(alice, _subscription())
    with case.factory.begin() as session:
        user = session.get(User, alice)
        if state == "disabled":
            user.is_active = False
        elif state == "channel":
            user.account_type = "channel"
        else:
            session.delete(user)
            session.flush()
            if state == "recreated":
                session.add(User(id=alice, name="alice", is_active=True, account_type="local"))
    assert case.service.for_owners((owner,)) == ()
    assert case.service.management_targets() == ()
    if state != "recreated":
        with pytest.raises(PermissionError):
            case.service.register(alice, _subscription("new"))


def test_account_rename_preserves_ownership_and_self_unsubscribe_is_idempotent(subscriptions):
    """
    改名不改变归属，本人主动取消及重复取消都不残留设备。

    :param subscriptions: 当前用例独立服务和账号
    """
    case = subscriptions
    alice, owner, _, _ = case.identities
    case.service.register(alice, _subscription())
    with case.factory.begin() as session:
        session.get(User, alice).name = "renamed"
    assert len(case.service.for_owners((owner,))) == 1
    case.service.unregister(alice, _subscription())
    case.service.unregister(alice, _subscription())
    assert case.service.for_owners((owner,)) == ()


def test_http_registration_uses_server_account_and_rejects_client_owner(subscriptions):
    """
    HTTP owner 参数被拒绝，真实归属只从当前本地账号读取，取消仅限本人。

    :param subscriptions: 真实临时数据库及可恢复的服务登记
    """
    case = subscriptions
    alice, owner, bob, bob_owner = case.identities
    current = SimpleNamespace(id=alice, notification_identity=bob_owner)
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/message")
    app.dependency_overrides[get_current_active_user] = lambda: current
    payload = {**_subscription().model_dump(by_alias=True), "expirationTime": None}
    with TestClient(app) as client:
        assert client.post("/message/webpush/subscribe", json={**payload, "owner": bob_owner}).status_code == 422
        assert case.service.management_targets() == ()
        assert client.post("/message/webpush/subscribe", json=payload).json()["success"]
        assert len(case.service.for_owners((owner,))) == 1
        assert case.service.for_owners((bob_owner,)) == ()
        current.id = bob
        assert client.post("/message/webpush/unsubscribe", json=payload).json()["success"]
        assert len(case.service.for_owners((owner,))) == 1
        response = client.post("/message/webpush/subscribe", json=_subscription(auth="forged").model_dump(by_alias=True))
        assert response.status_code == 409
        assert client.post("/message/webpush/subscribe", json=payload).json()["success"]
        assert case.service.for_owners((owner,)) == ()
        assert len(case.service.for_owners((bob_owner,))) == 1
        with case.factory.begin() as session:
            session.get(User, bob).is_active = False
        assert client.post("/message/webpush/subscribe", json=payload).status_code == 403


def test_management_delivery_ignores_ownerless_registry_and_stale_gone_failure(subscriptions, monkeypatch):
    """
    管理全体计划只包含持久化账号设备，410 清理不能删除发送途中重新登记的设备。

    :param subscriptions: 已注入端点的真实持久化服务
    :param monkeypatch: 恢复离线发送器替换
    """
    from app.adapters.network.webpush import WebPushDeliveryError

    case = subscriptions
    alice, owner, _, _ = case.identities
    case.service.register(alice, _subscription())
    attempts = []

    def expired_after_reregistration(**kwargs):
        """
        模拟旧网络请求返回前浏览器完成重新登记。

        :param kwargs: 已剥离 owner 的推送协议参数
        :raises WebPushDeliveryError: 模拟推送服务的 410 响应
        """
        attempts.append(kwargs["subscription"])
        case.service.register(alice, _subscription())
        raise WebPushDeliveryError("gone", response=SimpleNamespace(status_code=410))

    monkeypatch.setattr("app.adapters.network.webpush.send_webpush", expired_after_reregistration)
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/message")
    app.dependency_overrides[get_current_active_superuser] = lambda: SimpleNamespace(id=alice)
    previous = webpush_registry.list()
    webpush_registry.upsert({"endpoint": "https://push.example/legacy", "keys": {}})
    try:
        with TestClient(app) as client:
            assert client.post("/message/webpush/send", json={"title": "管理通知"}).status_code == 200
        assert [item["endpoint"] for item in attempts] == ["https://push.example/one"]
        assert all(set(item) == {"endpoint", "keys"} for item in attempts)
        assert len(case.service.for_owners((owner,))) == 1
    finally:
        webpush_registry.clear()
        for subscription in previous:
            webpush_registry.upsert(subscription)


def test_migration_is_repeatable_and_does_not_claim_legacy_records(tmp_path, monkeypatch):
    """
    新迁移重复升级不重复建表、不推断 owner，建出的真实表支持服务登记。

    :param tmp_path: 本次用例的独立数据库路径
    :param monkeypatch: 恢复 Alembic 操作上下文
    """
    migration = importlib.import_module("database.versions.62c91ad8b057_channel_resources")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    User.__table__.create(engine)
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration._create_browser_devices()
            migration._create_browser_devices()
            assert connection.scalar(sa.select(sa.func.count()).select_from(SubscriptionRow.__table__)) == 0
        factory = sessionmaker(engine)
        with factory.begin() as session:
            user = User(name="migrated", is_active=True, account_type="local")
            session.add(user)
            session.flush()
            user_id = user.id
        service = WebPushService(partial(webpush_scope, factory))
        service.register(user_id, _subscription())
        assert len(service.management_targets()) == 1
    finally:
        engine.dispose()


def test_private_message_never_delivers_to_ownerless_legacy_subscription(monkeypatch, subscriptions):
    """
    历史无归属订阅不能接收带明确私人目标的新通知。

    :param monkeypatch: 自动恢复发送器和模块配置的替换夹具
    :param subscriptions: 不包含任何新登记的独立持久化服务
    """
    previous = webpush_registry.list()
    webpush_registry.clear()
    webpush_registry.upsert({"endpoint": "https://push.example/legacy", "keys": {}})
    deliveries = []
    module = WebPushModule()
    config = NotificationConf(id="web", name="web", type="webpush", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "check_message", lambda *_: True)
    monkeypatch.setattr("app.modules.webpush.send_webpush", lambda **kwargs: deliveries.append(kwargs))
    message = Message(title="Alice 的私人通知", username="alice", targets={"webpush": "a" * 32})
    message.set_notification_route_scope("user")
    try:
        module.post_message(message)
        assert deliveries == []
    finally:
        webpush_registry.clear()
        for subscription in previous:
            webpush_registry.upsert(subscription)


@pytest.mark.parametrize("scope", ["user", "admin", "all", None])
def test_module_delivers_only_explicit_account_targets_with_real_scope_filter(subscriptions, monkeypatch, scope):
    """
    真实消息基类过滤与模块账号选择共同隔离个人、管理员和明确全体范围。

    :param subscriptions: 真实数据库服务和两个启用账号
    :param monkeypatch: 自动恢复模块配置与离线发送器
    :param scope: 通知链注入的投递范围；缺省不得广播
    """
    case = subscriptions
    alice, owner, bob, bob_owner = case.identities
    case.service.register(alice, _subscription("alice"))
    case.service.register(bob, _subscription("bob"))
    module = WebPushModule()
    config = NotificationConf(id="web", name="web", type="webpush", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {"web": config})
    monkeypatch.setattr(module, "get_config", lambda _name: config)
    deliveries = []
    monkeypatch.setattr("app.modules.webpush.send_webpush", lambda **kwargs: deliveries.append(kwargs))
    target = (owner, bob_owner) if scope == "all" else owner
    message = Message(title="通知", username="alice", targets={"webpush": target})
    message.set_notification_route_scope(scope)
    module.post_message(message)
    assert {item["subscription"]["endpoint"] for item in deliveries} == (
        {"https://push.example/alice", "https://push.example/bob"}
        if scope == "all" else {"https://push.example/alice"}
    )
    deliveries.clear()
    missing = Message(title="没有明确账号目标", username="alice")
    missing.set_notification_route_scope(scope)
    module.post_message(missing)
    assert deliveries == []


def test_module_rechecks_ordinary_delivery_after_endpoint_changes_owner(subscriptions, monkeypatch):
    """
    普通发送选择设备后若端点已经换号，原账号的正文不会提交给推送服务。
    """
    alice, owner, bob, bob_owner = subscriptions.identities
    service = subscriptions.service
    service.register(alice, _subscription())
    for_owners = service.for_owners

    def reassign_after_selection(owners):
        """
        返回旧选择快照前完成换号，模拟选中设备和实际提交之间的竞争。
        """
        selected = for_owners(owners)
        service.register(bob, _subscription())
        return selected

    monkeypatch.setattr(service, "for_owners", reassign_after_selection)
    module = WebPushModule()
    module._channel = NotificationChannel.WebPush
    config = NotificationConf(id="web", name="web", type="webpush", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    deliveries = []
    monkeypatch.setattr("app.modules.webpush.send_webpush", lambda **kwargs: deliveries.append(kwargs))
    message = Message(title="仅 Alice 可见", channel=NotificationChannel.WebPush, targets={"webpush": owner})
    message.set_notification_route_scope("user")
    receipts = message.collect_delivery_results()
    module.post_message(message)

    assert deliveries == []
    assert [(item.status, item.code) for item in receipts] == [("skipped", "subscription_changed")]
    assert not for_owners((owner,)) and len(for_owners((bob_owner,))) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_module_gone_cleanup_preserves_reassigned_endpoint(subscriptions, monkeypatch, status):
    """
    模块的失效清理也比较账号与版本，旧账号请求不能删除换绑后设备。

    :param subscriptions: 当前用例的真实持久化服务
    :param monkeypatch: 自动恢复模块配置和发送器
    :param status: 推送服务声明订阅失效的状态码
    """
    from app.adapters.network.webpush import WebPushDeliveryError

    case = subscriptions
    alice, owner, bob, bob_owner = case.identities
    case.service.register(alice, _subscription())

    def fail_after_reassignment(**_kwargs):
        """
        返回旧失效响应前把设备登记到当前另一个登录账号。

        :param _kwargs: 已被离线拦截的推送参数
        :raises WebPushDeliveryError: 模拟失效端点响应
        """
        case.service.register(bob, _subscription())
        raise WebPushDeliveryError("gone", response=SimpleNamespace(status_code=status))

    module = WebPushModule()
    config = NotificationConf(id="web", name="web", type="webpush", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "check_message", lambda *_: True)
    monkeypatch.setattr("app.modules.webpush.send_webpush", fail_after_reassignment)
    message = Message(title="私人通知", targets={"webpush": owner})
    message.set_notification_route_scope("user")
    module.post_message(message)
    assert case.service.for_owners((owner,)) == ()
    assert len(case.service.for_owners((bob_owner,))) == 1


def test_module_records_retry_after_without_removing_device(subscriptions, monkeypatch):
    """
    推送服务明确限流时保留设备，并给重试记录提供平台等待时间。
    """
    from app.adapters.network.webpush import WebPushDeliveryError

    alice, owner, _, _ = subscriptions.identities
    subscriptions.service.register(alice, _subscription())
    response = SimpleNamespace(status_code=429, headers={"Retry-After": "17"})

    def rate_limit(**_):
        """
        模拟浏览器推送服务的限流响应。
        """
        raise WebPushDeliveryError("rate limited", response)

    module = WebPushModule()
    config = NotificationConf(id="web", name="web", type="webpush", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {"web": config})
    monkeypatch.setattr(module, "check_message", lambda *_: True)
    monkeypatch.setattr("app.modules.webpush.send_webpush", rate_limit)
    message = Message(title="私人通知", targets={"webpush": owner}, channel=NotificationChannel.WebPush)
    message.set_notification_route_scope("user")
    results = message.collect_delivery_results()

    module.post_message(message)

    receipt, = results
    assert (receipt.status, receipt.retry_after, receipt.code) == ("retryable_failure", 17, "http_429")
    assert subscriptions.service.for_owners((owner,))


def test_startup_composes_and_resets_persistent_service(subscriptions, monkeypatch):
    """
    启动组合使用显式会话工厂，关停后访问拒绝并且下一启动恢复持久化登记。

    :param subscriptions: 测试独占数据库与服务装配
    :param monkeypatch: 自动恢复启动根会话工厂
    """
    from app.adapters.network import webpush as transport
    from app.application.webpush import get_webpush_service
    from app.startup.composition import webpush as composition

    case = subscriptions
    alice, owner, _, _ = case.identities
    monkeypatch.setattr(composition, "SessionFactory", case.factory)
    monkeypatch.setattr(transport, "_endpoint_validator", transport._endpoint_validator)
    composition.configure_webpush_services()
    get_webpush_service().register(alice, _subscription())
    composition.reset_webpush_services()
    with pytest.raises(RuntimeError, match="未初始化"):
        get_webpush_service()
    composition.configure_webpush_services()
    assert len(get_webpush_service().for_owners((owner,))) == 1
