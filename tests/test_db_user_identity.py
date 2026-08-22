"""
第三方身份绑定表的数据访问、登录解析与端点行为。

一个本项目用户要能同时绑定 GitHub 账号与多台媒体服务器各自的账号，同一第三方身份
又绝不能落到两个本项目用户名下——前者要求不对 (user_id, provider) 设唯一约束，
后者要求 (provider, external_id) 的唯一约束是数据库层真实生效的，而不是只在应用层
判断一次就当作已经拦住。
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.user import User
from app.db.models.user_identity import UserIdentity
from app.db.oper.user import UserOper
from app.db.oper.user_identity import UserIdentityAlreadyBoundError, UserIdentityOper


@pytest.fixture(autouse=True)
def _track(db):
    """把本文件涉及的表纳入用例级回收。"""
    db.watermark(User, UserIdentity)


def _identity(user_id: int, provider: str, external_id: str, display_name=None) -> UserIdentity:
    """构造一条身份绑定记录。"""
    return UserIdentity(
        user_id=user_id, provider=provider, external_id=external_id, display_name=display_name
    )


# --------------------------------------------------------------------------- #
# 数据库层唯一约束
# --------------------------------------------------------------------------- #

def test_unique_constraint_blocks_cross_user_duplicate_at_db_level(db):
    """
    同一 (provider, external_id) 绑定到第二个用户时，数据库唯一约束必须直接拦下，
    不依赖任何应用层判断——这里绕过 Oper 的预检查，直接对模型发起写入。
    """
    db.add(_identity(1001, "github", "ext-dup"))

    with pytest.raises(IntegrityError):
        db.session.add(_identity(1002, "github", "ext-dup"))
        db.session.commit()
    db.session.rollback()

    # 冲突未提交，原绑定与用户归属都不受影响
    assert UserIdentity.get_by_provider_external_id(db.session, "github", "ext-dup").user_id == 1001


def test_same_user_can_bind_multiple_providers(db):
    """同一用户可以绑定多个第三方身份，包括同一 provider 族下的不同实例。"""
    db.add(
        _identity(2001, "github", "ext-a"),
        _identity(2001, "plugin:embyA", "user"),
        _identity(2001, "plugin:embyB", "admin"),
    )

    rows = UserIdentity.get_by_user_id(db.session, 2001)
    assert {(row.provider, row.external_id) for row in rows} == {
        ("github", "ext-a"),
        ("plugin:embyA", "user"),
        ("plugin:embyB", "admin"),
    }


def test_get_by_provider_external_id_resolves_bound_user(db):
    """按 (provider, external_id) 能查到绑定的本项目用户，同步与异步结果一致。"""
    db.add(_identity(3001, "wechat", "wx-123"))

    found = UserIdentity.get_by_provider_external_id(db.session, "wechat", "wx-123")
    assert found.user_id == 3001

    async_found = asyncio.run(
        UserIdentity.async_get_by_provider_external_id(provider="wechat", external_id="wx-123")
    )
    assert async_found.user_id == 3001


def test_get_by_provider_external_id_returns_none_when_absent(db):
    """未绑定的身份查询返回 None，而不是抛异常。"""
    assert UserIdentity.get_by_provider_external_id(db.session, "github", "mp-test-missing") is None


# --------------------------------------------------------------------------- #
# Oper 层：友好错误、幂等绑定、按归属解绑
# --------------------------------------------------------------------------- #

def test_oper_bind_rejects_duplicate_with_friendly_error(db):
    """
    预检查命中冲突时，Oper.bind 抛出领域异常而不是把 IntegrityError 泄露给调用方。
    """
    oper = UserIdentityOper(db=db.session)
    oper.bind(4001, "github", "ext-dup-oper")

    with pytest.raises(UserIdentityAlreadyBoundError):
        oper.bind(4002, "github", "ext-dup-oper")


def test_oper_bind_translates_db_level_conflict_into_friendly_error(db, monkeypatch):
    """
    竞态场景下预检查漏过冲突（另一请求刚好在两次查询之间完成写入），数据库唯一约束
    兜底拦截时同样要给出领域异常。
    """
    oper = UserIdentityOper(db=db.session)
    oper.bind(4101, "github", "ext-race")
    monkeypatch.setattr(oper, "get_by_provider_external_id", lambda provider, external_id: None)

    with pytest.raises(UserIdentityAlreadyBoundError):
        oper.bind(4102, "github", "ext-race")
    db.session.rollback()


def test_oper_bind_is_idempotent_for_the_same_user(db):
    """同一用户重复绑定同一身份直接返回既有记录，不报错也不产生第二行。"""
    oper = UserIdentityOper(db=db.session)
    first = oper.bind(4003, "github", "ext-idempotent")
    second = oper.bind(4003, "github", "ext-idempotent")

    assert first.id == second.id
    assert len(UserIdentity.get_by_user_id(db.session, 4003)) == 1


def test_oper_unbind_scoped_to_owner(db):
    """解绑必须同时匹配绑定 ID 与所属用户，不能删掉别人的绑定。"""
    oper = UserIdentityOper(db=db.session)
    identity = oper.bind(5001, "github", "ext-unbind")

    assert oper.unbind(identity.id, user_id=9999) is False
    assert UserIdentity.get_by_id(db.session, identity.id) is not None

    assert oper.unbind(identity.id, user_id=5001) is True
    assert UserIdentity.get_by_id(db.session, identity.id) is None


def test_oper_async_variants_match_sync_behaviour(db):
    """异步查询、列举与解绑必须与同步路径给出一致的结果。"""
    oper = UserIdentityOper(db=db.session)
    identity = oper.bind(6001, "github", "ext-async")
    # bind() 不再替调用方隐式提交（Oper 不得借 Base 写包装吞掉调用方事务边界）；
    # 本用例接下来要换一条独立的异步连接读取这行数据，必须自己提交这个由本用例
    # 持有的同步会话，写入才会跨连接可见。
    db.session.commit()

    found = asyncio.run(oper.async_get_by_provider_external_id("github", "ext-async"))
    assert found.user_id == 6001
    assert [i.id for i in asyncio.run(oper.async_list_by_user_id(6001))] == [identity.id]
    assert asyncio.run(oper.async_unbind(identity.id, user_id=6001)) is True
    assert UserIdentity.get_by_id(db.session, identity.id) is None


# --------------------------------------------------------------------------- #
# 级联删除
# --------------------------------------------------------------------------- #

def test_deleting_user_async_path_cascades_identity_rows(db):
    """
    删除本项目用户后，其全部身份绑定必须一并消失——UserOper.async_delete 是唯一
    生产可达的用户删除路径（HTTP 端点 delete_user_by_id/by_name 都经它）。

    删除前先把主键读成普通 int：删除经另一条会话（async_session_scope）提交，
    再用发起方会话（db.session）刷新一个刚被外部删掉的 ORM 对象属性会触发
    ObjectDeletedError，这是访问方式的问题，不是级联本身的问题。
    """
    user = db.add(User(name="mp-test-identity-owner-async", hashed_password="x"))
    user_id = user.id
    identity = db.add(_identity(user_id, "github", "ext-cascade-async"))
    identity_id = identity.id

    asyncio.run(UserOper().async_delete(user_id))

    assert User.get_by_id(db.session, user_id) is None
    assert UserIdentity.get_by_id(db.session, identity_id) is None


def test_deleting_user_sync_path_also_cascades_identity_rows(db):
    """同步删除路径（User.delete_by_id）同样要级联清理身份绑定。"""
    user = db.add(User(name="mp-test-identity-owner-sync", hashed_password="x"))
    user_id = user.id
    identity = db.add(_identity(user_id, "github", "ext-cascade-sync"))
    identity_id = identity.id

    assert User().delete_by_id(db.session, user_id) is True

    assert UserIdentity.get_by_id(db.session, identity_id) is None


def test_deleting_user_does_not_remove_other_users_identities(db):
    """级联删除只能清理被删用户自己的绑定，不能波及其他用户。"""
    victim = db.add(User(name="mp-test-identity-victim", hashed_password="x"))
    victim_id = victim.id
    bystander = db.add(User(name="mp-test-identity-bystander", hashed_password="x"))
    bystander_id = bystander.id
    victim_identity = db.add(_identity(victim_id, "github", "ext-victim"))
    victim_identity_id = victim_identity.id
    bystander_identity = db.add(_identity(bystander_id, "github", "ext-bystander"))
    bystander_identity_id = bystander_identity.id

    asyncio.run(UserOper().async_delete(victim_id))

    assert UserIdentity.get_by_id(db.session, victim_identity_id) is None
    assert UserIdentity.get_by_id(db.session, bystander_identity_id) is not None


# --------------------------------------------------------------------------- #
# 登录解析：查绑定 → 命中即登录 / 未命中按自动建号策略处理
# --------------------------------------------------------------------------- #

def test_resolve_returns_bound_user_without_creating_anything(db):
    """
    已绑定身份直接解析出本项目用户，不触发任何建号逻辑。

    先把主键读成普通 int 再调用解析函数：解析函数内部用组合根登记的独立端口
    （不带显式会话）读写数据库，与 db.session 是不同的会话；若之后还用
    db.session 刷新本用例自己创建的 ORM 对象属性，会因跨会话触发不必要的
    刷新失败，与本用例要验证的行为无关。
    """
    from app.application.security import auth as auth_module

    user = db.add(User(name="mp-test-identity-resolve", hashed_password="x"))
    user_id = user.id
    UserIdentityOper(db=db.session).bind(user_id, "github", "ext-resolve")

    result = auth_module.resolve_or_create_user_id_for_identity("github", "ext-resolve")
    assert result == user_id


def test_resolve_returns_none_when_unbound_and_auto_create_disabled(monkeypatch):
    """默认策略下，未绑定的第三方身份首次登录不自动建号，返回 None。"""
    from app.application.security import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "AUTH_IDENTITY_AUTO_CREATE_USER", False)

    result = auth_module.resolve_or_create_user_id_for_identity("github", "mp-test-ext-unbound")
    assert result is None


def test_resolve_creates_and_binds_user_when_auto_create_enabled(db, monkeypatch):
    """开启自动建号后，未绑定身份首次登录会创建新用户并完成绑定。"""
    from app.application.security import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "AUTH_IDENTITY_AUTO_CREATE_USER", True)

    user_id = auth_module.resolve_or_create_user_id_for_identity(
        "github", "mp-test-ext-auto-create", display_name="Auto Created"
    )

    assert user_id is not None
    created = User.get_by_id(db.session, user_id)
    assert created is not None
    bound = UserIdentity.get_by_provider_external_id(db.session, "github", "mp-test-ext-auto-create")
    assert bound.user_id == user_id


def test_create_plugin_auth_ticket_for_identity_returns_none_when_unresolved(monkeypatch):
    """未绑定且未开启自动建号时，不签发登录票据。"""
    from app.application.security import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "AUTH_IDENTITY_AUTO_CREATE_USER", False)

    ticket = auth_module.create_plugin_auth_ticket_for_identity(
        "github", "mp-test-ext-no-ticket"
    )
    assert ticket is None


def test_create_plugin_auth_ticket_for_identity_issues_ticket_when_bound(db):
    """已绑定身份登录成功后签发的票据能兑换出对应的本项目用户。"""
    from app.application.security import auth as auth_module

    user = db.add(User(name="mp-test-identity-ticket", hashed_password="x"))
    user_id = user.id
    UserIdentityOper(db=db.session).bind(user_id, "github", "ext-ticket")

    ticket = auth_module.create_plugin_auth_ticket_for_identity("github", "ext-ticket")
    assert ticket

    ticket_data = auth_module.consume_plugin_auth_ticket(ticket)
    assert ticket_data["user_id"] == user_id


# --------------------------------------------------------------------------- #
# HTTP 端点：列出、解绑
# --------------------------------------------------------------------------- #

def test_list_user_identities_endpoint_scopes_to_current_user(db):
    """列表端点只能看到当前用户自己的绑定。"""
    from app.api.endpoints.user import list_user_identities
    from app.application.security.identity import UserIdentityService

    oper = UserIdentityOper(db=db.session)
    oper.bind(7001, "github", "ext-endpoint")
    oper.bind(7002, "github", "ext-other")

    response = list_user_identities(
        current_user=SimpleNamespace(id=7001),
        service=UserIdentityService(repository=oper),
    )

    assert response.success is True
    assert [item["external_id"] for item in response.data] == ["ext-endpoint"]


def test_unbind_user_identity_endpoint_rejects_other_users_binding(db):
    """解绑端点不能删掉不属于当前用户的绑定。"""
    from app.api.endpoints.user import unbind_user_identity
    from app.application.security.identity import UserIdentityService

    oper = UserIdentityOper(db=db.session)
    identity = oper.bind(7003, "github", "ext-owned")
    service = UserIdentityService(repository=oper)

    denied = unbind_user_identity(
        identity.id, current_user=SimpleNamespace(id=9999), service=service
    )
    assert denied.success is False
    assert UserIdentity.get_by_id(db.session, identity.id) is not None

    allowed = unbind_user_identity(
        identity.id, current_user=SimpleNamespace(id=7003), service=service
    )
    assert allowed.success is True
    assert UserIdentity.get_by_id(db.session, identity.id) is None
