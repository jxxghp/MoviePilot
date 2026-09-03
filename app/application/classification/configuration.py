"""分类策略初始化、发布、历史和回滚应用服务。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from functools import partial
from typing import cast

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
    ClassificationFieldDefinition,
    ClassificationPolicy,
    ClassificationPolicyState,
    ClassificationValidationResult,
)

CLASSIFICATION_POLICY_HISTORY_LIMIT = 10


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


def build_default_classification_policy() -> ClassificationPolicy:
    """构造电影、电视剧和音乐均有稳定兜底分类的初始草稿。"""
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
