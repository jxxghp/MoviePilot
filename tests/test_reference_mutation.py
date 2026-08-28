"""SystemConfig 与 Subscription 跨表引用修改的真实事务测试。"""

import asyncio
from collections.abc import Mapping

import pytest

from app.application.rules import (
    AsyncRuleGroupMutationService,
    RuleGroupMutationConflictError,
    SyncRuleGroupMutationService,
)
from app.application.site.mutation import SyncSiteReferenceMutationService
from app.db.adapters.configuration import SessionSystemConfigurationRepository
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.db.models.subscribe import Subscribe
from app.db.models.systemconfig import SystemConfig
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.common import JsonData
from app.schemas.types import MediaType, SystemConfigKey
from app.startup.composition import subscription as subscription_composition

_INITIAL_RULE_GROUPS = [
    {"name": "keep", "rule_string": "4K"},
    {"name": "old", "rule_string": "1080P"},
]
_INITIAL_CUSTOM_RULES = [{"id": "OLD", "name": "旧规则", "include": "old"}]


def _seed_references(db) -> int:
    """写入一组规则、RSS 和订阅引用，并刷新进程配置快照。"""
    db.watermark(SystemConfig, Subscribe)
    rows = [
        SystemConfig(
            key=SystemConfigKey.UserFilterRuleGroups.value,
            value=_INITIAL_RULE_GROUPS,
        ),
        SystemConfig(
            key=SystemConfigKey.SearchFilterRuleGroups.value,
            value=["old", "keep", "dangling"],
        ),
        SystemConfig(
            key=SystemConfigKey.SubscribeFilterRuleGroups.value,
            value=["old"],
        ),
        SystemConfig(
            key=SystemConfigKey.BestVersionFilterRuleGroups.value,
            value=["keep"],
        ),
        SystemConfig(
            key=SystemConfigKey.DefaultMovieSubscribeConfig.value,
            value={"quality": "WEB-DL", "filter_groups": ["old", "keep"]},
        ),
        SystemConfig(
            key=SystemConfigKey.DefaultTvSubscribeConfig.value,
            value={"filter_groups": ["dangling"]},
        ),
        SystemConfig(
            key=SystemConfigKey.RssSites.value,
            value=[1, 2, 3],
        ),
    ]
    subscription = Subscribe(
        name="跨表引用订阅",
        type=MediaType.TV.value,
        media_source="themoviedb",
        media_id="reference-mutation",
        filter_groups=["old", "keep", "dangling"],
        sites=[1, 2],
    )
    db.add(*rows, subscription)
    SystemConfigOper().load_snapshot(db.session)
    return subscription.id


def _config_value(db, key: SystemConfigKey):
    """从真实数据库读取单项 SystemConfig 值。"""
    db.session.expire_all()
    return db.session.query(SystemConfig).filter_by(key=key.value).one().value


def test_rule_group_prune_commits_all_references_and_publishes_after_commit(db) -> None:
    """通用 definitions 写入口应原子清悬空引用且不创建空默认配置。"""
    subscribe_id = _seed_references(db)
    published: list[Mapping[SystemConfigKey, JsonData]] = []
    with SessionFactory() as session:
        service = SyncRuleGroupMutationService(
            configuration=SessionSystemConfigurationRepository(session),
            subscriptions=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=published.append,
        )
        result = service.apply(
            [{"name": "keep", "rule_string": "4K"}],
            expected_rule_groups=_INITIAL_RULE_GROUPS,
        )

    assert _config_value(db, SystemConfigKey.SearchFilterRuleGroups) == ["keep"]
    assert _config_value(db, SystemConfigKey.SubscribeFilterRuleGroups) == []
    assert _config_value(db, SystemConfigKey.DefaultMovieSubscribeConfig) == {
        "quality": "WEB-DL",
        "filter_groups": ["keep"],
    }
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).filter_groups == ["keep"]
    assert published and SystemConfigKey.UserFilterRuleGroups in published[0]
    assert result.subscriptions[0].subscribe_id == subscribe_id
    assert db.session.query(SystemConfig).filter_by(
        key=SystemConfigKey.DefaultMusicSubscribeConfig.value
    ).one_or_none() is None


