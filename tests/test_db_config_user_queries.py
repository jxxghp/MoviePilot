"""
配置表、用户表与 PassKey 表的查询行为。

这几张表决定「谁能登录、看到什么配置」，查错一行的后果是越权或配置串用，而不是
一个能被日志发现的异常。同步方法都有一个已是 2.0 写法的异步孪生方法，这里对同一
批数据同时跑两条路径并要求结果一致——同步侧改写后若有偏差，这个断言会直接暴露。
"""

import asyncio

import pytest

from app.db import base as db_base
from app.db.models.passkey import PassKey
from app.db.models.systemconfig import SystemConfig
from app.db.models.user import User
from app.db.models.userconfig import UserConfig
from app.db.oper.passkey import PassKeyOper
from app.db.oper.user import UserOper
from app.db.session import async_session_scope


@pytest.fixture(autouse=True)
def _track(db):
    """把本文件涉及的表纳入用例级回收。"""
    db.watermark(SystemConfig, UserConfig, User, PassKey)


# --------------------------------------------------------------------------- #
# SystemConfig
# --------------------------------------------------------------------------- #


def test_systemconfig_get_by_key_matches_async_twin(db):
    """
    按键取配置的同步与异步结果必须一致，且只命中同名键。
    """
    db.add(SystemConfig(key="mp-test-a", value={"n": 1}), SystemConfig(key="mp-test-b", value={"n": 2}))

    found = SystemConfig.get_by_key(db.session, "mp-test-a")
    assert found.value == {"n": 1}

    async_found = db.run_async_session(lambda session: SystemConfig.async_get_by_key(session, "mp-test-a"))
    assert async_found.value == found.value


def test_systemconfig_get_by_key_returns_none_when_absent(db):
    """
    键不存在时返回 None——调用方据此决定是否落默认值。
    """
    assert SystemConfig.get_by_key(db.session, "mp-test-missing") is None


def test_systemconfig_queries_reuse_explicit_sessions(db, monkeypatch):
    """SystemConfig 显式同步与异步会话不得触发兼容会话。"""
    db.add(SystemConfig(key="mp-explicit-config", value=True))
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(AssertionError("不应创建额外同步事务")),
    )
    assert SystemConfig.get_by_key(db.session, "mp-explicit-config") is not None

    async def check() -> None:
        """验证异步配置查询复用显式 AsyncSession。"""
        async with async_session_scope() as session:
            monkeypatch.setattr(
                db_base,
                "run_async_transaction",
                lambda _operation: (_ for _ in ()).throw(AssertionError("不应创建额外异步事务")),
            )
            assert (
                await SystemConfig.async_get_by_key(
                    session,
                    "mp-explicit-config",
                )
                is not None
            )

    asyncio.run(check())


def test_systemconfig_delete_by_key_removes_only_that_key(db):
    """
    按键删除只能删掉那一个键，误删会静默丢失其他配置。
    """
    db.add(SystemConfig(key="mp-test-del", value={"n": 1}), SystemConfig(key="mp-test-keep", value={"n": 2}))

    assert SystemConfig().delete_by_key(db.session, "mp-test-del") is True

    assert SystemConfig.get_by_key(db.session, "mp-test-del") is None
    assert SystemConfig.get_by_key(db.session, "mp-test-keep").value == {"n": 2}


def test_systemconfig_delete_by_key_tolerates_missing_key(db):
    """
    删除不存在的键返回 True 而不抛异常，保持调用方的幂等语义。
    """
    assert SystemConfig().delete_by_key(db.session, "mp-test-missing") is True


# --------------------------------------------------------------------------- #
# UserConfig
# --------------------------------------------------------------------------- #


def test_userconfig_get_by_key_scopes_by_username(db):
    """
    用户配置必须同时按用户名和键命中——只按键会把别人的配置读给当前用户。
    """
    db.add(User(name="alice"), User(name="bob"))
    db.add(
        UserConfig(username="alice", key="theme", value="dark"), UserConfig(username="bob", key="theme", value="light")
    )

    assert UserConfig.get_by_key(db.session, username="alice", key="theme").value == "dark"
    assert UserConfig.get_by_key(db.session, username="bob", key="theme").value == "light"
    assert UserConfig.get_by_key(db.session, username="carol", key="theme") is None


