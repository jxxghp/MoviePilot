"""基于 SystemConfig 单行事务的分类策略状态适配器。"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.classification.contract import (
    ClassificationPolicyConflictError,
    ClassificationPolicyReferenceViolationError,
    ClassificationPolicyStateCorruptError,
    ClassificationReferenceSnapshotValidator,
)
from app.application.directory import (
    DirectoryConfigurationWriteResult,
)
from app.db.models.systemconfig import SystemConfig
from app.schemas.category import ClassificationPolicy, ClassificationPolicyState
from app.schemas.common import JsonData
from app.schemas.types import SystemConfigKey

DirectoryConfigurationNormalizer = Callable[
    [object, ClassificationPolicy | None],
    list[dict[str, Any]] | None,
]
"""使用事务内活动策略规范化目录配置的纯函数。"""


def discard_removed_source_fallbacks(value: Any) -> Any:
    """读取旧持久化策略时兼容已移除的来源级兜底字段。

    来源级兜底不能再直接写入新版策略模型，因此将其转换为策略末尾的
    来源限定 catch-all 分类规则。分类求值器会在已有分类命中后跳过后续
    分类规则，这与旧版“来源兜底仅在规则未命中时生效”的语义一致。
    """
    if not isinstance(value, Mapping):
        return value

    state = copy.deepcopy(dict(value))
    policies: list[dict[str, Any]] = []
    active = state.get("active")
    if isinstance(active, dict):
        policies.append(active)
    history = state.get("history")
    if isinstance(history, list):
        policies.extend(item for item in history if isinstance(item, dict))
    for policy in policies:
        source_fallbacks = policy.pop("source_fallbacks", None)
        if not isinstance(source_fallbacks, Mapping):
            continue
        fallbacks = policy.get("fallbacks")
        tmdb_fallbacks = source_fallbacks.get("themoviedb")
        if isinstance(fallbacks, dict) and isinstance(tmdb_fallbacks, Mapping):
            for media_type, category_id in tmdb_fallbacks.items():
                if fallbacks.get(media_type) != _COMMON_FALLBACK_IDS.get(media_type):
                    continue
                if category_id:
                    fallbacks[media_type] = category_id
        _promote_source_fallback_rules(
            policy,
            {
                source: values
                for source, values in source_fallbacks.items()
                if str(source).strip().casefold() != "themoviedb"
            },
        )
    return state


_SOURCE_FALLBACK_MEDIA_TYPES = frozenset({"电影", "电视剧", "音乐"})
_SOURCE_FALLBACK_MEDIA_KEYS = {
    "电影": "movie",
    "电视剧": "tv",
    "音乐": "music",
}


def _promote_source_fallback_rules(
    policy: dict[str, Any],
    source_fallbacks: Mapping[Any, Any],
) -> None:
    """把旧来源兜底转换为末尾的来源限定分类规则。"""
    raw_rules = policy.get("rules")
    rules = raw_rules if isinstance(raw_rules, list) else []
    if rules is not raw_rules:
        policy["rules"] = rules

    used_ids = {
        str(rule.get("id"))
        for rule in rules
        if isinstance(rule, Mapping) and rule.get("id")
    }
    priorities = [
        _safe_priority(rule.get("priority", index), index)
        for index, rule in enumerate(rules)
        if isinstance(rule, Mapping)
    ]
    next_priority = max(priorities, default=-1) + 1

    for raw_source, raw_fallbacks in source_fallbacks.items():
        source = str(raw_source or "").strip().casefold()
        if not source or not isinstance(raw_fallbacks, Mapping):
            continue
        for raw_media_type, raw_category_id in raw_fallbacks.items():
            media_type = str(raw_media_type or "").strip()
            category_id = str(raw_category_id or "").strip()
            if media_type not in _SOURCE_FALLBACK_MEDIA_TYPES or not category_id:
                continue
            rule_id = f"compat.source-fallback.{source}.{_SOURCE_FALLBACK_MEDIA_KEYS[media_type]}"
            if rule_id in used_ids:
                continue
            rules.append(
                {
                    "id": rule_id,
                    "name": f"兼容来源兜底 · {source} · {media_type}",
                    "kind": "category",
                    "enabled": True,
                    "priority": next_priority,
                    "media_types": [media_type],
                    "sources": [source],
                    "when": {
                        "field": "identity.media_source",
                        "operator": "equals",
                        "value": source,
                    },
                    "target": {"category_id": category_id},
                }
            )
            used_ids.add(rule_id)
            next_priority += 1


def _safe_priority(value: Any, fallback: int) -> int:
    """读取损坏旧策略中的优先级，异常值退回稳定列表位置。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


_COMMON_FALLBACK_IDS = {
    "电影": "movie.uncategorized",
    "电视剧": "tv.uncategorized",
    "音乐": "music.uncategorized",
}

_CONFIGURATION_LOCK_KEYS = (
    SystemConfigKey.MediaClassificationPolicy.value,
    SystemConfigKey.Directories.value,
)
_configuration_mutation_lock = threading.RLock()


