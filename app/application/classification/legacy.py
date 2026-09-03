"""旧版 TMDB 分类配置兼容入口。"""

from app.application.classification.migration import (
    LegacyClassificationDiagnostic,
    LegacyClassificationMigrationResult,
    build_legacy_tmdb_extension_facts,
    legacy_extension_fields_from_policy,
    migrate_legacy_category_config,
    resolve_legacy_tmdb_category,
)
from app.application.classification.projection import (
    LegacyCategoryProjectionResult,
    project_classification_policy_to_legacy_config,
    project_policy_to_legacy_category_config,
    project_policy_to_legacy_category_projection,
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
