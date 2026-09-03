"""分类策略 SystemConfig 适配器的真实数据库事务测试。"""

import copy
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.application.classification.configuration import (
    ClassificationPolicyConfigurationService,
    ClassificationPolicyValidationError,
    build_default_classification_policy,
)
from app.application.classification.contract import (
    ClassificationPolicyConflictError,
    ClassificationPolicyStateCorruptError,
)
from app.application.classification.reference import (
    validate_directory_classification_references,
)
from app.application.directory import normalize_directory_configurations_for_policy
from app.db.adapters.classification import (
    SystemConfigClassificationPolicyStore,
    SystemConfigDirectoryConfigurationStore,
)
from app.db.models.systemconfig import SystemConfig
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.schemas.category import ClassificationCategory, ClassificationPolicyState
from app.schemas.types import SystemConfigKey

NOW = datetime(2026, 9, 2, 18, 30, tzinfo=timezone.utc)
_ISOLATED_KEYS = (
    SystemConfigKey.MediaClassificationPolicy.value,
    SystemConfigKey.Directories.value,
)


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _isolate_system_config(db: Any) -> Iterator[None]:
    """隔离策略和目录配置，并在用例结束后恢复共享测试库原值。"""
    db.watermark(SystemConfig)
    snapshots = [
        (row.id, row.key, copy.deepcopy(row.value))
        for row in db.session.execute(select(SystemConfig).where(SystemConfig.key.in_(_ISOLATED_KEYS))).scalars()
    ]
    db.session.execute(delete(SystemConfig).where(SystemConfig.key.in_(_ISOLATED_KEYS)))
    db.session.commit()
    SystemConfigOper().load_snapshot(db.session)
    try:
        yield
    finally:
        db.session.rollback()
        db.session.execute(delete(SystemConfig).where(SystemConfig.key.in_(_ISOLATED_KEYS)))
        for row_id, key, value in snapshots:
            db.session.add(SystemConfig(id=row_id, key=key, value=value))
        db.session.commit()
        SystemConfigOper().load_snapshot(db.session)


def _store(db: Any) -> tuple[SystemConfigClassificationPolicyStore, SystemConfigOper]:
    """构造已加载全局快照和真实短会话分类策略仓储。"""
    oper = SystemConfigOper()
    oper.load_snapshot(db.session)
    return (
        SystemConfigClassificationPolicyStore(
            SessionFactory,
            oper.publish_many,
            reference_validator=validate_directory_classification_references,
        ),
        oper,
    )


def test_adapter_initializes_and_round_trips_json_datetime(db: Any) -> None:
    """首次创建原子写入 JSON，并在提交后发布系统配置快照。"""
    store, oper = _store(db)
    service = ClassificationPolicyConfigurationService(store, clock=lambda: NOW)

    active = service.initialize()
    loaded = store.load()
    cached = oper.get(SystemConfigKey.MediaClassificationPolicy)

    assert active.revision == 1
    assert loaded is not None
    assert loaded.active.updated_at == NOW
    assert cached["active"]["updated_at"] == "2026-09-02T18:30:00Z"


def test_adapter_rejects_stale_database_revision_and_service_reloads(
    db: Any,
) -> None:
    """数据库事实源先更新时，陈旧服务冲突并刷新活动快照。"""
    store, _oper = _store(db)
    service = ClassificationPolicyConfigurationService(store, clock=lambda: NOW)
    revision_one = service.initialize()
    external = build_default_classification_policy()
    external.categories[0].name = "外部提交"
    external.categories[0].path = ["外部提交"]
    external_state = ClassificationPolicyState(
        active=external.model_copy(
            deep=True,
            update={"revision": 2, "updated_at": NOW},
        ),
        history=[revision_one],
    )
    store.compare_and_set(expected_revision=1, state=external_state)

    with pytest.raises(ClassificationPolicyConflictError) as error:
        service.publish(build_default_classification_policy(), expected_revision=1)

    assert error.value.current_revision == 2
    assert service.active().revision == 2
    assert service.active().categories[0].name == "外部提交"


