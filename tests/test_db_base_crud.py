"""
ORM 基类通用增删改查的行为。

这几个方法被全部 22 个模型继承，是覆盖面最广的一段代码：get/list/delete/truncate
任何一个出偏差都会同时影响所有表。同步方法与其异步孪生方法必须给出相同结果，
否则同一张表经 API（异步）与经调度任务（同步）会看到不同的数据。
"""
import asyncio

import pytest

from app.db.models.systemconfig import SystemConfig
from app.db.models.userconfig import UserConfig
from app.db.uow import run_async_transaction


@pytest.fixture(autouse=True)
def _track(db):
    """把用于验证基类行为的两张表纳入用例级回收。"""
    db.watermark(SystemConfig, UserConfig)


def test_create_persists_and_get_reads_back(db):
    """
    创建后可按主键读回，同步与异步取到同一行。
    """
    row = SystemConfig(key="base-create", value={"n": 1})
    row.create(db.session)
    db.session.commit()

    assert row.id is not None
    assert SystemConfig.get(db.session, row.id).key == "base-create"
    assert db.run_async_session(
        lambda session: SystemConfig.async_get(session, rid=row.id)
    ).key == "base-create"


def test_get_returns_none_for_missing_id(db):
    """
    主键不存在时返回 None，而不是抛异常或返回任意一行。
    """
    assert SystemConfig.get(db.session, -1) is None
    assert db.run_async_session(
        lambda session: SystemConfig.async_get(session, rid=-1)
    ) is None


def test_async_create_flushes_and_assigns_primary_key(db):
    """
    异步创建必须在返回前拿到主键。

    异步路径的调用方常常紧接着用 id 建立关联，拿到 None 会让关联静默丢失。
    """
    created = asyncio.run(run_async_transaction(
        lambda session: SystemConfig(
            key="base-async-create", value={"n": 2}
        ).async_create(session)
    ))

    assert created.id is not None
    assert SystemConfig.get(db.session, created.id).value == {"n": 2}


def test_update_writes_payload_fields(db):
    """
    更新按字典逐字段赋值并落库，同步与异步行为一致。
    """
    row = SystemConfig(key="base-update", value={"n": 1})
    row.create(db.session)
    db.session.flush()

    row.update(db.session, {"value": {"n": 9}})
    assert SystemConfig.get(db.session, row.id).value == {"n": 9}
    db.session.commit()

    async def update_in_owned_transaction(session) -> None:
        """在同一异步事务中读取并更新目标行。"""
        async_row = await SystemConfig.async_get(session, row.id)
        assert async_row is not None
        await async_row.async_update(session, payload={"value": {"n": 10}})

    asyncio.run(run_async_transaction(update_in_owned_transaction))
    db.session.expire_all()
    assert SystemConfig.get(db.session, row.id).value == {"n": 10}


def test_delete_removes_only_the_given_row(db):
    """
    按主键删除只影响那一行——条件失效会退化成清表。
    """
    dropped = db.add(SystemConfig(key="base-del", value={"n": 1}))
    kept = db.add(SystemConfig(key="base-keep", value={"n": 2}))

    SystemConfig.delete(db.session, dropped.id)

    assert SystemConfig.get(db.session, dropped.id) is None
    assert SystemConfig.get(db.session, kept.id) is not None


def test_async_delete_removes_only_the_given_row(db):
    """
    异步删除同样只影响目标行。
    """
    dropped = db.add(SystemConfig(key="base-async-del", value={"n": 1}))
    kept = db.add(SystemConfig(key="base-async-keep", value={"n": 2}))

    asyncio.run(run_async_transaction(
        lambda session: SystemConfig.async_delete(session, rid=dropped.id)
    ))

    assert SystemConfig.get(db.session, dropped.id) is None
    assert SystemConfig.get(db.session, kept.id) is not None


def test_async_delete_tolerates_missing_row(db):
    """
    删除不存在的行不抛异常，保持调用方的幂等语义。
    """
    asyncio.run(run_async_transaction(
        lambda session: SystemConfig.async_delete(session, rid=-1)
    ))


def test_list_returns_every_row_of_that_model_only(db):
    """
    列举必须限定在本模型对应的表，不能跨表。
    """
    db.add(UserConfig(username="base-user", key="k", value="v"))

    listed = UserConfig.list(db.session)

    assert any(item.username == "base-user" for item in listed)
    assert all(isinstance(item, UserConfig) for item in listed)


def test_async_list_matches_sync_list(db):
    """
    同步与异步列举必须返回同一批主键。
    """
    db.add(UserConfig(username="base-list", key="k", value="v"))

    sync_ids = sorted(item.id for item in UserConfig.list(db.session))
    async_ids = sorted(item.id for item in db.run_async_session(UserConfig.async_list))

    assert sync_ids == async_ids


def test_truncate_empties_the_table(db):
    """
    清表后该模型不再有任何行，同步与异步实现须一致。
    """
    db.add(UserConfig(username="base-truncate", key="k", value="v"))

    UserConfig.truncate(db.session)
    assert UserConfig.list(db.session) == []

    db.add(UserConfig(username="base-truncate-async", key="k", value="v"))
    asyncio.run(run_async_transaction(UserConfig.async_truncate))
    assert UserConfig.list(db.session) == []


def test_to_dict_covers_every_mapped_column(db):
    """
    字典转换必须覆盖全部映射列——API 直接把它作为响应体返回，缺列即为接口缺字段。
    """
    row = db.add(SystemConfig(key="base-dict", value={"n": 1}))

    payload = row.to_dict()

    assert set(payload) == {column.name for column in SystemConfig.__table__.columns}
    assert payload["key"] == "base-dict"