def _lock_configuration_rows(session: Session) -> dict[str, SystemConfig]:
    """按固定顺序锁定策略与目录行，避免跨事务形成反向等待。"""
    records: dict[str, SystemConfig] = {}
    for key in _CONFIGURATION_LOCK_KEYS:
        record = session.execute(
            select(SystemConfig)
            .where(SystemConfig.key == key)
            .with_for_update()
        ).scalar_one_or_none()
        if record is not None:
            records[key] = record
    return records


class SystemConfigClassificationPolicyStore:
    """把分类策略状态包原子存入 MediaClassificationPolicy 配置键。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        publish: Callable[[Mapping[SystemConfigKey, Any]], None],
        reference_validator: ClassificationReferenceSnapshotValidator | None = None,
    ) -> None:
        """绑定会话、快照发布端口及事务内外部引用复验函数。"""
        self._session_factory = session_factory
        self._publish = publish
        self._reference_validator = reference_validator

    def load(self) -> ClassificationPolicyState | None:
        """使用独立只读会话从数据库事实源加载完整状态包。"""
        with self._session_factory() as session:
            value = session.execute(
                select(SystemConfig.value).where(
                    SystemConfig.key
                    == SystemConfigKey.MediaClassificationPolicy.value
                )
            ).scalar_one_or_none()
        return self._decode(value)

    def compare_and_set(
        self,
        *,
        expected_revision: int,
        state: ClassificationPolicyState,
    ) -> None:
        """在 SystemConfig 行锁事务中检查 revision 并整体替换状态包。"""
        if state.active.revision != expected_revision + 1:
            raise ValueError("待发布分类策略 revision 必须等于 expected_revision + 1")

        serialized = cast(JsonData, state.model_dump(mode="json"))
        try:
            with _configuration_mutation_lock:
                with self._session_factory() as session:
                    records_by_key = _lock_configuration_rows(session)
                    record = records_by_key.get(
                        SystemConfigKey.MediaClassificationPolicy.value
                    )
                    current_state = self._decode(record.value if record else None)
                    current_revision = (
                        current_state.active.revision if current_state else 0
                    )
                    if current_revision != expected_revision:
                        raise ClassificationPolicyConflictError(
                            expected_revision=expected_revision,
                            current_revision=current_revision,
                        )
                    if self._reference_validator is not None:
                        directories = records_by_key.get(
                            SystemConfigKey.Directories.value
                        )
                        validation = self._reference_validator(
                            state.active,
                            directories.value if directories else None,
                        )
                        if not validation.valid:
                            raise ClassificationPolicyReferenceViolationError(validation)
                    if record is None:
                        session.add(
                            SystemConfig(
                                key=SystemConfigKey.MediaClassificationPolicy.value,
                                value=serialized,
                            )
                        )
                    else:
                        record.value = serialized
                    session.commit()
                self._publish(
                    {SystemConfigKey.MediaClassificationPolicy: serialized}
                )
        except IntegrityError as error:
            current = self.load()
            raise ClassificationPolicyConflictError(
                expected_revision=expected_revision,
                current_revision=current.active.revision if current else 0,
            ) from error
    @staticmethod
    def _decode(value: Any) -> ClassificationPolicyState | None:
        """把 JSON 配置解析为独立状态对象，并标记损坏数据。"""
        if value is None:
            return None
        try:
            return cast(
                ClassificationPolicyState,
                ClassificationPolicyState.model_validate(
                    discard_removed_source_fallbacks(value)
                ),
            )
        except ValidationError as error:
            raise ClassificationPolicyStateCorruptError(
                "MediaClassificationPolicy 配置结构无效"
            ) from error


class SystemConfigDirectoryConfigurationStore:
    """在分类策略与目录配置共锁事务中保存规范化目录快照。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        publish: Callable[[Mapping[SystemConfigKey, Any]], None],
        normalizer: DirectoryConfigurationNormalizer,
    ) -> None:
        """绑定短会话、提交后快照发布器和 Application 规范化函数。"""
        self._session_factory = session_factory
        self._publish = publish
        self._normalizer = normalizer

    def save(self, value: object) -> DirectoryConfigurationWriteResult:
        """锁内读取活动策略、严格规范化目录，并原子写入当前路径快照。"""
        with _configuration_mutation_lock:
            with self._session_factory() as session:
                records_by_key = _lock_configuration_rows(session)
                policy_record = records_by_key.get(
                    SystemConfigKey.MediaClassificationPolicy.value
                )
                policy_state = SystemConfigClassificationPolicyStore._decode(
                    policy_record.value if policy_record else None
                )
                normalized = self._normalizer(
                    value,
                    policy_state.active if policy_state is not None else None,
                )
                serialized = copy.deepcopy(normalized)
                directory_record = records_by_key.get(
                    SystemConfigKey.Directories.value
                )
                changed: bool | None
                if directory_record is not None and directory_record.value == serialized:
                    changed = None
                else:
                    changed = True
                    if directory_record is None:
                        session.add(
                            SystemConfig(
                                key=SystemConfigKey.Directories.value,
                                value=serialized,
                            )
                        )
                    else:
                        directory_record.value = serialized
                session.commit()
            self._publish({SystemConfigKey.Directories: serialized})
        return DirectoryConfigurationWriteResult(
            changed=changed,
            normalized_value=copy.deepcopy(normalized),
        )
