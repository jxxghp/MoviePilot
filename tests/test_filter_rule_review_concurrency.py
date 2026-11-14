"""
通过真实配置仓储和事务作用域验证自定义过滤规则并发保存。
"""

import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.endpoints import rule as rule_endpoint
from app.application import filtering, rules
from app.application.configuration import SystemConfigService
from app.application.rules import AsyncRuleGroupMutationService, RuleGroupMutationConflictError
from app.db.adapters.configuration import SessionSystemConfigurationRepository
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.db.models.subscribe import Subscribe
from app.db.models.systemconfig import SystemConfig
from app.db.oper.systemconfig import SystemConfigOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork
from app.schemas.rule import CustomFilterRuleCreateRequest, CustomFilterRuleUpdateRequest
from app.schemas.types import SystemConfigKey
from app.startup.composition import subscription as composition


@pytest.mark.parametrize("operation", ["add", "update", "delete"])
def test_custom_rule_mutations_reject_stale_snapshots(db, monkeypatch, operation) -> None:
    """
    两个请求都读取旧快照后，冲突请求明确失败，不覆盖另一已确认成功的规则。
    """
    db.watermark(SystemConfig, Subscribe)
    original = [{"id": "A", "name": "Rule A"}, {"id": "B", "name": "Rule B"}]
    config = SystemConfigOper()
    db.add(SystemConfig(key=SystemConfigKey.CustomFilterRules.value, value=original),
           SystemConfig(key=SystemConfigKey.UserFilterRuleGroups.value, value=[]))
    config.load_snapshot(db.session)
    system_service = SystemConfigService(config, async_executor=SimpleNamespace(run=asyncio.to_thread))
    monkeypatch.setattr(filtering, "get_configured_system_config", lambda: system_service)
    monkeypatch.setattr(rules, "get_configured_system_config", lambda: system_service)
    scope = partial(composition.async_rule_group_mutation_scope, config.publish_many)
    service = filtering.FilterRuleService(None, scope, AsyncMock())

    async def execute():
        """
        在首次持久化边界暂停，确保两个异步用例都基于相同旧配置构造意图。
        """
        arrived = 0
        ready = asyncio.Event()
        original_scope = service._mutation_scope
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def synchronized_scope():
            """
            让两个修改同时到达持久化边界，验证旧快照只能提交一次。
            """
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            async with original_scope() as mutation:
                yield mutation

        service._mutation_scope = synchronized_scope
        if operation == "add":
            tasks = [service.add_custom(rule_id=key, name=f"Rule {key}", include=None, exclude=None,
                                       size_range=None, seeders=None, publish_time=None) for key in ("C", "D")]
        elif operation == "update":
            tasks = [service.update_custom(current_rule_id=key, name=f"Updated {key}") for key in ("A", "B")]
        else:
            tasks = [service.delete_custom(key) for key in ("A", "B")]
        return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)

    outcomes = asyncio.run(execute())
    conflicts = [value for value in outcomes if isinstance(value, RuleGroupMutationConflictError)]
    assert len(conflicts) == 1, outcomes
    assert sum(isinstance(value, dict) for value in outcomes) == 1, outcomes
    db.session.expire_all()
    stored = db.session.query(SystemConfig).filter_by(key=SystemConfigKey.CustomFilterRules.value).one().value
    if operation == "add":
        assert {item["id"] for item in stored} in ({"A", "B", "C"}, {"A", "B", "D"})
    elif operation == "update":
        assert sum(item["name"].startswith("Updated") for item in stored) == 1
    else:
        assert len(stored) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "update", "delete"])
async def test_custom_rule_api_returns_explicit_conflict(monkeypatch, operation) -> None:
    """
    过期快照作为可重读后重试的业务失败返回，不能退化为未处理的服务器异常。
    """
    service = SimpleNamespace(**{f"{operation}_custom": AsyncMock(
        side_effect=RuleGroupMutationConflictError("规则已更新，请重新读取"))})
    monkeypatch.setattr(rule_endpoint, "_service", lambda _runtime: service)
    if operation == "add":
        result = await rule_endpoint.add_custom_rule(CustomFilterRuleCreateRequest(rule_id="R", name="Rule"), runtime=None)
    elif operation == "update":
        result = await rule_endpoint.update_custom_rule("R", CustomFilterRuleUpdateRequest(name="Updated"), runtime=None)
    else:
        result = await rule_endpoint.delete_custom_rule("R", runtime=None)
    assert result.success is False
    assert result.message == "规则已更新，请重新读取"


@pytest.mark.asyncio
async def test_independent_sqlite_scopes_reject_rule_snapshot_conflict(tmp_path) -> None:
    """
    不共享进程锁的两个真实会话也不能同时提交基于旧规则集合的不同变更。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rules.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SystemConfig.__table__.create)
            await connection.run_sync(Subscribe.__table__.create)
        async with AsyncSession(engine) as session:
            session.add_all([SystemConfig(key=key.value, value=[]) for key in (
                SystemConfigKey.UserFilterRuleGroups, SystemConfigKey.CustomFilterRules)])
            await session.commit()
        ready = asyncio.Event()
        arrived = 0

        async def mutate(rule_id):
            """
            每个命令拥有独占会话和事务，只通过数据库竞争协调。
            """
            nonlocal arrived
            async with AsyncSession(engine) as session:
                service = AsyncRuleGroupMutationService(
                    SessionSystemConfigurationRepository(session), SessionSubscriptionRepository(session),
                    SqlAlchemyAsyncUnitOfWork(session), AsyncMock())
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()
                try:
                    return await service.apply([], expected_rule_groups=[],
                                               custom_rules=[{"id": rule_id, "name": rule_id}], expected_custom_rules=[])
                except RuleGroupMutationConflictError as error:
                    return error

        outcomes = await asyncio.wait_for(asyncio.gather(mutate("A"), mutate("B")), 10)
        assert sum(isinstance(value, RuleGroupMutationConflictError) for value in outcomes) == 1
        async with AsyncSession(engine) as session:
            row = (await session.execute(select(SystemConfig).where(
                SystemConfig.key == SystemConfigKey.CustomFilterRules.value))).scalar_one()
            assert len(row.value) == 1
    finally:
        await engine.dispose()
