"""分类策略运行时门面及 legacy 只读投影。"""

from __future__ import annotations

import threading
from typing import Optional, cast

from app.application.classification.configuration import (
    ClassificationPolicyConfigurationService,
    ClassificationPolicyNotInitializedError,
)
from app.application.classification.contract import ClassificationPolicyConflictError
from app.application.classification.legacy import (
    project_policy_to_legacy_category_config,
)
from app.schemas.category import (
    CategoryConfig,
    ClassificationPolicy,
    ClassificationValidationIssue,
    MediaCategoryMap,
)


class ClassificationRuntime:
    """
    向 API 和业务链暴露当前分类策略，并封装 legacy 迁移失败回退。

    持久化和 revision 并发控制由配置服务负责；本门面只协调动态字段登记和兼容投影。
    """

    def __init__(
        self,
        service: ClassificationPolicyConfigurationService,
        *,
        legacy_config: Optional[CategoryConfig] = None,
        diagnostics: tuple[ClassificationValidationIssue, ...] = (),
    ) -> None:
        """绑定策略服务，并保存未成功迁移时的只读 legacy 快照。"""
        self._service = service
        self._legacy_config = (legacy_config or CategoryConfig()).model_copy(deep=True)
        self._diagnostics = tuple(issue.model_copy(deep=True) for issue in diagnostics)
        self._lock = threading.RLock()

    @property
    def service(self) -> ClassificationPolicyConfigurationService:
        """返回当前 lifespan 唯一的策略配置服务。"""
        return self._service

    def active_policy(self) -> Optional[ClassificationPolicy]:
        """返回活动策略；尚未成功迁移或初始化时返回 None。"""
        try:
            return self._service.active()
        except ClassificationPolicyNotInitializedError:
            return None

    def require_policy(self) -> ClassificationPolicy:
        """返回活动策略；不可用时保留配置服务的明确未初始化异常。"""
        return self._service.active()

    def diagnostics(self) -> tuple[ClassificationValidationIssue, ...]:
        """返回与内部引用隔离的当前迁移或发布诊断。"""
        with self._lock:
            return tuple(issue.model_copy(deep=True) for issue in self._diagnostics)

    def legacy_config(self) -> CategoryConfig:
        """把活动策略投影为旧配置；策略不可用时返回启动时 legacy 快照。"""
        policy = self.active_policy()
        if policy is not None:
            return project_policy_to_legacy_category_config(policy)
        with self._lock:
            return cast(CategoryConfig, self._legacy_config.model_copy(deep=True))

    def media_categories(self) -> MediaCategoryMap:
        """按策略顺序返回电影、电视剧和音乐的启用分类名称。"""
        categories: dict[str, list[str]] = {
            "电影": [],
            "电视剧": [],
            "音乐": [],
        }
        policy = self.active_policy()
        if policy is None:
            legacy = self.legacy_config()
            categories["电影"] = list((legacy.movie or {}).keys())
            categories["电视剧"] = list((legacy.tv or {}).keys())
            return MediaCategoryMap(categories)
        for category in policy.categories:
            if category.enabled and category.name not in categories[category.media_type]:
                categories[category.media_type].append(category.name)
        return MediaCategoryMap(categories)

    async def publish_policy(
        self,
        policy: ClassificationPolicy,
        *,
        expected_revision: int,
    ) -> ClassificationPolicy:
        """发布新版策略；未迁移的空存储允许以 revision 0 初始化恢复。"""
        current = self.active_policy()
        if current is None:
            if expected_revision != 0:
                raise ClassificationPolicyConflictError(
                    expected_revision=expected_revision,
                    current_revision=0,
                )
            published = await self._service.async_initialize(policy)
        else:
            published = await self._service.async_publish(
                policy,
                expected_revision=expected_revision,
            )
        with self._lock:
            self._diagnostics = ()
        return published
