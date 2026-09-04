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
    """读取持久化策略时丢弃已删除的来源级默认分类字段，不再恢复其行为。"""
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
        policy.pop("source_fallbacks", None)
    return state

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
