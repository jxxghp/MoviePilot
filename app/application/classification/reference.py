"""稳定分类引用、兼容路径快照和安全目录路径解析。"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol, cast

from app.domain.classification.validation import (
    validate_classification_category_path,
)
from app.schemas.category import (
    ClassificationMediaType,
    ClassificationPolicy,
    ClassificationResult,
    ClassificationSelection,
    ClassificationValidationIssue,
    ClassificationValidationResult,
)
from app.schemas.types import MediaType

ClassificationReferenceState = Literal[
    "resolved",
    "legacy",
    "missing",
    "policy_unavailable",
    "category_missing",
    "category_disabled",
    "media_type_mismatch",
    "invalid_path",
]
"""分类引用解析后的稳定状态集合。"""


class ClassificationPolicyProvider(Protocol):
    """分类引用解析只需要的活动策略读取端口。"""

    def active_policy(self) -> ClassificationPolicy | None:
        """返回当前活动策略；策略不可用时返回空值。"""
        ...


_classification_resolver_lock = threading.RLock()
_classification_resolver: ClassificationCategoryResolver | None = None


def configure_classification_category_resolver(
    resolver: ClassificationCategoryResolver | None,
) -> ClassificationCategoryResolver | None:
    """装配稳定分类引用解析器，并返回旧值供组合根或测试恢复。"""
    global _classification_resolver
    with _classification_resolver_lock:
        previous = _classification_resolver
        _classification_resolver = resolver
    return previous


def reset_classification_category_resolver(
    resolver: ClassificationCategoryResolver | None = None,
) -> None:
    """恢复指定分类解析器；省略参数时清除当前 lifespan 装配。"""
    global _classification_resolver
    with _classification_resolver_lock:
        _classification_resolver = resolver


def classification_category_resolver_snapshot() -> ClassificationCategoryResolver:
    """返回当前解析器；未装配时退化为只接受安全路径快照。"""
    with _classification_resolver_lock:
        resolver = _classification_resolver
    return resolver or ClassificationCategoryResolver(lambda: None)


@dataclass(frozen=True, slots=True)
class ClassificationCategoryResolution:
    """一次分类引用解析产生的稳定 ID、路径和降级说明。"""

    category_id: str | None
    path: tuple[str, ...]
    state: ClassificationReferenceState
    policy_revision: int = 0
    message: str | None = None

    @property
    def requested(self) -> bool:
        """返回调用方是否提供了分类 ID 或路径快照。"""
        return bool(self.category_id or self.path or self.state == "invalid_path")

    @property
    def usable(self) -> bool:
        """返回该解析结果是否包含可安全拼装的相对目录路径。"""
        return bool(self.path)

    @property
    def stable(self) -> bool:
        """返回分类 ID 是否已在当前策略中解析成功。"""
        return self.state == "resolved" and bool(self.category_id)

    @property
    def downgraded(self) -> bool:
        """返回本次解析是否从稳定 ID 降级到了兼容路径语义。"""
        return self.state not in {"resolved", "legacy", "missing"}


@dataclass(frozen=True, slots=True)
class EffectiveClassificationSnapshot:
    """一次已生效分类的稳定持久化快照，不依赖当前活动策略。"""

    category_id: str | None = None
    category_path: tuple[str, ...] = ()
    rule_id: str | None = None
    policy_revision: int | None = None
    source: str | None = None

    @property
    def path(self) -> str | None:
        """返回持久化使用的 POSIX 风格相对路径快照。"""
        return "/".join(self.category_path) or None

    @property
    def selected(self) -> bool:
        """返回快照是否声明了稳定分类或兼容路径。"""
        return bool(self.category_id or self.category_path)

    def to_selection(self) -> ClassificationSelection | None:
        """转换为领域分类选择；空快照返回空值。"""
        if not self.selected:
            return None
        return ClassificationSelection(
            category_id=self.category_id,
            category_path=list(self.category_path),
            rule_id=self.rule_id,
            source=self.source,
        )


def effective_classification_snapshot(
    media: object | None,
) -> EffectiveClassificationSnapshot:
    """从最终媒体对象投影分类事实，绝不读取描述性 metadata_category。"""
    if media is None:
        return EffectiveClassificationSnapshot()
    classification = getattr(media, "classification", None)
    selection: ClassificationSelection | None = None
    policy_revision: int | None = None
    if isinstance(classification, ClassificationResult):
        selection = classification.effective
        policy_revision = classification.policy_revision
    if selection is not None:
        path, path_error = _path_snapshot(selection.category_path)
        return EffectiveClassificationSnapshot(
            category_id=str(selection.category_id or "").strip() or None,
            category_path=() if path_error else path,
            rule_id=str(selection.rule_id or "").strip() or None,
            policy_revision=policy_revision,
            source=str(selection.source or "").strip() or None,
        )
    path_snapshot = getattr(media, "library_category", None)
    if not path_snapshot:
        path_snapshot = getattr(media, "category", None)
    path, path_error = _path_snapshot(path_snapshot)
    return EffectiveClassificationSnapshot(
        category_path=() if path_error else path,
        source="legacy" if path and not path_error else None,
    )


def persisted_classification_snapshot(
    *,
    category_id: object = None,
    category_path: object = None,
    rule_id: object = None,
    policy_revision: object = None,
    source: object = None,
) -> EffectiveClassificationSnapshot:
    """从数据库标量构造安全历史快照，不使用当前策略反查旧事实。"""
    path, path_error = _path_snapshot(cast(str | Sequence[str] | None, category_path))
    revision: int | None
    try:
        revision = (
            int(policy_revision)
            if isinstance(policy_revision, (int, str))
            else None
        )
    except ValueError:
        revision = None
    return EffectiveClassificationSnapshot(
        category_id=str(category_id or "").strip() or None,
        category_path=() if path_error else path,
        rule_id=str(rule_id or "").strip() or None,
        policy_revision=revision,
        source=str(source or "").strip() or ("legacy" if path and not path_error else None),
    )


def apply_persisted_classification_snapshot(
    media: object | None,
    snapshot: EffectiveClassificationSnapshot,
) -> object | None:
    """把历史分类原样冻结到媒体副本，避免当前策略覆盖已发生事实。"""
    if media is None or not snapshot.selected:
        return media
    frozen_media = deepcopy(media)
    selection = snapshot.to_selection()
    classification = getattr(frozen_media, "classification", None)
    if isinstance(classification, ClassificationResult):
        result = classification.model_copy(deep=True)
        result.effective = selection
        if snapshot.policy_revision is not None:
            result.policy_revision = snapshot.policy_revision
    else:
        result = ClassificationResult(
            effective=selection,
            policy_revision=snapshot.policy_revision or 0,
            state="complete",
        )
    setattr(frozen_media, "classification", result)
    setattr(frozen_media, "library_category", snapshot.path or "")
    return frozen_media


def subscription_classification_override(
    *,
    category_id: str | None,
    path_snapshot: str | Sequence[str] | None,
    media_type: object,
) -> ClassificationSelection | None:
    """按稳定 ID 优先解析订阅人工覆盖，并兼容安全旧路径。"""
    resolution = classification_category_resolver_snapshot().resolve(
        category_id=category_id,
        path_snapshot=path_snapshot,
        media_type=media_type,
    )
    if not resolution.requested or not (resolution.category_id or resolution.path):
        return None
    return ClassificationSelection(
        category_id=resolution.category_id,
        category_path=list(resolution.path),
        source="subscription",
    )


def normalize_classification_reference_payload(
    payload: Mapping[str, object],
    *,
    media_type: object,
    id_field: str = "media_category_id",
    path_field: str = "media_category",
) -> dict[str, object]:
    """规范化写入载荷中的稳定分类 ID 与兼容路径，并保留 PATCH 清空语义。"""
    normalized = dict(payload)
    has_id = id_field in normalized
    has_path = path_field in normalized
    if not has_id and not has_path:
        return normalized
    if has_id and not str(normalized.get(id_field) or "").strip():
        normalized[id_field] = None
        normalized[path_field] = None
        return normalized

    resolution = classification_category_resolver_snapshot().resolve(
        category_id=cast(str | None, normalized.get(id_field)),
        path_snapshot=cast(str | Sequence[str] | None, normalized.get(path_field)),
        media_type=media_type,
    )
    if has_id and resolution.category_id and not resolution.stable:
        raise ValueError(resolution.message or "分类 ID 无效")
    if not has_id and resolution.requested and not resolution.usable:
        raise ValueError(resolution.message or "分类引用无效")
    normalized[id_field] = resolution.category_id
    normalized[path_field] = "/".join(resolution.path) or None
    return normalized


class ClassificationCategoryResolver:
    """依据当前策略解析稳定分类 ID，并保留显式兼容降级。"""

    def __init__(
        self,
        provider: ClassificationPolicyProvider | Callable[[], ClassificationPolicy | None],
    ) -> None:
        """保存活动策略端口或等价的无参读取函数。"""
        self._provider = provider

    def resolve(
        self,
        *,
        category_id: str | None = None,
        path_snapshot: str | Sequence[str] | None = None,
        media_type: object = None,
    ) -> ClassificationCategoryResolution:
        """优先解析稳定 ID；失败时仅使用通过安全校验的路径快照。"""
        normalized_id = str(category_id or "").strip() or None
        snapshot, snapshot_error = _path_snapshot(path_snapshot)
        normalized_type = classification_media_type(media_type)
        if not normalized_id:
            if snapshot_error:
                return ClassificationCategoryResolution(
                    category_id=None,
                    path=(),
                    state="invalid_path",
                    message=snapshot_error,
                )
            if snapshot:
                return ClassificationCategoryResolution(
                    category_id=None,
                    path=snapshot,
                    state="legacy",
                )
            return ClassificationCategoryResolution(
                category_id=None,
                path=(),
                state="missing",
            )

        policy = self._active_policy()
        if policy is None:
            return self._downgrade(
                category_id=normalized_id,
                snapshot=snapshot,
                snapshot_error=snapshot_error,
                state="policy_unavailable",
                message="分类策略不可用，已降级为兼容路径快照",
            )
        category = next(
            (item for item in policy.categories if item.id == normalized_id),
            None,
        )
        if category is None:
            return self._downgrade(
                category_id=normalized_id,
                snapshot=snapshot,
                snapshot_error=snapshot_error,
                state="category_missing",
                policy_revision=policy.revision,
                message=f"分类 {normalized_id} 已删除或不存在，已降级为兼容路径快照",
            )
        if not category.enabled:
            return self._downgrade(
                category_id=normalized_id,
                snapshot=snapshot,
                snapshot_error=snapshot_error,
                state="category_disabled",
                policy_revision=policy.revision,
                message=f"分类 {normalized_id} 已禁用，已降级为兼容路径快照",
            )
        if normalized_type and category.media_type != normalized_type:
            return self._downgrade(
                category_id=normalized_id,
                snapshot=snapshot,
                snapshot_error=snapshot_error,
                state="media_type_mismatch",
                policy_revision=policy.revision,
                message=(
                    f"分类 {normalized_id} 属于 {category.media_type}，"
                    f"与 {normalized_type} 不一致，已降级为兼容路径快照"
                ),
            )
        try:
            path = validate_classification_category_path(category.path)
        except ValueError as error:
            return self._downgrade(
                category_id=normalized_id,
                snapshot=snapshot,
                snapshot_error=snapshot_error,
                state="invalid_path",
                policy_revision=policy.revision,
                message=f"分类 {normalized_id} 的当前路径无效：{error}",
            )
        return ClassificationCategoryResolution(
            category_id=normalized_id,
            path=path,
            state="resolved",
            policy_revision=policy.revision,
        )

    def resolve_media(self, media: object | None) -> ClassificationCategoryResolution:
        """从媒体生效分类读取稳定 ID，缺失时兼容 library_category。"""
        if media is None:
            return ClassificationCategoryResolution(None, (), "missing")
        classification = getattr(media, "classification", None)
        selection: ClassificationSelection | None = None
        if classification is not None:
            selection = classification.effective or classification.recommended
        if selection is not None:
            return self.resolve(
                category_id=selection.category_id,
                path_snapshot=selection.category_path,
                media_type=getattr(media, "type", None),
            )
        path_snapshot = getattr(media, "library_category", None)
        if not path_snapshot:
            path_snapshot = getattr(media, "category", None)
        return self.resolve(
            path_snapshot=path_snapshot,
            media_type=getattr(media, "type", None),
        )

    def category_paths(
        self,
        media_type: object = None,
    ) -> tuple[tuple[str, ...], ...]:
        """返回活动策略中指定媒体类型的启用安全分类路径。"""
        policy = self._active_policy()
        if policy is None:
            return ()
        normalized_type = classification_media_type(media_type)
        paths: list[tuple[str, ...]] = []
        for category in policy.categories:
            if not category.enabled:
                continue
            if normalized_type and category.media_type != normalized_type:
                continue
            try:
                path = validate_classification_category_path(category.path)
            except ValueError:
                continue
            if path not in paths:
                paths.append(path)
        return tuple(paths)

    def _active_policy(self) -> ClassificationPolicy | None:
        """兼容协议对象和轻量无参函数两种策略提供器。"""
        provider = self._provider
        if callable(provider) and not hasattr(provider, "active_policy"):
            return provider()
        return cast(ClassificationPolicyProvider, provider).active_policy()

    @staticmethod
    def _downgrade(
        *,
        category_id: str,
        snapshot: tuple[str, ...],
        snapshot_error: str | None,
        state: ClassificationReferenceState,
        message: str,
        policy_revision: int = 0,
    ) -> ClassificationCategoryResolution:
        """构造保留失败状态的兼容路径降级结果。"""
        if snapshot_error:
            return ClassificationCategoryResolution(
                category_id=category_id,
                path=(),
                state="invalid_path",
                policy_revision=policy_revision,
                message=f"{message}；路径快照无效：{snapshot_error}",
            )
        return ClassificationCategoryResolution(
            category_id=category_id,
            path=snapshot,
            state=state,
            policy_revision=policy_revision,
            message=message,
        )


@dataclass(frozen=True, slots=True)
class ClassificationDirectoryReference:
    """目录配置对一个稳定分类 ID 的只读引用快照。"""

    index: int
    directory_name: str
    category_id: str
    media_type: ClassificationMediaType | None
    path_snapshot: str | None


class DirectoryClassificationReferenceValidator:
    """读取当前目录配置并校验候选策略不会破坏稳定引用。"""

    def __init__(self, provider: Callable[[], object]) -> None:
        """保存每次校验都读取最新目录配置快照的提供函数。"""
        self._provider = provider

    def validate(
        self,
        policy: ClassificationPolicy,
    ) -> ClassificationValidationResult:
        """校验候选策略与当前目录稳定引用的一致性。"""
        return validate_directory_classification_references(
            policy,
            self._provider(),
        )


def validate_directory_classification_references(
    policy: ClassificationPolicy,
    raw_directories: object,
) -> ClassificationValidationResult:
    """校验候选策略不会删除、禁用或改变目录引用分类的媒体类型。"""
    references, issues = _directory_references(raw_directories)
    categories = {category.id: category for category in policy.categories}
    for reference in references:
        category = categories.get(reference.category_id)
        issue_path: list[str | int] = [
            "references",
            "directories",
            reference.index,
            "media_category_id",
        ]
        owner = reference.directory_name or f"第 {reference.index + 1} 项目录"
        if category is None:
            issues.append(
                ClassificationValidationIssue(
                    severity="error",
                    code="referenced_category_missing",
                    message=(
                        f"目录 {owner} 引用的分类 {reference.category_id} 不存在，"
                        "请先解除目录引用"
                    ),
                    path=issue_path,
                )
            )
            continue
        if not category.enabled:
            issues.append(
                ClassificationValidationIssue(
                    severity="error",
                    code="referenced_category_disabled",
                    message=(
                        f"目录 {owner} 引用的分类 {reference.category_id} 已被禁用，"
                        "请先解除目录引用"
                    ),
                    path=issue_path,
                )
            )
            continue
        if reference.media_type and category.media_type != reference.media_type:
            issues.append(
                ClassificationValidationIssue(
                    severity="error",
                    code="referenced_category_media_type_mismatch",
                    message=(
                        f"目录 {owner} 的媒体类型为 {reference.media_type}，"
                        f"但分类 {reference.category_id} 已变为 {category.media_type}"
                    ),
                    path=issue_path,
                )
            )
    return ClassificationValidationResult(
        valid=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )


def _directory_references(
    raw_directories: object,
) -> tuple[list[ClassificationDirectoryReference], list[ClassificationValidationIssue]]:
    """从兼容目录配置中投影稳定引用，并保留损坏配置诊断。"""
    if raw_directories is None:
        return [], []
    if not isinstance(raw_directories, list):
        return [], [
            ClassificationValidationIssue(
                severity="error",
                code="directory_reference_config_invalid",
                message="目录配置不是数组，无法验证稳定分类引用",
                path=["references", "directories"],
            )
        ]
    references: list[ClassificationDirectoryReference] = []
    issues: list[ClassificationValidationIssue] = []
    for index, item in enumerate(raw_directories):
        if item is None:
            continue
        if not isinstance(item, dict):
            issues.append(
                ClassificationValidationIssue(
                    severity="error",
                    code="directory_reference_config_invalid",
                    message=f"第 {index + 1} 项目录配置不是对象",
                    path=["references", "directories", index],
                )
            )
            continue
        category_id = str(item.get("media_category_id") or "").strip()
        if not category_id:
            continue
        raw_media_type = item.get("media_type")
        media_type = classification_media_type(raw_media_type)
        if raw_media_type and media_type is None:
            issues.append(
                ClassificationValidationIssue(
                    severity="error",
                    code="referenced_category_media_type_mismatch",
                    message=(
                        f"第 {index + 1} 项目录引用分类 {category_id}，"
                        f"但媒体类型 {raw_media_type!r} 无效"
                    ),
                    path=["references", "directories", index, "media_type"],
                )
            )
            continue
        references.append(
            ClassificationDirectoryReference(
                index=index,
                directory_name=str(item.get("name") or "").strip(),
                category_id=category_id,
                media_type=media_type,
                path_snapshot=(
                    str(item.get("media_category") or "").strip() or None
                ),
            )
        )
    return references, issues


def classification_media_type(value: object) -> ClassificationMediaType | None:
    """把 MediaType、中文值或 Agent 值规范为分类策略媒体类型。"""
    if isinstance(value, Enum):
        value = value.value
    text = str(value or "").strip()
    if text in {MediaType.MOVIE.value, MediaType.TV.value, MediaType.MUSIC.value}:
        return cast(ClassificationMediaType, text)
    agent_type = MediaType.from_agent(text)
    if agent_type in {MediaType.MOVIE, MediaType.TV, MediaType.MUSIC}:
        return cast(ClassificationMediaType, agent_type.value)
    return None


def append_classification_category_path(
    root: Path,
    segments: Sequence[str],
) -> Path:
    """把安全分类路径段逐级追加到目标根目录。"""
    return root.joinpath(*validate_classification_category_path(segments))


def category_path_below_media_type(
    segments: Sequence[str],
    media_type: object,
    *,
    type_folder_enabled: bool,
) -> tuple[str, ...]:
    """兼容移除已重复包含在分类快照首段的媒体类型根目录。"""
    path = validate_classification_category_path(segments)
    if not type_folder_enabled or not path:
        return path
    normalized_type = classification_media_type(media_type)
    if normalized_type and path[0] == normalized_type:
        return path[1:]
    return path


def ensure_path_within_root(root: Path, target: Path) -> Path:
    """拒绝重命名或插件结果逃逸已选定的目标根目录。"""
    pure_root, pure_target = _comparable_paths(root, target)
    if ".." in pure_target.parts:
        raise ValueError(f"目标路径包含上级目录，不能越过目标根目录：{target}")
    if pure_target != pure_root and not pure_target.is_relative_to(pure_root):
        raise ValueError(f"目标路径不在已选定目录内：{target}")
    return target


def _path_snapshot(
    value: str | Sequence[str] | None,
) -> tuple[tuple[str, ...], str | None]:
    """把兼容字符串或路径段数组转换为受校验的不可变快照。"""
    if value is None:
        return (), None
    if isinstance(value, str):
        if not value:
            return (), None
        raw_segments: Sequence[object] = value.split("/")
    else:
        raw_segments = value
    segments: list[str] = []
    for segment in raw_segments:
        if not isinstance(segment, str):
            return (), "目录路径段必须是字符串"
        normalized = segment.strip()
        if normalized:
            segments.append(normalized)
    try:
        return validate_classification_category_path(segments), None
    except (TypeError, ValueError) as error:
        return (), str(error)


def _comparable_paths(root: Path, target: Path) -> tuple[PurePath, PurePath]:
    """按 POSIX 或 Windows drive 语义构造可比较的纯路径。"""
    root_text = root.as_posix()
    target_text = target.as_posix()
    if re.match(r"^[A-Za-z]:/", root_text):
        return PureWindowsPath(root_text), PureWindowsPath(target_text)
    return PurePosixPath(root_text), PurePosixPath(target_text)