def test_adapter_detects_corrupt_persisted_state(db: Any) -> None:
    """损坏配置必须显式报错，不能静默覆盖为默认策略。"""
    store, _oper = _store(db)
    db.add(
        SystemConfig(
            key=SystemConfigKey.MediaClassificationPolicy.value,
            value={"active": {"revision": 0}, "history": []},
        )
    )

    with pytest.raises(ClassificationPolicyStateCorruptError):
        store.load()


def test_adapter_revalidates_directory_references_inside_cas_transaction(
    db: Any,
) -> None:
    """即使应用层快照陈旧，CAS 事务也必须阻止破坏最新目录引用。"""
    store, _oper = _store(db)
    service = ClassificationPolicyConfigurationService(store, clock=lambda: NOW)
    service.initialize()
    with_category = service.active()
    with_category.categories.append(
        ClassificationCategory(
            id="tv.anime.jp",
            media_type="电视剧",
            name="日番",
            path=["动漫", "日番"],
        )
    )
    service.publish(with_category, expected_revision=1)
    db.add(
        SystemConfig(
            key=SystemConfigKey.Directories.value,
            value=[
                {
                    "name": "动漫目录",
                    "media_type": "电视剧",
                    "media_category_id": "tv.anime.jp",
                    "media_category": "动漫/日番",
                }
            ],
        )
    )
    candidate = service.active()
    candidate.categories = [item for item in candidate.categories if item.id != "tv.anime.jp"]

    with pytest.raises(ClassificationPolicyValidationError) as error:
        service.publish(candidate, expected_revision=2)

    assert "referenced_category_missing" in {issue.code for issue in error.value.result.issues}
    assert service.active().revision == 2
    assert store.load().active.revision == 2


def test_directory_write_wins_shared_lock_and_blocks_category_removal(
    db: Any,
) -> None:
    """目录事务先取得共锁时，随后发布不得删除其刚绑定的分类。"""
    store, oper = _store(db)
    service = ClassificationPolicyConfigurationService(store, clock=lambda: NOW)
    service.initialize()
    with_category = service.active()
    with_category.categories.append(
        ClassificationCategory(
            id="tv.anime.jp",
            media_type="电视剧",
            name="日番",
            path=["动漫", "日番"],
        )
    )
    service.publish(with_category, expected_revision=1)

    normalizer_entered = threading.Event()
    release_normalizer = threading.Event()
    directory_errors: list[BaseException] = []
    policy_errors: list[BaseException] = []

    def blocking_normalizer(value: object, policy):
        """在目录事务持有共锁时暂停，制造策略发布交错。"""
        normalizer_entered.set()
        if not release_normalizer.wait(2):
            raise TimeoutError("目录规范化等待发布测试超时")
        return normalize_directory_configurations_for_policy(value, policy)

    directory_store = SystemConfigDirectoryConfigurationStore(
        SessionFactory,
        oper.publish_many,
        blocking_normalizer,
    )
    directory_value = [
        {
            "name": "动漫目录",
            "media_type": "电视剧",
            "media_category_id": "tv.anime.jp",
            "media_category": "旧动漫/日番",
        }
    ]
    candidate = service.active()
    candidate.categories = [category for category in candidate.categories if category.id != "tv.anime.jp"]

    def save_directory() -> None:
        """在线程私有会话中保存目录并记录异常。"""
        try:
            directory_store.save(directory_value)
        except BaseException as error:  # noqa: BLE001 线程异常需要回传主测试断言
            directory_errors.append(error)

    def remove_category() -> None:
        """并发发布删除分类的候选策略并记录异常。"""
        try:
            service.publish(candidate, expected_revision=2)
        except BaseException as error:  # noqa: BLE001 线程异常需要回传主测试断言
            policy_errors.append(error)

    directory_thread = threading.Thread(target=save_directory, daemon=True)
    policy_thread = threading.Thread(target=remove_category, daemon=True)
    directory_thread.start()
    assert normalizer_entered.wait(1)
    policy_thread.start()
    release_normalizer.set()
    directory_thread.join(timeout=2)
    policy_thread.join(timeout=2)

    assert not directory_thread.is_alive()
    assert not policy_thread.is_alive()
    assert directory_errors == []
    assert len(policy_errors) == 1
    assert isinstance(policy_errors[0], ClassificationPolicyValidationError)
    assert oper.get(SystemConfigKey.Directories)[0]["media_category"] == "动漫/日番"
    assert store.load().active.revision == 2


