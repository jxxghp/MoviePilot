"""分类策略版本服务的发布、历史、回滚和快照隔离测试。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar, cast

import pytest
from pydantic import ValidationError

from app.application.classification.configuration import (
    CLASSIFICATION_POLICY_HISTORY_LIMIT,
    ClassificationPolicyConfigurationService,
    ClassificationPolicyNotInitializedError,
    ClassificationPolicyRevisionNotFoundError,
    ClassificationPolicyValidationError,
    build_default_classification_policy,
)
from app.application.classification.contract import (
    ClassificationPolicyConflictError,
)
from app.application.classification.reference import (
    DirectoryClassificationReferenceValidator,
)
from app.schemas.category import (
    ClassificationCategory,
    ClassificationFieldDefinition,
    ClassificationPolicy,
    ClassificationPolicyState,
)

NOW = datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)
T = TypeVar("T")


class _MemoryPolicyStore:
    """以深拷贝模拟数据库事实源和原子 revision 比较。"""

    def __init__(self) -> None:
        """初始化空状态和成功写入计数。"""
        self.persisted: ClassificationPolicyState | None = None
        self.write_count = 0

    def load(self) -> ClassificationPolicyState | None:
        """返回与内部状态隔离的持久化快照。"""
        if self.persisted is None:
            return None
        return cast(
            ClassificationPolicyState,
            self.persisted.model_copy(deep=True),
        )

    def compare_and_set(
        self,
        *,
        expected_revision: int,
        state: ClassificationPolicyState,
    ) -> None:
        """按当前活动 revision 原子替换状态包。"""
        current_revision = self.persisted.active.revision if self.persisted else 0
        if current_revision != expected_revision:
            raise ClassificationPolicyConflictError(
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        self.persisted = cast(
            ClassificationPolicyState,
            state.model_copy(deep=True),
        )
        self.write_count += 1


class _InlineDatabaseExecutor:
    """在测试协程中直接执行同步短事务操作。"""

    async def run(self, operation: Callable[[], T]) -> T:
        """执行并返回同步操作结果。"""
        return operation()


def _service(
    store: _MemoryPolicyStore,
    *,
    asynchronous: bool = False,
    directories: list[dict[str, object]] | None = None,
) -> ClassificationPolicyConfigurationService:
    """构造使用固定时钟的分类策略服务。"""
    return ClassificationPolicyConfigurationService(
        store,
        clock=lambda: NOW,
        async_executor=_InlineDatabaseExecutor() if asynchronous else None,
        reference_validator=(
            DirectoryClassificationReferenceValidator(lambda: directories)
            if directories is not None
            else None
        ),
    )


def _custom_category() -> ClassificationCategory:
    """构造不参与 fallback、可被目录独立引用的电视剧分类。"""
    return ClassificationCategory(
        id="tv.anime.jp",
        media_type="电视剧",
        name="日番",
        path=["动漫", "日番"],
    )


def _renamed_draft(name: str) -> ClassificationPolicy:
    """构造只改变电影兜底分类显示信息的合法草稿。"""
    draft = build_default_classification_policy()
    draft.categories[0].name = name
    draft.categories[0].path = [name]
    return draft


def test_uninitialized_service_rejects_snapshot_reads() -> None:
    """组合根初始化完成前不得向消费者暴露半成品策略。"""
    service = _service(_MemoryPolicyStore())

    with pytest.raises(ClassificationPolicyNotInitializedError):
        service.active()


def test_policy_schema_rejects_unknown_version_and_negative_revision() -> None:
    """持久化边界不得把未知结构版本或负 revision 当成当前策略。"""
    with pytest.raises(ValidationError):
        ClassificationPolicy.model_validate({"schema_version": 3})
    with pytest.raises(ValidationError):
        ClassificationPolicy.model_validate({"revision": -1})


def test_initialize_publishes_valid_three_media_fallbacks() -> None:
    """首次初始化发布 revision 1，且三种媒体类型都有稳定兜底。"""
    store = _MemoryPolicyStore()
    service = _service(store)

    active = service.initialize()
    validation = service.validate(active)

    assert active.revision == 1
    assert active.updated_at == NOW
    assert set(active.fallbacks) == {"电影", "电视剧", "音乐"}
    assert validation.valid is True
    assert service.history() == ()
    assert store.write_count == 1


def test_initialize_loads_existing_state_without_republishing() -> None:
    """已存在状态时初始化只加载，不生成额外 revision。"""
    store = _MemoryPolicyStore()
    first = _service(store)
    expected = first.initialize()

    second = _service(store)
    loaded = second.initialize(_renamed_draft("不得发布"))

    assert loaded == expected
    assert store.write_count == 1


def test_dynamic_extension_fields_can_be_registered_after_startup() -> None:
    """兼容配置写入口可登记新 TMDB 字段，且返回快照不能篡改内部目录。"""
    service = _service(_MemoryPolicyStore())
    service.initialize()
    field = ClassificationFieldDefinition(
        id="extensions.themoviedb.status",
        label="TMDB status",
        value_type="string_list",
        operators=["contains_any", "contains_none", "exists", "not_exists"],
        media_types=["电影"],
        source_support={"themoviedb": "extension"},
    )

    registered = service.register_extra_fields([field])
    registered[0].label = "篡改"

    assert service.extra_fields()[0].id == "extensions.themoviedb.status"
    assert service.extra_fields()[0].label == "TMDB status"


def test_publish_overrides_client_revision_and_tracks_history() -> None:
    """发布只信任 expected revision，并把旧活动版本放入历史。"""
    store = _MemoryPolicyStore()
    service = _service(store)
    service.initialize()
    draft = _renamed_draft("电影新分类")
    draft.revision = 999
    draft.updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

    published = service.publish(draft, expected_revision=1)

    assert published.revision == 2
    assert published.updated_at == NOW
    assert published.categories[0].name == "电影新分类"
    assert [item.revision for item in service.history()] == [1]


def test_history_is_bounded_and_sorted_newest_first() -> None:
    """连续发布只保留最近十个历史 revision。"""
    store = _MemoryPolicyStore()
    service = _service(store)
    service.initialize()

    for expected_revision in range(1, CLASSIFICATION_POLICY_HISTORY_LIMIT + 2):
        service.publish(
            _renamed_draft(f"分类{expected_revision + 1}"),
            expected_revision=expected_revision,
        )

    assert service.active().revision == 12
    assert [policy.revision for policy in service.history()] == list(
        range(11, 1, -1)
    )


def test_stale_local_revision_is_rejected_without_store_write() -> None:
    """客户端 revision 早于本进程快照时立即冲突且不调用持久化写入。"""
    store = _MemoryPolicyStore()
    service = _service(store)
    service.initialize()

    with pytest.raises(ClassificationPolicyConflictError) as error:
        service.publish(_renamed_draft("冲突"), expected_revision=0)

    assert error.value.current_revision == 1
    assert service.active().revision == 1
    assert store.write_count == 1


def test_store_conflict_reloads_database_fact_snapshot() -> None:
    """其他实例先提交后，本服务冲突并刷新为数据库中的新活动版本。"""
    store = _MemoryPolicyStore()
    service = _service(store)
    revision_one = service.initialize()
    external_active = _renamed_draft("外部版本").model_copy(
        deep=True,
        update={"revision": 2, "updated_at": NOW},
    )
    store.persisted = ClassificationPolicyState(
        active=external_active,
        history=[revision_one],
    )

    with pytest.raises(ClassificationPolicyConflictError) as error:
        service.publish(_renamed_draft("本地草稿"), expected_revision=1)

    assert error.value.current_revision == 2
    assert service.active().revision == 2
    assert service.active().categories[0].name == "外部版本"


def test_invalid_policy_does_not_change_storage_or_memory() -> None:
    """结构化校验错误在打开写事务前阻止发布。"""
    store = _MemoryPolicyStore()
    service = _service(store)
    service.initialize()
    invalid = build_default_classification_policy()
    invalid.fallbacks.pop("音乐")

    with pytest.raises(ClassificationPolicyValidationError) as error:
        service.publish(invalid, expected_revision=1)

    assert error.value.result.valid is False
    assert service.active().revision == 1
    assert store.write_count == 1


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("delete", "referenced_category_missing"),
        ("disable", "referenced_category_disabled"),
        ("change_type", "referenced_category_media_type_mismatch"),
    ],
)
def test_referenced_directory_category_cannot_be_broken(
    mutation: str,
    expected_code: str,
) -> None:
    """发布必须阻止删除、禁用或改变仍被目录稳定引用的分类类型。"""
    directories: list[dict[str, object]] = []
    store = _MemoryPolicyStore()
    service = _service(store, directories=directories)
    service.initialize()
    with_category = service.active()
    with_category.categories.append(_custom_category())
    service.publish(with_category, expected_revision=1)
    directories.append(
        {
            "name": "动漫目录",
            "media_type": "tv",
            "media_category_id": "tv.anime.jp",
            "media_category": "动漫/日番",
        }
    )
    candidate = service.active()
    category = next(item for item in candidate.categories if item.id == "tv.anime.jp")
    if mutation == "delete":
        candidate.categories.remove(category)
    elif mutation == "disable":
        category.enabled = False
    else:
        category.media_type = "电影"

    validation = service.validate(candidate)
    with pytest.raises(ClassificationPolicyValidationError) as error:
        service.publish(candidate, expected_revision=2)

    assert validation.valid is False
    assert expected_code in {issue.code for issue in validation.issues}
    assert expected_code in {issue.code for issue in error.value.result.issues}
    assert service.active().revision == 2


def test_referenced_directory_category_path_can_be_renamed() -> None:
    """分类路径和名称变化不得破坏以稳定 ID 建立的目录引用。"""
    directories: list[dict[str, object]] = []
    service = _service(_MemoryPolicyStore(), directories=directories)
    service.initialize()
    draft = service.active()
    draft.categories.append(_custom_category())
    service.publish(draft, expected_revision=1)
    directories.append(
        {
            "name": "动漫目录",
            "media_type": "电视剧",
            "media_category_id": "tv.anime.jp",
            "media_category": "动漫/日番",
        }
    )
    renamed = service.active()
    category = next(item for item in renamed.categories if item.id == "tv.anime.jp")
    category.name = "日本动画"
    category.path = ["动画", "日本"]

    published = service.publish(renamed, expected_revision=2)

    assert published.revision == 3
    assert category.id in {item.id for item in published.categories}


def test_rollback_cannot_restore_policy_that_breaks_directory_reference() -> None:
    """回滚也必须执行与普通发布相同的目录稳定引用约束。"""
    directories: list[dict[str, object]] = []
    service = _service(_MemoryPolicyStore(), directories=directories)
    service.initialize()
    draft = service.active()
    draft.categories.append(_custom_category())
    service.publish(draft, expected_revision=1)
    directories.append(
        {
            "name": "动漫目录",
            "media_type": "电视剧",
            "media_category_id": "tv.anime.jp",
        }
    )

    with pytest.raises(ClassificationPolicyValidationError) as error:
        service.rollback(1, expected_revision=2)

    assert "referenced_category_missing" in {
        issue.code for issue in error.value.result.issues
    }
    assert service.active().revision == 2


def test_rollback_republishes_history_content_with_new_revision() -> None:
    """回滚复制目标内容，但 revision 继续单调递增。"""
    store = _MemoryPolicyStore()
    service = _service(store)
    service.initialize()
    service.publish(_renamed_draft("第二版"), expected_revision=1)
    service.publish(_renamed_draft("第三版"), expected_revision=2)

    rolled_back = service.rollback(2, expected_revision=3)

    assert rolled_back.revision == 4
    assert rolled_back.categories[0].name == "第二版"
    assert [policy.revision for policy in service.history()] == [3, 2, 1]


def test_rollback_rejects_revision_outside_bounded_history() -> None:
    """不存在的目标 revision 不得被解释为默认策略或当前版本。"""
    service = _service(_MemoryPolicyStore())
    service.initialize()

    with pytest.raises(ClassificationPolicyRevisionNotFoundError):
        service.rollback(99, expected_revision=1)


def test_returned_active_state_and_history_cannot_mutate_internal_snapshot() -> None:
    """所有读取结果都必须与服务内部活动引用隔离。"""
    service = _service(_MemoryPolicyStore())
    service.initialize()
    service.publish(_renamed_draft("第二版"), expected_revision=1)

    active = service.active()
    state = service.state()
    history = service.history()
    active.categories[0].name = "篡改活动"
    state.active.categories[0].name = "篡改状态"
    history[0].categories[0].name = "篡改历史"

    assert service.active().categories[0].name == "第二版"
    assert service.history()[0].categories[0].name == "未分类"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_async_entrypoints_delegate_to_same_version_semantics() -> None:
    """异步入口通过执行端口复用同一套同步 revision 逻辑。"""
    service = _service(_MemoryPolicyStore(), asynchronous=True)

    initialized = await service.async_initialize()
    published = await service.async_publish(
        _renamed_draft("异步版本"),
        expected_revision=initialized.revision,
    )
    rolled_back = await service.async_rollback(
        initialized.revision,
        expected_revision=published.revision,
    )

    assert (initialized.revision, published.revision, rolled_back.revision) == (
        1,
        2,
        3,
    )
    assert rolled_back.categories[0].name == "未分类"
