"""
配置表、用户表与 PassKey 表的查询行为。

这几张表决定「谁能登录、看到什么配置」，查错一行的后果是越权或配置串用，而不是
一个能被日志发现的异常。同步方法都有一个已是 2.0 写法的异步孪生方法，这里对同一
批数据同时跑两条路径并要求结果一致——同步侧改写后若有偏差，这个断言会直接暴露。
"""
import asyncio

import pytest

from app.db.models.passkey import PassKey
from app.db.models.systemconfig import SystemConfig
from app.db.models.user import User
from app.db.models.userconfig import UserConfig
from app.db.oper.passkey import PassKeyOper
from app.db.oper.user import UserOper


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
    db.add(SystemConfig(key="mp-test-a", value={"n": 1}),
           SystemConfig(key="mp-test-b", value={"n": 2}))

    found = SystemConfig.get_by_key(db.session, "mp-test-a")
    assert found.value == {"n": 1}

    async_found = asyncio.run(SystemConfig.async_get_by_key(key="mp-test-a"))
    assert async_found.value == found.value


def test_systemconfig_get_by_key_returns_none_when_absent(db):
    """
    键不存在时返回 None——调用方据此决定是否落默认值。
    """
    assert SystemConfig.get_by_key(db.session, "mp-test-missing") is None


def test_systemconfig_delete_by_key_removes_only_that_key(db):
    """
    按键删除只能删掉那一个键，误删会静默丢失其他配置。
    """
    db.add(SystemConfig(key="mp-test-del", value={"n": 1}),
           SystemConfig(key="mp-test-keep", value={"n": 2}))

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
    db.add(UserConfig(username="alice", key="theme", value="dark"),
           UserConfig(username="bob", key="theme", value="light"))

    assert UserConfig.get_by_key(db.session, username="alice", key="theme").value == "dark"
    assert UserConfig.get_by_key(db.session, username="bob", key="theme").value == "light"
    assert UserConfig.get_by_key(db.session, username="carol", key="theme") is None


def test_userconfig_delete_by_key_removes_only_that_user(db):
    """
    删除某用户的配置不能波及同名键的其他用户。
    """
    db.add(UserConfig(username="alice", key="theme", value="dark"),
           UserConfig(username="bob", key="theme", value="light"))

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
    created = db.add(User(name="mp-test-user", email="u@example.com",
                          hashed_password="x", is_active=True))

    by_name = User.get_by_name(db.session, "mp-test-user")
    by_id = User.get_by_id(db.session, created.id)
    assert by_name.id == by_id.id == created.id

    assert asyncio.run(User.async_get_by_name(name="mp-test-user")).id == created.id
    assert asyncio.run(User.async_get_by_id(user_id=created.id)).id == created.id


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

    assert asyncio.run(oper.async_update_otp_by_name(
        name="mp-test-async-otp", otp=True, secret="S2")) is True
    assert asyncio.run(oper.async_update_otp_by_name(
        name="mp-test-nobody", otp=True, secret="S2")) is False

    assert asyncio.run(oper.async_delete_by_name(name="mp-test-async-otp")) is True
    assert User.get_by_name(db.session, "mp-test-async-otp") is None

    asyncio.run(oper.async_delete(async_id_user.id))
    assert User.get_by_id(db.session, async_id_user.id) is None


# --------------------------------------------------------------------------- #
# PassKey
# --------------------------------------------------------------------------- #

def _passkey(user_id: int, credential_id: str, is_active: bool = True) -> PassKey:
    """构造一条 PassKey 记录。"""
    return PassKey(user_id=user_id, credential_id=credential_id,
                   public_key="pk", sign_count=0, is_active=is_active)


def test_passkey_listing_excludes_inactive_credentials(db):
    """
    列出用户凭据时必须排除已停用的。

    停用的凭据仍能被列出意味着它还会出现在登录选项里，等于停用没生效。
    """
    db.add(_passkey(9001, "cred-active-1"),
           _passkey(9001, "cred-active-2"),
           _passkey(9001, "cred-inactive", is_active=False),
           _passkey(9002, "cred-other"))

    listed = PassKey.get_by_user_id(db.session, 9001)

    assert {p.credential_id for p in listed} == {"cred-active-1", "cred-active-2"}
    assert {p.credential_id for p in asyncio.run(PassKey.async_get_by_user_id(user_id=9001))} == \
        {"cred-active-1", "cred-active-2"}


def test_passkey_oper_queries_use_explicit_session(db, monkeypatch):
    """PassKeyOper 的宿主查询使用调用方 Session，不创建兼容事务。"""
    db.add(_passkey(9002, "cred-oper"), _passkey(9002, "cred-oper-inactive", is_active=False))
    monkeypatch.setattr(
        "app.db.oper.passkey.run_sync_transaction",
        lambda _query: pytest.fail("显式 Session 查询不应创建兼容事务"),
    )

    oper = PassKeyOper(db.session)

    assert [item.credential_id for item in oper.list_by_user_id(9002)] == ["cred-oper"]
    assert oper.get_by_credential_id("cred-oper").user_id == 9002
    assert oper.get_by_credential_id("cred-oper-inactive") is None


def test_passkey_lookup_by_credential_id_skips_inactive(db):
    """
    按凭据 ID 查找同样必须忽略停用记录，否则停用的密钥仍可完成认证。
    """
    db.add(_passkey(9003, "cred-live"), _passkey(9003, "cred-dead", is_active=False))

    assert PassKey.get_by_credential_id(db.session, "cred-live").user_id == 9003
    assert PassKey.get_by_credential_id(db.session, "cred-dead") is None
    assert asyncio.run(PassKey.async_get_by_credential_id(credential_id="cred-dead")) is None


def test_passkey_get_by_id_ignores_active_flag(db):
    """
    按主键取记录是管理用途，不应过滤停用状态——否则管理端看不到自己刚停用的凭据。
    """
    dead = db.add(_passkey(9004, "cred-admin", is_active=False))

    assert PassKey.get_by_id(db.session, dead.id).credential_id == "cred-admin"
    assert asyncio.run(PassKey.async_get_by_id(passkey_id=dead.id)).credential_id == "cred-admin"


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