def test_userconfig_delete_by_key_removes_only_that_user(db):
    """
    删除某用户的配置不能波及同名键的其他用户。
    """
    db.add(User(name="alice"), User(name="bob"))
    db.add(
        UserConfig(username="alice", key="theme", value="dark"), UserConfig(username="bob", key="theme", value="light")
    )

    assert UserConfig().delete_by_key(db.session, username="alice", key="theme") is True

    assert UserConfig.get_by_key(db.session, username="alice", key="theme") is None
    assert UserConfig.get_by_key(db.session, username="bob", key="theme").value == "light"


def test_userconfig_delete_by_key_tolerates_missing_row(db):
    """
    删除不存在的用户配置返回 True，不抛异常。
    """
    assert UserConfig().delete_by_key(db.session, username="nobody", key="theme") is True


# --------------------------------------------------------------------------- #
# User
# --------------------------------------------------------------------------- #


def test_user_lookup_by_name_and_id_matches_async_twin(db):
    """
    按名与按 ID 取用户的同步、异步结果必须指向同一行。

    登录链路走同步、API 依赖注入走异步，两者不一致会表现为「能登录但查不到自己」。
    """
    created = db.add(User(name="mp-test-user", email="u@example.com", hashed_password="x", is_active=True))

    by_name = User.get_by_name(db.session, "mp-test-user")
    by_id = User.get_by_id(db.session, created.id)
    assert by_name.id == by_id.id == created.id

    assert db.run_async_session(lambda session: User.async_get_by_name(session, "mp-test-user")).id == created.id
    assert db.run_async_session(lambda session: User.async_get_by_id(session, created.id)).id == created.id


def test_user_lookup_returns_none_when_absent(db):
    """
    查无此人时返回 None，而不是抛异常或返回任意一行。
    """
    assert User.get_by_name(db.session, "mp-test-nobody") is None
    assert User.get_by_id(db.session, -1) is None


def test_user_delete_by_name_and_by_id_remove_only_the_target(db):
    """
    按名、按 ID 删除都只能删掉目标用户。
    """
    keep = db.add(User(name="mp-test-keep", hashed_password="x"))
    drop_by_name = db.add(User(name="mp-test-drop-name", hashed_password="x"))
    drop_by_id = db.add(User(name="mp-test-drop-id", hashed_password="x"))

    assert User().delete_by_name(db.session, drop_by_name.name) is True
    assert User().delete_by_id(db.session, drop_by_id.id) is True

    assert User.get_by_name(db.session, "mp-test-drop-name") is None
    assert User.get_by_id(db.session, drop_by_id.id) is None
    assert User.get_by_id(db.session, keep.id) is not None


def test_user_update_otp_reports_whether_user_existed(db):
    """
    更新 OTP 必须如实反馈用户是否存在。

    对不存在的用户返回 True 会让上层以为二次验证已开启，实际并没有。
    """
    db.add(User(name="mp-test-otp", hashed_password="x", is_otp=False))

    assert User().update_otp_by_name(db.session, "mp-test-otp", True, "SECRET") is True
    assert User().update_otp_by_name(db.session, "mp-test-nobody", True, "SECRET") is False

    updated = User.get_by_name(db.session, "mp-test-otp")
    assert (updated.is_otp, updated.otp_secret) == (True, "SECRET")


def test_user_async_mutations_match_sync_behaviour(db):
    """
    异步的删除与 OTP 更新必须与同步路径给出相同的存在性判断。
    """
    async_id_user = db.add(User(name="mp-test-async-id", hashed_password="x"))
    db.add(User(name="mp-test-async-otp", hashed_password="x", is_otp=False))
    oper = UserOper()

    assert asyncio.run(oper.async_update_otp_by_name(name="mp-test-async-otp", otp=True, secret="S2")) is True
    assert asyncio.run(oper.async_update_otp_by_name(name="mp-test-nobody", otp=True, secret="S2")) is False

    assert asyncio.run(oper.async_delete_by_name(name="mp-test-async-otp")) is True
    assert User.get_by_name(db.session, "mp-test-async-otp") is None

    asyncio.run(oper.async_delete(async_id_user.id))
    assert User.get_by_id(db.session, async_id_user.id) is None


def test_legacy_user_oper_delete_cascades_user_children(db):
    """旧 UserOper 删除入口保持可用，并由数据库清除配置和 PassKey。"""
    user = db.add(User(name="legacy-delete", is_active=True))
    user_id = user.id
    username = user.name
    db.add(
        UserConfig(username=username, key="theme", value="dark"),
        _passkey(user_id, "legacy-delete-credential"),
    )

    assert asyncio.run(UserOper().async_delete_by_name(username)) is True

    db.session.expire_all()
    assert User.get_by_id(db.session, user_id) is None
    assert UserConfig.get_by_key(db.session, username, "theme") is None
    assert PassKey.get_by_credential_id(db.session, "legacy-delete-credential") is None