@pytest.mark.parametrize("failure", ["configuration", "subscription"])
def test_rule_group_stage_failure_rolls_back_every_table(db, failure: str) -> None:
    """配置或订阅任一暂存失败时，定义、引用和订阅行必须完整回滚。"""
    subscribe_id = _seed_references(db)
    published = []
    with SessionFactory() as session:
        configuration = SessionSystemConfigurationRepository(session)
        subscriptions = SessionSubscriptionRepository(session)

        if failure == "configuration":
            original_stage_set = configuration.stage_set

            def fail_configuration(key, value) -> None:
                """先暂存部分配置，再模拟配置适配器失败。"""
                original_stage_set(key, value)
                if key == SystemConfigKey.SubscribeFilterRuleGroups:
                    raise RuntimeError("configuration stage failed")

            configuration.stage_set = fail_configuration  # type: ignore[method-assign]
        else:
            original_stage_update = subscriptions.stage_update

            def fail_subscription(subscribe_id, patch):
                """先暂存订阅更新，再模拟订阅适配器失败。"""
                original_stage_update(subscribe_id, patch)
                raise RuntimeError("subscription stage failed")

            subscriptions.stage_update = fail_subscription  # type: ignore[method-assign]

        service = SyncRuleGroupMutationService(
            configuration=configuration,
            subscriptions=subscriptions,
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=published.append,
        )
        with pytest.raises(RuntimeError, match=f"{failure} stage failed"):
            service.apply(
                [{"name": "keep", "rule_string": "4K"}],
                expected_rule_groups=_INITIAL_RULE_GROUPS,
            )

    assert _config_value(db, SystemConfigKey.UserFilterRuleGroups)[1]["name"] == "old"
    assert _config_value(db, SystemConfigKey.SearchFilterRuleGroups) == [
        "old",
        "keep",
        "dangling",
    ]
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).filter_groups == [
        "old",
        "keep",
        "dangling",
    ]
    assert published == []


def test_combined_custom_rule_stage_failure_rolls_back_both_definitions(db) -> None:
    """自定义规则与规则组组合写任一暂存失败时不得留下半更新。"""
    subscribe_id = _seed_references(db)
    db.add(
        SystemConfig(
            key=SystemConfigKey.CustomFilterRules.value,
            value=_INITIAL_CUSTOM_RULES,
        )
    )
    published = []
    with SessionFactory() as session:
        configuration = SessionSystemConfigurationRepository(session)
        original_stage_set = configuration.stage_set

        def fail_custom_rule_stage(key, value) -> None:
            """先暂存两份定义，再在第二份配置上模拟数据库失败。"""
            original_stage_set(key, value)
            if key == SystemConfigKey.CustomFilterRules:
                raise RuntimeError("custom rule stage failed")

        configuration.stage_set = fail_custom_rule_stage  # type: ignore[method-assign]
        service = SyncRuleGroupMutationService(
            configuration=configuration,
            subscriptions=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=published.append,
        )
        with pytest.raises(RuntimeError, match="custom rule stage failed"):
            service.apply(
                [
                    {"name": "keep", "rule_string": "4K"},
                    {"name": "old", "rule_string": "NEW"},
                ],
                expected_rule_groups=_INITIAL_RULE_GROUPS,
                custom_rules=[
                    {"id": "NEW", "name": "旧规则", "include": "old"}
                ],
                expected_custom_rules=_INITIAL_CUSTOM_RULES,
            )

    assert _config_value(db, SystemConfigKey.UserFilterRuleGroups) == _INITIAL_RULE_GROUPS
    assert _config_value(db, SystemConfigKey.CustomFilterRules) == _INITIAL_CUSTOM_RULES
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).filter_groups == [
        "old",
        "keep",
        "dangling",
    ]
    assert published == []


