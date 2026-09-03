"""媒体分类策略的启动迁移、持久化适配和运行时组合。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal, Mapping, cast

import ruamel.yaml
from pydantic import ValidationError

from app.application.classification.configuration import (
    ClassificationPolicyConfigurationService,
)
from app.application.classification.contract import (
    ClassificationPolicyStateCorruptError,
)
from app.application.classification.execution import (
    ClassificationEnrichmentPort,
    ClassificationExecutionService,
    ClassificationExtensionFactsProvider,
)
from app.application.classification.legacy import (
    LegacyClassificationDiagnostic,
    LegacyClassificationMigrationResult,
    legacy_extension_fields_from_policy,
    migrate_legacy_category_config,
)
from app.application.classification.reference import (
    DirectoryClassificationReferenceValidator,
    validate_directory_classification_references,
)
from app.application.classification.runtime import ClassificationRuntime
from app.application.database import AsyncDatabaseExecutor
from app.db.adapters.classification import SystemConfigClassificationPolicyStore
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.runtime.config import Settings
from app.runtime.log import logger
from app.schemas.category import (
    CategoryConfig,
    ClassificationFieldDefinition,
    ClassificationPolicyState,
    ClassificationValidationIssue,
)
from app.schemas.types import SystemConfigKey


@dataclass(frozen=True, slots=True)
class ClassificationComposition:
    """保存启动阶段构造的分类运行时及其一次性迁移状态。"""

    runtime: ClassificationRuntime
    execution: ClassificationExecutionService
    migrated: bool


def _composition(
    runtime: ClassificationRuntime,
    *,
    migrated: bool,
    extension_facts_provider: ClassificationExtensionFactsProvider | None = None,
    enrichment: ClassificationEnrichmentPort | None = None,
) -> ClassificationComposition:
    """为同一分类运行时构造唯一执行服务和组合结果。"""
    return ClassificationComposition(
        runtime=runtime,
        execution=ClassificationExecutionService(
            runtime,
            extension_facts_provider=extension_facts_provider,
            enrichment=enrichment,
        ),
        migrated=migrated,
    )


async def compose_classification(
    *,
    executor: AsyncDatabaseExecutor,
    settings: Settings,
    system_config: SystemConfigOper,
    extra_fields_provider: Callable[
        [], Iterable[ClassificationFieldDefinition]
    ] | None = None,
    extension_facts_provider: ClassificationExtensionFactsProvider | None = None,
    enrichment: ClassificationEnrichmentPort | None = None,
) -> ClassificationComposition:
    """
    构造分类策略服务，并仅在新配置键不存在时读取一次 legacy YAML

    已存在但损坏的新策略不会重新读取 YAML 或覆盖数据库事实，运行时保持不可用并暴露诊断。
    """
    finish = partial(
        _composition,
        extension_facts_provider=extension_facts_provider,
        enrichment=enrichment,
    )
    values = system_config.all()
    policy_key = SystemConfigKey.MediaClassificationPolicy.value
    stored_value = values.get(policy_key)
    extra_fields: tuple[ClassificationFieldDefinition, ...] = ()
    existing_issue: tuple[ClassificationValidationIssue, ...] = ()
    if policy_key in values:
        try:
            stored_state = ClassificationPolicyState.model_validate(stored_value)
            extra_fields = legacy_extension_fields_from_policy(stored_state.active)
        except ValidationError:
            existing_issue = (
                _issue(
                    severity="error",
                    code="classification_policy_corrupt",
                    message="MediaClassificationPolicy 配置结构无效，已停止自动分类并保留数据库原值",
                    path=[policy_key],
                ),
            )

    reference_validator = DirectoryClassificationReferenceValidator(
        lambda: system_config.get(SystemConfigKey.Directories)
    )
    service = ClassificationPolicyConfigurationService(
        SystemConfigClassificationPolicyStore(
            SessionFactory,
            system_config.publish_many,
            validate_directory_classification_references,
        ),
        extra_fields=extra_fields,
        extra_fields_provider=extra_fields_provider,
        async_executor=executor,
        reference_validator=reference_validator,
    )
    if policy_key in values:
        if existing_issue:
            return finish(
                ClassificationRuntime(service, diagnostics=existing_issue),
                migrated=False,
            )
        try:
            await service.async_reload()
        except ClassificationPolicyStateCorruptError as error:
            issue = _issue(
                severity="error",
                code="classification_policy_corrupt",
                message=str(error),
                path=[policy_key],
            )
            return finish(
                ClassificationRuntime(service, diagnostics=(issue,)),
                migrated=False,
            )
        return finish(
            ClassificationRuntime(service),
            migrated=False,
        )

    legacy_path = Path(settings.CONFIG_PATH) / "category.yaml"
    if not await executor.run(legacy_path.exists):
        await service.async_initialize()
        return finish(
            ClassificationRuntime(service),
            migrated=False,
        )

    try:
        raw_legacy = await executor.run(partial(_load_legacy_yaml, legacy_path))
    except Exception as error:
        issue = _issue(
            severity="error",
            code="legacy_category_yaml_invalid",
            message=f"旧分类配置读取失败：{error}",
            path=[str(legacy_path)],
        )
        logger.error(f"旧分类策略自动迁移失败：{error}")
        return finish(
            ClassificationRuntime(service, diagnostics=(issue,)),
            migrated=False,
        )

    migration = migrate_legacy_category_config(raw_legacy)
    legacy_config = _legacy_config_snapshot(raw_legacy)
    if not migration.valid:
        logger.error("旧分类策略无法无损迁移，继续保留 legacy 只读兼容行为")
        return finish(
            ClassificationRuntime(
                service,
                legacy_config=legacy_config,
                diagnostics=_migration_issues(migration.issues),
            ),
            migrated=False,
        )

    service.register_extra_fields(migration.extra_fields)
    await service.async_initialize(migration.policy)
    _log_migration_issues(migration)
    logger.info("已将 category.yaml 无损迁移为 MediaClassificationPolicy revision 1")
    return finish(
        ClassificationRuntime(
            service,
            legacy_config=legacy_config,
            diagnostics=_migration_issues(migration.issues),
        ),
        migrated=True,
    )


def _load_legacy_yaml(path: Path) -> Mapping[str, Any]:
    """读取保持声明顺序的 legacy YAML 映射，禁止在迁移期间写回文件。"""
    yaml_loader = ruamel.yaml.YAML(typ="safe")
    with path.open("r", encoding="utf-8", errors="replace") as file:
        value = yaml_loader.load(file)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("category.yaml 顶层必须是映射")
    return cast(Mapping[str, Any], value)


def _legacy_config_snapshot(value: Mapping[str, Any]) -> CategoryConfig:
    """为兼容 GET 保存可解析的旧配置快照；异常结构由迁移诊断负责说明。"""
    try:
        return cast(CategoryConfig, CategoryConfig.model_validate(value))
    except ValidationError:
        return CategoryConfig()


def _issue(
    *,
    severity: Literal["error", "warning"],
    code: str,
    message: str,
    path: list[str],
) -> ClassificationValidationIssue:
    """构造启动组合层统一使用的分类诊断。"""
    return ClassificationValidationIssue(
        severity=severity,
        code=code,
        message=message,
        path=path,
    )


def _log_migration_issues(migration: LegacyClassificationMigrationResult) -> None:
    """记录不阻止发布的 legacy 迁移提示。"""
    for issue in migration.issues:
        if issue.severity == "warning":
            logger.warning(f"旧分类策略迁移提示 [{issue.code}]：{issue.message}")


def _migration_issues(
    diagnostics: tuple[LegacyClassificationDiagnostic, ...],
) -> tuple[ClassificationValidationIssue, ...]:
    """把纯迁移诊断转换为运行时统一诊断 schema。"""
    return tuple(
        ClassificationValidationIssue(
            severity=diagnostic.severity,
            code=diagnostic.code,
            message=diagnostic.message,
            path=list(diagnostic.path),
        )
        for diagnostic in diagnostics
    )