# --------------------------------------------------------------------------- #
# PassKey
# --------------------------------------------------------------------------- #


def _passkey(user_id: int, credential_id: str, is_active: bool = True) -> PassKey:
    """构造一条 PassKey 记录。"""
    return PassKey(user_id=user_id, credential_id=credential_id, public_key="pk", sign_count=0, is_active=is_active)


@pytest.fixture(autouse=True)
def _create_passkey_owners(request, db) -> None:
    """PassKey 查询用例必须使用受外键保护的真实用户主体。"""
    if not request.node.name.startswith("test_passkey_"):
        return
    db.add(
        *[
            User(
                id=user_id,
                name=f"passkey-owner-{user_id}",
                is_active=True,
                is_superuser=False,
            )
            for user_id in range(9001, 9012)
        ]
    )


def test_passkey_listing_excludes_inactive_credentials(db):
    """
    列出用户凭据时必须排除已停用的。

    停用的凭据仍能被列出意味着它还会出现在登录选项里，等于停用没生效。
    """
    db.add(
        _passkey(9001, "cred-active-1"),
        _passkey(9001, "cred-active-2"),
        _passkey(9001, "cred-inactive", is_active=False),
        _passkey(9002, "cred-other"),
    )

    listed = PassKey.get_by_user_id(db.session, 9001)

    assert {p.credential_id for p in listed} == {"cred-active-1", "cred-active-2"}
    assert {
        p.credential_id for p in db.run_async_session(lambda session: PassKey.async_get_by_user_id(session, 9001))
    } == {"cred-active-1", "cred-active-2"}


def test_passkey_oper_queries_use_explicit_session(db, monkeypatch):
    """PassKeyOper 的宿主查询使用调用方 Session，不创建兼容事务。"""
    db.add(_passkey(9002, "cred-oper"), _passkey(9002, "cred-oper-inactive", is_active=False))
    monkeypatch.setattr(
        "app.db.base.run_sync_transaction",
        lambda _query: pytest.fail("显式 Session 查询不应创建兼容事务"),
    )

    oper = PassKeyOper(db.session)

    assert {item.credential_id for item in oper.list()} == {
        "cred-oper",
        "cred-oper-inactive",
    }
    assert [item.credential_id for item in oper.list_by_user_id(9002)] == ["cred-oper"]
    assert oper.get_by_credential_id("cred-oper").user_id == 9002
    assert oper.get_by_credential_id("cred-oper-inactive") is None


def test_passkey_oper_paginates_and_counts_active_credentials(db):
    """PassKey 列表只统计有效凭据，并在数据库查询中稳定分页。"""
    first = db.add(_passkey(9002, "cred-page-1"))
    second = db.add(_passkey(9002, "cred-page-2"))
    db.add(_passkey(9002, "cred-page-off", is_active=False))
    oper = PassKeyOper(db.session)

    page = oper.list_by_user_id(9002, page=2, count=1)

    assert oper.count_by_user_id(9002) == 2
    assert [item.id for item in page] == [second.id]
    assert first.id < second.id


def test_passkey_lookup_by_credential_id_skips_inactive(db):
    """
    按凭据 ID 查找同样必须忽略停用记录，否则停用的密钥仍可完成认证。
    """
    db.add(_passkey(9003, "cred-live"), _passkey(9003, "cred-dead", is_active=False))

    assert PassKey.get_by_credential_id(db.session, "cred-live").user_id == 9003
    assert PassKey.get_by_credential_id(db.session, "cred-dead") is None
    assert db.run_async_session(lambda session: PassKey.async_get_by_credential_id(session, "cred-dead")) is None


def test_passkey_remaining_queries_reuse_explicit_sessions(db, monkeypatch):
    """PassKey 其余同步/异步查询必须复用调用方会话。"""
    key = db.add(_passkey(9008, "cred-explicit"))
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(AssertionError("不应创建额外同步事务")),
    )
    assert PassKey.get_by_id(db.session, key.id).credential_id == "cred-explicit"

    async def check() -> None:
        """验证三个异步查询都复用显式 AsyncSession。"""
        async with async_session_scope() as session:
            monkeypatch.setattr(
                db_base,
                "run_async_transaction",
                lambda _operation: (_ for _ in ()).throw(AssertionError("不应创建额外异步事务")),
            )
            assert [
                item.credential_id
                for item in await PassKey.async_get_by_user_id(
                    session,
                    9008,
                )
            ] == ["cred-explicit"]
            assert (
                await PassKey.async_get_by_credential_id(
                    session,
                    "cred-explicit",
                )
                is not None
            )
            assert await PassKey.async_get_by_id(session, key.id) is not None

    asyncio.run(check())