def test_site_reference_mutation_commits_and_rolls_back_atomically(db) -> None:
    """RssSites 与 Subscription.sites 使用同一 UoW，失败不留下半更新。"""
    subscribe_id = _seed_references(db)
    with SessionFactory() as session:
        configuration = SessionSystemConfigurationRepository(session)
        subscriptions = SessionSubscriptionRepository(session)
        original_stage_update = subscriptions.stage_update

        def fail_subscription(target_id, patch):
            """暂存订阅站点清理后注入失败。"""
            original_stage_update(target_id, patch)
            raise RuntimeError("site subscription stage failed")

        subscriptions.stage_update = fail_subscription  # type: ignore[method-assign]
        service = SyncSiteReferenceMutationService(
            configuration=configuration,
            subscriptions=subscriptions,
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=lambda _values: None,
        )
        with pytest.raises(RuntimeError, match="site subscription stage failed"):
            service.apply(1)

    assert _config_value(db, SystemConfigKey.RssSites) == [1, 2, 3]
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).sites == [1, 2]

    with SessionFactory() as session:
        service = SyncSiteReferenceMutationService(
            configuration=SessionSystemConfigurationRepository(session),
            subscriptions=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=SystemConfigOper().publish_many,
        )
        result = service.apply(1)

    assert _config_value(db, SystemConfigKey.RssSites) == [2, 3]
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).sites == [2]
    assert result.subscription_ids == (subscribe_id,)
    assert SystemConfigOper().get(SystemConfigKey.RssSites) == [2, 3]

    with SessionFactory() as session:
        service = SyncSiteReferenceMutationService(
            configuration=SessionSystemConfigurationRepository(session),
            subscriptions=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=SystemConfigOper().publish_many,
        )
        reset_result = service.apply("*")

    assert _config_value(db, SystemConfigKey.RssSites) == []
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).sites == []
    assert reset_result.rss_sites == ()
    assert reset_result.subscription_ids == (subscribe_id,)
    assert SystemConfigOper().get(SystemConfigKey.RssSites) == []


def test_async_rule_group_path_commits_real_database_transaction(db) -> None:
    """Agent 使用的异步服务与同步服务共享相同的原子引用语义。"""
    subscribe_id = _seed_references(db)

    async def execute(session) -> None:
        """在真实 AsyncSession 中执行一次改名。"""
        async def publish(values) -> None:
            """模拟提交后发布配置快照。"""
            SystemConfigOper().publish_many(values)

        service = AsyncRuleGroupMutationService(
            configuration=SessionSystemConfigurationRepository(session),
            subscriptions=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            publish=publish,
        )
        await service.apply(
            [
                {"name": "keep", "rule_string": "4K"},
                {"name": "new", "rule_string": "1080P"},
            ],
            expected_rule_groups=_INITIAL_RULE_GROUPS,
            previous_name="old",
            current_name="new",
        )

    db.run_async_session(execute)
    assert _config_value(db, SystemConfigKey.SearchFilterRuleGroups) == [
        "new",
        "keep",
    ]
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).filter_groups == ["new", "keep"]


def test_async_scope_serializes_writes_without_blocking_event_loop(db, monkeypatch) -> None:
    """并发规则修改应串行收口，等待共享锁时事件循环仍能推进。"""
    subscribe_id = _seed_references(db)
    original_commit = subscription_composition.SqlAlchemyAsyncUnitOfWork.commit

    async def delayed_commit(unit_of_work) -> None:
        """延长持锁事务，给并发等待和事件循环心跳留下观察窗口。"""
        await asyncio.sleep(0.04)
        await original_commit(unit_of_work)

    monkeypatch.setattr(
        subscription_composition.SqlAlchemyAsyncUnitOfWork,
        "commit",
        delayed_commit,
    )

    async def execute() -> int:
        """并发执行两个幂等 prune，并统计等待期间的 loop 心跳。"""
        running = True
        ticks = 0

        async def mutate(target_name: str) -> object:
            """通过生产异步 scope 提交基于同一旧快照的不同改名意图。"""
            async with subscription_composition.async_rule_group_mutation_scope(
                SystemConfigOper().publish_many
            ) as service:
                try:
                    return await service.apply(
                        [
                            {"name": "keep", "rule_string": "4K"},
                            {"name": target_name, "rule_string": "1080P"},
                        ],
                        expected_rule_groups=_INITIAL_RULE_GROUPS,
                        previous_name="old",
                        current_name=target_name,
                    )
                except RuleGroupMutationConflictError as error:
                    return error

        async def heartbeat() -> None:
            """证明线程锁等待没有阻塞事件循环线程。"""
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        results = await asyncio.gather(mutate("first"), mutate("second"))
        running = False
        await heartbeat_task
        assert sum(isinstance(result, RuleGroupMutationConflictError) for result in results) == 1
        return ticks

    ticks = asyncio.run(execute())
    assert ticks >= 5
    final_groups = _config_value(db, SystemConfigKey.UserFilterRuleGroups)
    final_name = final_groups[1]["name"]
    assert final_name in {"first", "second"}
    assert _config_value(db, SystemConfigKey.SearchFilterRuleGroups) == [
        final_name,
        "keep",
    ]
    db.session.expire_all()
    assert db.session.get(Subscribe, subscribe_id).filter_groups == [
        final_name,
        "keep",
    ]
