"""旧版 TMDB 分类配置兼容入口。"""

from app.application.classification.migration import (
    LegacyClassificationDiagnostic,
    LegacyClassificationMigrationResult,
    legacy_extension_fields_from_policy,
    migrate_legacy_category_config,
)
from app.application.classification.projection import (
    LegacyCategoryProjectionResult,
    build_legacy_tmdb_extension_facts,
    project_classification_policy_to_legacy_config,
    project_policy_to_legacy_category_config,
    project_policy_to_legacy_category_projection,
    resolve_legacy_tmdb_category,
)

__all__ = [
    "LegacyCategoryProjectionResult",
    "LegacyClassificationDiagnostic",
    "LegacyClassificationMigrationResult",
    "build_legacy_tmdb_extension_facts",
    "legacy_extension_fields_from_policy",
    "migrate_legacy_category_config",
    "project_classification_policy_to_legacy_config",
    "project_policy_to_legacy_category_config",
    "project_policy_to_legacy_category_projection",
    "resolve_legacy_tmdb_category",
]