def test_passkey_get_by_id_ignores_active_flag(db):
    """
    按主键取记录是管理用途，不应过滤停用状态——否则管理端看不到自己刚停用的凭据。
    """
    dead = db.add(_passkey(9004, "cred-admin", is_active=False))

    assert PassKey.get_by_id(db.session, dead.id).credential_id == "cred-admin"
    assert db.run_async_session(lambda session: PassKey.async_get_by_id(session, dead.id)).credential_id == "cred-admin"


def test_passkey_delete_requires_matching_owner(db):
    """
    删除必须同时匹配主键与所属用户。

    只按主键删除即为越权：任意登录用户都能删掉别人的凭据。
    """
    victim = db.add(_passkey(9005, "cred-victim"))

    assert PassKey.delete_by_id(db.session, passkey_id=victim.id, user_id=9999) is False
    assert PassKey.get_by_id(db.session, victim.id) is not None

    assert PassKey.delete_by_id(db.session, passkey_id=victim.id, user_id=9005) is True
    assert PassKey.get_by_id(db.session, victim.id) is None


def test_passkey_async_delete_enforces_the_same_ownership_rule(db):
    """
    异步删除必须与同步路径使用同一套归属判定。
    """
    victim = db.add(_passkey(9006, "cred-async-victim"))

    oper = PassKeyOper()
    assert asyncio.run(oper.async_delete_by_id(passkey_id=victim.id, user_id=9999)) is False
    assert asyncio.run(oper.async_delete_by_id(passkey_id=victim.id, user_id=9006)) is True
    assert PassKey.get_by_id(db.session, victim.id) is None


def test_passkey_update_last_used_persists_sign_count(db):
    """
    签名计数必须落库——它是防重放的依据，不落库等于校验形同虚设。
    """
    key = db.add(_passkey(9007, "cred-count"))

    assert key.update_last_used(db.session, sign_count=42) is True

    assert PassKey.get_by_id(db.session, key.id).sign_count == 42


def test_passkey_oper_sign_count_compare_and_swap_has_single_winner(db):
    """两个基于同一旧计数的认证提交只能有一个更新成功。"""
    key = db.add(_passkey(9008, "cred-cas"))
    key.sign_count = 41
    db.session.flush()
    oper = PassKeyOper(db.session)

    assert (
        oper.compare_and_update_sign_count(
            passkey_id=key.id,
            expected_sign_count=41,
            sign_count=42,
        )
        is True
    )
    assert (
        oper.compare_and_update_sign_count(
            passkey_id=key.id,
            expected_sign_count=41,
            sign_count=43,
        )
        is False
    )

    db.session.expire_all()
    assert PassKey.get_by_id(db.session, key.id).sign_count == 42


def test_passkey_oper_sign_count_cas_rejects_inactive_or_regressed_key(db):
    """停用凭证及未递增的非零计数都不得被认证提交覆盖。"""
    inactive = db.add(_passkey(9009, "cred-cas-inactive", is_active=False))
    active = db.add(_passkey(9010, "cred-cas-regressed"))
    active.sign_count = 5
    db.session.flush()
    oper = PassKeyOper(db.session)

    assert (
        oper.compare_and_update_sign_count(
            passkey_id=inactive.id,
            expected_sign_count=0,
            sign_count=1,
        )
        is False
    )
    assert (
        oper.compare_and_update_sign_count(
            passkey_id=active.id,
            expected_sign_count=5,
            sign_count=4,
        )
        is False
    )
    assert (
        oper.compare_and_update_sign_count(
            passkey_id=active.id,
            expected_sign_count=5,
            sign_count=5,
        )
        is False
    )


def test_passkey_oper_sign_count_cas_allows_counterless_authenticator(db):
    """不支持签名计数器的认证器允许按 WebAuthn 约定保持零计数。"""
    key = db.add(_passkey(9011, "cred-cas-counterless"))
    oper = PassKeyOper(db.session)

    assert (
        oper.compare_and_update_sign_count(
            passkey_id=key.id,
            expected_sign_count=0,
            sign_count=0,
        )
        is True
    )