def test_policy_write_wins_shared_lock_and_blocks_stale_directory_binding(
    db: Any,
) -> None:
    """策略事务先取得共锁时，随后目录写入不得绑定已删除分类。"""
    store, oper = _store(db)
    service = ClassificationPolicyConfigurationService(store, clock=lambda: NOW)
    service.initialize()
    with_category = service.active()
    with_category.categories.append(
        ClassificationCategory(
            id="tv.anime.jp",
            media_type="电视剧",
            name="日番",
            path=["动漫", "日番"],
        )
    )
    service.publish(with_category, expected_revision=1)

    validator_entered = threading.Event()
    release_validator = threading.Event()
    policy_errors: list[BaseException] = []
    directory_errors: list[BaseException] = []

    def blocking_validator(policy, directories):
        """在策略事务持有共锁时暂停，制造目录保存交错。"""
        validator_entered.set()
        if not release_validator.wait(2):
            raise TimeoutError("策略引用复验等待目录测试超时")
        return validate_directory_classification_references(policy, directories)

    blocking_store = SystemConfigClassificationPolicyStore(
        SessionFactory,
        oper.publish_many,
        reference_validator=blocking_validator,
    )
    blocking_service = ClassificationPolicyConfigurationService(
        blocking_store,
        clock=lambda: NOW,
    )
    blocking_service.reload()
    candidate = blocking_service.active()
    candidate.categories = [category for category in candidate.categories if category.id != "tv.anime.jp"]
    directory_store = SystemConfigDirectoryConfigurationStore(
        SessionFactory,
        oper.publish_many,
        normalize_directory_configurations_for_policy,
    )

    def remove_category() -> None:
        """在线程私有会话中发布删除分类的策略。"""
        try:
            blocking_service.publish(candidate, expected_revision=2)
        except BaseException as error:  # noqa: BLE001 线程异常需要回传主测试断言
            policy_errors.append(error)

    def save_directory() -> None:
        """并发保存仍绑定旧分类 ID 的目录并记录异常。"""
        try:
            directory_store.save(
                [
                    {
                        "name": "动漫目录",
                        "media_type": "电视剧",
                        "media_category_id": "tv.anime.jp",
                        "media_category": "动漫/日番",
                    }
                ]
            )
        except BaseException as error:  # noqa: BLE001 线程异常需要回传主测试断言
            directory_errors.append(error)

    policy_thread = threading.Thread(target=remove_category, daemon=True)
    directory_thread = threading.Thread(target=save_directory, daemon=True)
    policy_thread.start()
    assert validator_entered.wait(1)
    directory_thread.start()
    release_validator.set()
    policy_thread.join(timeout=2)
    directory_thread.join(timeout=2)

    assert not policy_thread.is_alive()
    assert not directory_thread.is_alive()
    assert policy_errors == []
    assert len(directory_errors) == 1
    assert isinstance(directory_errors[0], ValueError)
    assert oper.get(SystemConfigKey.Directories) is None
    active = blocking_store.load().active
    assert active.revision == 3
    assert all(category.id != "tv.anime.jp" for category in active.categories)
