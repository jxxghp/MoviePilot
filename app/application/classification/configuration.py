"""分类策略初始化、发布、历史和回滚应用服务。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from functools import partial
from typing import Union, cast

from app.application.classification.contract import (
    ClassificationPolicyConflictError,
    ClassificationPolicyReferenceValidator,
    ClassificationPolicyReferenceViolationError,
    ClassificationPolicyStore,
)
from app.application.database import AsyncDatabaseExecutor
from app.domain.classification.fields import merge_field_definitions
from app.domain.classification.validation import ClassificationPolicyValidator
from app.schemas.category import (
    ClassificationCategory,
    ClassificationCondition,
    ClassificationFieldDefinition,
    ClassificationOperator,
    ClassificationPolicy,
    ClassificationPolicyState,
    ClassificationRule,
    ClassificationTarget,
    ClassificationValidationResult,
)

CLASSIFICATION_POLICY_HISTORY_LIMIT = 10

_DEFAULT_MUSIC_CATEGORIES = (
    ("music.album", "Album", ("Album",)),
    (
        "music.compilation",
        "Album / Compilation",
        ("Album", "Compilation"),
    ),
    ("music.ep", "EP", ("EP",)),
    ("music.single", "Single", ("Single",)),
)


class ClassificationPolicyNotInitializedError(RuntimeError):
    """表示分类策略服务尚未加载或初始化活动快照。"""


class ClassificationPolicyRevisionNotFoundError(LookupError):
    """表示请求回滚的历史 revision 不在有界历史中。"""


class ClassificationPolicyValidationError(ValueError):
    """表示策略包含阻止发布的结构化校验错误。"""

    def __init__(self, result: ClassificationValidationResult) -> None:
        """保存完整校验结果，供 API 层返回字段级错误。"""
        self.result = result.model_copy(deep=True)
        super().__init__("分类策略校验失败")


def _build_uncategorized_classification_policy() -> ClassificationPolicy:
    """构造旧版仅包含媒体类型兜底分类的初始草稿。"""
    categories = [
        ClassificationCategory(
            id="movie.uncategorized",
            media_type="电影",
            name="未分类",
            path=["未分类"],
        ),
        ClassificationCategory(
            id="tv.uncategorized",
            media_type="电视剧",
            name="未分类",
            path=["未分类"],
        ),
        ClassificationCategory(
            id="music.uncategorized",
            media_type="音乐",
            name="未分类",
            path=["未分类"],
        ),
    ]
    return ClassificationPolicy(
        categories=categories,
        fallbacks={
            "电影": "movie.uncategorized",
            "电视剧": "tv.uncategorized",
            "音乐": "music.uncategorized",
        },
    )


def with_default_music_classification(policy: ClassificationPolicy) -> ClassificationPolicy:
    """为尚未配置音乐分类的策略追加安全、结构化的常用专辑分类。"""
    if not needs_default_music_classification(policy):
        return cast(ClassificationPolicy, policy.model_copy(deep=True))

    categories = [
        *(item.model_copy(deep=True) for item in policy.categories),
        *(
            ClassificationCategory(
                id=category_id,
                media_type="音乐",
                name=name,
                path=list(path),
            )
            for category_id, name, path in _DEFAULT_MUSIC_CATEGORIES
        ),
    ]
    priority = max((item.priority for item in policy.rules), default=-1) + 1
    rules = [*(item.model_copy(deep=True) for item in policy.rules)]

    def append_rule(
        *,
        rule_id: str,
        name: str,
        field: str,
        operator: ClassificationOperator,
        value: Union[str, list[str]],
        category_id: str,
    ) -> None:
        nonlocal priority
        rules.append(
            ClassificationRule(
                id=rule_id,
                name=name,
                kind="category",
                priority=priority,
                media_types=["音乐"],
                when=ClassificationCondition(
                    field=field,
                    operator=operator,
                    value=value,
                ),
                target=ClassificationTarget(category_id=category_id),
            )
        )
        priority += 1

    # 精选集同时具有 Album 主类型，必须在普通 Album 之前匹配。
    append_rule(
        rule_id="music.compilation.default",
        name="音乐精选集",
        field="music.secondary_types",
        operator="contains_any",
        value=["Compilation"],
        category_id="music.compilation",
    )
    for album_type, suffix, category_id in (
        ("EP", "ep", "music.ep"),
        ("Single", "single", "music.single"),
        ("Album", "album", "music.album"),
    ):
        append_rule(
            rule_id=f"music.{suffix}.default",
            name=f"音乐{album_type}",
            field="music.album_type",
            operator="equals",
            value=album_type,
            category_id=category_id,
        )
    return cast(
        ClassificationPolicy,
        policy.model_copy(
            deep=True,
            update={"categories": categories, "rules": rules},
        ),
    )


def needs_default_music_classification(policy: ClassificationPolicy) -> bool:
    """判断音乐侧是否仍为旧版原始兜底，未包含任何用户分类。"""
    music_rules = [item for item in policy.rules if "音乐" in item.media_types]
    music_categories = [
        item for item in policy.categories if item.media_type == "音乐"
    ]
    if music_rules or len(music_categories) != 1:
        return False
    category = music_categories[0]
    return bool(
        category.id == "music.uncategorized"
        and category.name == "未分类"
        and category.path == ["未分类"]
        and category.enabled
        and not category.labels
        and policy.fallbacks.get("音乐") == category.id
    )


def build_default_classification_policy() -> ClassificationPolicy:
    """构造带稳定兜底和常用音乐专辑分类的初始草稿。"""
    return with_default_music_classification(
        _build_uncategorized_classification_policy()
    )


class ClassificationPolicyConfigurationService:
    """维护分类策略的进程内完整快照和数据库 CAS 发布语义。"""

    def __init__(
        self,
        store: ClassificationPolicyStore,
        *,
        extra_fields: Iterable[ClassificationFieldDefinition] = (),
        extra_fields_provider: Callable[
            [], Iterable[ClassificationFieldDefinition]
        ] | None = None,
        clock: Callable[[], datetime] | None = None,
        async_executor: AsyncDatabaseExecutor | None = None,
        reference_validator: ClassificationPolicyReferenceValidator | None = None,
    ) -> None:
        """注入状态仓储、字段目录、时钟、异步端口和外部引用校验器。"""
        self._store = store
        self._extra_fields = tuple(
            field.model_copy(deep=True)
            for field in merge_field_definitions(extra_fields)
            if field.id.startswith("extensions.")
        )
        self._extra_fields_provider = extra_fields_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._async_executor = async_executor
        self._reference_validator = reference_validator
        self._lock = threading.RLock()
        self._state: ClassificationPolicyState | None = None

    def initialize(
        self,
        initial_policy: ClassificationPolicy | None = None,
    ) -> ClassificationPolicy:
        """加载已发布状态；配置不存在时以指定草稿或默认策略发布 revision 1。"""
        with self._lock:
            stored = self._store.load()
            if stored is not None:
                self._publish_memory_snapshot(stored)
                return self.active()
            draft = initial_policy or build_default_classification_policy()
            return self._publish_locked(
                draft=draft,
                expected_revision=0,
                current_state=None,
            )

    def reload(self) -> ClassificationPolicy:
        """从持久化事实源重新加载完整状态包并替换进程内快照。"""
        with self._lock:
            stored = self._store.load()
            if stored is None:
                raise ClassificationPolicyNotInitializedError("分类策略尚未初始化")
            self._publish_memory_snapshot(stored)
            return self.active()

    def active(self) -> ClassificationPolicy:
        """返回与内部活动引用隔离的当前策略副本。"""
        with self._lock:
            return cast(
                ClassificationPolicy,
                self._require_state().active.model_copy(deep=True),
            )

    def state(self) -> ClassificationPolicyState:
        """返回与内部活动引用隔离的完整状态包副本。"""
        with self._lock:
            return cast(
                ClassificationPolicyState,
                self._require_state().model_copy(deep=True),
            )

    def history(self) -> tuple[ClassificationPolicy, ...]:
        """按 revision 从新到旧返回隔离的有界历史快照。"""
        with self._lock:
            return tuple(
                policy.model_copy(deep=True)
                for policy in self._require_state().history
            )

    def validate(self, draft: ClassificationPolicy) -> ClassificationValidationResult:
        """使用发布时相同字段和外部引用约束校验草稿，但不修改状态。"""
        with self._lock:
            policy_result = ClassificationPolicyValidator.validate(
                draft,
                extra_fields=self._current_extra_fields(),
            )
            if self._reference_validator is None:
                return policy_result
            reference_result = self._reference_validator.validate(draft)
            issues = [
                *(issue.model_copy(deep=True) for issue in policy_result.issues),
                *(issue.model_copy(deep=True) for issue in reference_result.issues),
            ]
            return ClassificationValidationResult(
                valid=not any(issue.severity == "error" for issue in issues),
                issues=issues,
            )

    def register_extra_fields(
        self,
        extra_fields: Iterable[ClassificationFieldDefinition],
    ) -> tuple[ClassificationFieldDefinition, ...]:
        """合并动态来源字段目录并返回隔离后的当前扩展字段快照。"""
        with self._lock:
            merged = merge_field_definitions((*self._extra_fields, *tuple(extra_fields)))
            self._extra_fields = tuple(
                field.model_copy(deep=True)
                for field in merged
                if field.id.startswith("extensions.")
            )
            return tuple(field.model_copy(deep=True) for field in self._extra_fields)

    def extra_fields(self) -> tuple[ClassificationFieldDefinition, ...]:
        """返回与内部字段目录隔离的动态来源字段定义。"""
        with self._lock:
            return tuple(
                field.model_copy(deep=True)
                for field in self._current_extra_fields()
            )

    def _current_extra_fields(self) -> tuple[ClassificationFieldDefinition, ...]:
        """合并静态兼容字段和插件运行时当前快照，不缓存可卸载声明。"""
        try:
            dynamic = (
                tuple(self._extra_fields_provider())
                if self._extra_fields_provider is not None
                else ()
            )
        except Exception:  # noqa: BLE001  可选插件目录不可阻断静态策略管理
            dynamic = ()
        return tuple(
            field.model_copy(deep=True)
            for field in merge_field_definitions((*self._extra_fields, *dynamic))
            if field.id.startswith("extensions.")
        )

    def publish(
        self,
        draft: ClassificationPolicy,
        *,
        expected_revision: int,
    ) -> ClassificationPolicy:
        """校验草稿并以 CAS 发布下一个 revision。"""
        with self._lock:
            current_state = self._require_state()
            return self._publish_locked(
                draft=draft,
                expected_revision=expected_revision,
                current_state=current_state,
            )

    def rollback(
        self,
        target_revision: int,
        *,
        expected_revision: int,
    ) -> ClassificationPolicy:
        """选择历史内容并以新的单调 revision 发布，不复用旧版本号。"""
        with self._lock:
            current_state = self._require_state()
            target = next(
                (
                    policy
                    for policy in current_state.history
                    if policy.revision == target_revision
                ),
                None,
            )
            if target is None:
                raise ClassificationPolicyRevisionNotFoundError(
                    f"分类策略历史 revision {target_revision} 不存在"
                )
            return self._publish_locked(
                draft=target,
                expected_revision=expected_revision,
                current_state=current_state,
            )

    async def async_initialize(
        self,
        initial_policy: ClassificationPolicy | None = None,
    ) -> ClassificationPolicy:
        """在线程执行端口中初始化分类策略，避免阻塞异步启动流程。"""
        return await self._run_async(partial(self.initialize, initial_policy))

    async def async_reload(self) -> ClassificationPolicy:
        """在线程执行端口中重新加载分类策略。"""
        return await self._run_async(self.reload)

    async def async_publish(
        self,
        draft: ClassificationPolicy,
        *,
        expected_revision: int,
    ) -> ClassificationPolicy:
        """在线程执行端口中校验并原子发布分类策略。"""
        return await self._run_async(
            partial(self.publish, draft, expected_revision=expected_revision)
        )

    async def async_rollback(
        self,
        target_revision: int,
        *,
        expected_revision: int,
    ) -> ClassificationPolicy:
        """在线程执行端口中把历史内容发布为新的 revision。"""
        return await self._run_async(
            partial(
                self.rollback,
                target_revision,
                expected_revision=expected_revision,
            )
        )

    def _publish_locked(
        self,
        *,
        draft: ClassificationPolicy,
        expected_revision: int,
        current_state: ClassificationPolicyState | None,
    ) -> ClassificationPolicy:
        """在进程锁内构造、校验、CAS 持久化并发布完整状态包。"""
        current_revision = current_state.active.revision if current_state else 0
        if expected_revision != current_revision:
            raise ClassificationPolicyConflictError(
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        candidate = draft.model_copy(
            deep=True,
            update={
                "revision": current_revision + 1,
                "updated_at": self._clock(),
            },
        )
        validation = self.validate(candidate)
        if not validation.valid:
            raise ClassificationPolicyValidationError(validation)
        history = [] if current_state is None else [
            current_state.active.model_copy(deep=True),
            *(policy.model_copy(deep=True) for policy in current_state.history),
        ]
        desired = ClassificationPolicyState(
            active=candidate,
            history=history[:CLASSIFICATION_POLICY_HISTORY_LIMIT],
        )
        try:
            self._store.compare_and_set(
                expected_revision=expected_revision,
                state=desired,
            )
        except ClassificationPolicyReferenceViolationError as error:
            raise ClassificationPolicyValidationError(error.result) from error
        except Exception:
            stored = self._store.load()
            if stored is not None:
                self._publish_memory_snapshot(stored)
            raise
        self._publish_memory_snapshot(desired)
        return cast(
            ClassificationPolicy,
            desired.active.model_copy(deep=True),
        )

    def _publish_memory_snapshot(self, state: ClassificationPolicyState) -> None:
        """以深拷贝整体替换内部快照，禁止暴露持久化对象引用。"""
        self._state = state.model_copy(deep=True)

    def _require_state(self) -> ClassificationPolicyState:
        """返回内部活动状态；未初始化时明确失败。"""
        if self._state is None:
            raise ClassificationPolicyNotInitializedError("分类策略尚未初始化")
        return self._state

    async def _run_async(
        self,
        operation: Callable[[], ClassificationPolicy],
    ) -> ClassificationPolicy:
        """通过注入的数据库执行端口运行一个同步短事务操作。"""
        if self._async_executor is None:
            raise RuntimeError("分类策略异步数据库执行端口尚未配置")
        return cast(
            ClassificationPolicy,
            await self._async_executor.run(operation),
        )
