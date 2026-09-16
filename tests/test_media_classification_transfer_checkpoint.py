"""Durable transfer 分类快照的版本化冻结与离线恢复合同。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.application.classification.reference import EffectiveClassificationSnapshot
from app.application.transfer import workflow as transfer_application
from app.application.transfer.execution import build_transfer_checkpoint_fingerprint
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType
from tests.test_transfer_planning_checkpoint import (
    _bind_checkpoint,
    _bind_planning_input,
    _chain,
    _checkpoint,
    _planned_admission,
    _planning_input,
    _resolved_checkpoint,
    _source_snapshot,
    _task,
)

FROZEN_CLASSIFICATION = {
    "category_id": "movie.frozen",
    "library_category": "电影/动画",
    "classification_rule_id": "rule.frozen",
    "classification_policy_revision": 17,
    "classification_source": "manual",
}
EMPTY_CLASSIFICATION = {
    "category_id": None,
    "library_category": None,
    "classification_rule_id": None,
    "classification_policy_revision": None,
    "classification_source": None,
}


def _classified_media(
    *,
    category_id: str = "movie.effective",
    category_path: tuple[str, ...] = ("电影", "生效分类"),
    rule_id: str = "rule.effective",
    revision: int = 42,
    source: str = "automatic",
) -> MediaInfo:
    """构造同时包含推荐、生效和描述性分类的最终媒体对象。"""
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="123",
        type=MediaType.MOVIE,
        title="Frozen Movie",
        library_category="/".join(category_path),
        metadata_category="metadata/不得冻结",
        classification=ClassificationResult(
            recommended=ClassificationSelection(
                category_id="movie.recommended",
                category_path=["电影", "推荐分类"],
                rule_id="rule.recommended",
                source="automatic",
            ),
            effective=ClassificationSelection(
                category_id=category_id,
                category_path=list(category_path),
                rule_id=rule_id,
                source=source,
            ),
            policy_revision=revision,
            state="complete",
        ),
    )


def _v2_checkpoint(
    *,
    classification_snapshot: dict[str, object] = FROZEN_CLASSIFICATION,
    resolved_mediainfo: dict[str, object] | None = None,
):
    """从现有 planned fixture 构造带顶层分类快照的 v2 检查点。"""
    payload = _resolved_checkpoint().to_payload()
    payload["schema_version"] = 2
    payload["classification_snapshot"] = dict(classification_snapshot)
    if resolved_mediainfo is not None:
        payload["resolved_mediainfo"] = resolved_mediainfo
    return transfer_application.TransferPlanCheckpoint.from_payload(payload)


def _provider_v2_checkpoint(
    classification_snapshot: dict[str, object] = FROZEN_CLASSIFICATION,
):
    """构造冻结旧 provider 调用和分类事实的 provider_pending 检查点。"""
    resolved = _resolved_checkpoint()
    invocation = transfer_application.TransferProviderInvocationSnapshot(
        fileitem=_source_snapshot(),
        meta=resolved.resolved_meta,
        meta_kind=resolved.resolved_meta_kind,
        mediainfo=_classified_media().to_dict(),
        mediainfo_kind="MediaInfo",
        target_directory={
            "library_storage": "local",
            "library_path": "/library",
            "transfer_type": "copy",
        },
        target_storage="local",
        target_path="/library",
        transfer_type="copy",
        scrape=True,
        library_type_folder=False,
        library_category_folder=True,
        episodes_info=(),
        preview=False,
    )
    provider = transfer_application.TransferProviderReference(
        plugin_id="FrozenProvider",
        plugin_name="冻结分类插件",
    )
    checkpoint = transfer_application.TransferPlanCheckpoint(
        planning_input=resolved.planning_input,
        target_storage="",
        root_target_path="",
        final_target_path="",
        resolved_transfer_type="",
        items=(),
        resolved_meta=invocation.meta,
        resolved_meta_kind=invocation.meta_kind,
        resolved_mediainfo=invocation.mediainfo,
        resolved_mediainfo_kind=invocation.mediainfo_kind,
        legacy_transfer_providers=(provider,),
        provider_invocation=invocation,
    )
    payload = checkpoint.to_payload()
    payload["schema_version"] = 2
    payload["classification_snapshot"] = dict(classification_snapshot)
    return transfer_application.TransferPlanCheckpoint.from_payload(payload)


def _assert_classification_snapshot(
    checkpoint,
    expected: dict[str, object],
    *,
    expect_payload: bool = True,
) -> EffectiveClassificationSnapshot:
    """断言类型化快照，并按版本要求核对五字段 JSON 投影。"""
    snapshot = getattr(checkpoint, "classification_snapshot")
    assert isinstance(snapshot, EffectiveClassificationSnapshot)
    assert {
        "category_id": snapshot.category_id,
        "library_category": snapshot.path,
        "classification_rule_id": snapshot.rule_id,
        "classification_policy_revision": snapshot.policy_revision,
        "classification_source": snapshot.source,
    } == expected
    payload = checkpoint.to_payload()
    if expect_payload:
        assert payload["classification_snapshot"] == expected
    else:
        assert "classification_snapshot" not in payload
    return snapshot


def _capture_checkpoint_repository(task, captured: list[object]) -> Mock:
    """构造回读持久投影并记录每次 CAS 提交内容的仓储替身。"""
    repository = Mock()

    def checkpoint_plan(**kwargs):
        """记录待持久检查点，并模拟仓储 JSON 往返后的 planned 投影。"""
        checkpoint = kwargs["checkpoint"]
        persisted = transfer_application.TransferPlanCheckpoint.from_payload(
            checkpoint.to_payload()
        )
        captured.append(persisted)
        return _planned_admission(task, persisted)

    repository.checkpoint_plan.side_effect = checkpoint_plan
    return repository


def test_v2_classification_snapshot_round_trip_preserves_all_five_fields() -> None:
    """v2 JSON 往返必须保留分类 ID、路径、规则、revision 和来源。"""
    checkpoint = _v2_checkpoint()

    restored = transfer_application.TransferPlanCheckpoint.from_payload(
        checkpoint.to_payload()
    )

    assert restored.schema_version == 2
    _assert_classification_snapshot(restored, FROZEN_CLASSIFICATION)
    assert restored.fingerprint == checkpoint.fingerprint


@pytest.mark.parametrize(
    "unsafe_path",
    ("../逃逸", "/绝对路径", "电影\\逃逸"),
)
def test_v2_classification_snapshot_rejects_unsafe_relative_paths(
    unsafe_path: str,
) -> None:
    """v2 快照不得接受目录穿越、绝对路径或非 POSIX 分隔符。"""
    payload = _resolved_checkpoint().to_payload()
    payload["schema_version"] = 2
    payload["classification_snapshot"] = {
        **FROZEN_CLASSIFICATION,
        "library_category": unsafe_path,
    }

    with pytest.raises(ValueError, match="分类.*路径|library_category|相对路径"):
        transfer_application.TransferPlanCheckpoint.from_payload(payload)


def test_host_checkpoint_creation_freezes_effective_selection_only() -> None:
    """宿主计划只冻结 effective，不得使用 recommended 或 metadata_category。"""
    captured: list[object] = []
    task = _task()
    task.meta = MetaBase("Frozen.Movie.2026.mkv")
    task.mediainfo = _classified_media()
    task.bind_admission_task_id("task-host-classification")
    _bind_planning_input(task, _planning_input())
    repository = _capture_checkpoint_repository(task, captured)
    chain = _chain(repository=repository, checkpoint=_checkpoint())

    chain._plan_checkpoint_and_execute(task)

    assert len(captured) == 1
    _assert_classification_snapshot(
        captured[0],
        {
            "category_id": "movie.effective",
            "library_category": "电影/生效分类",
            "classification_rule_id": "rule.effective",
            "classification_policy_revision": 42,
            "classification_source": "automatic",
        },
    )


def test_provider_pending_creation_freezes_effective_selection_only() -> None:
    """provider_pending 必须在插件副作用前冻结与宿主相同的 effective 分类。"""
    captured: list[object] = []
    task = _task()
    task.meta = MetaBase("Frozen.Movie.2026.mkv")
    task.mediainfo = _classified_media(source="subscription")
    task.bind_admission_task_id("task-provider-classification")
    _bind_planning_input(task, _planning_input())
    repository = _capture_checkpoint_repository(task, captured)
    chain = _chain(repository=repository)
    chain._module_dispatcher.freeze_plugin_providers.return_value = (
        SimpleNamespace(
            plugin_id="FrozenProvider",
            plugin_name="冻结分类插件",
            method="transfer",
        ),
    )
    chain._module_dispatcher.execute_frozen_plugin_providers.return_value = (
        TransferInfo(
            success=True,
            fileitem=task.fileitem,
            transfer_type="copy",
        )
    )

    chain._plan_checkpoint_and_execute(task)

    assert len(captured) == 1
    assert captured[0].is_provider_pending is True
    _assert_classification_snapshot(
        captured[0],
        {
            "category_id": "movie.effective",
            "library_category": "电影/生效分类",
            "classification_rule_id": "rule.effective",
            "classification_policy_revision": 42,
            "classification_source": "subscription",
        },
    )


def test_planned_replay_restores_top_level_snapshot_without_live_dependencies() -> None:
    """策略、识别和目录服务漂移或失效时，planned 恢复仍使用顶层旧分类。"""
    drifted_media = _classified_media(
        category_id="movie.drifted",
        category_path=("电影", "新策略"),
        rule_id="rule.drifted",
        revision=99,
    )
    checkpoint = _v2_checkpoint(resolved_mediainfo=drifted_media.to_dict())
    task = _task()
    task.meta = MetaBase("Drifted.Movie.2026.mkv")
    task.mediainfo = drifted_media
    task.bind_admission_task_id("task-classification-replay")
    _bind_planning_input(task, checkpoint.planning_input)
    _bind_checkpoint(task, checkpoint)
    chain = _chain(repository=Mock(), checkpoint=checkpoint)
    chain.jobview = Mock()
    chain._TransferChain__finish_scrape_batch_task = Mock()

    def unavailable(*_args, **_kwargs):
        """确保 planned 恢复没有读取任何当前在线分类依赖。"""
        raise AssertionError("planned replay 不得读取当前识别、目录或分类策略")

    with (
        patch("app.chain.transfer.execution.MediaChain", side_effect=unavailable),
        patch("app.chain.transfer.execution.DirectoryHelper", side_effect=unavailable),
        patch(
            "app.application.classification.reference.classification_category_resolver_snapshot",
            side_effect=unavailable,
        ),
    ):
        result = chain._TransferChain__handle_transfer(task)

    assert result[0] is True
    execute_call = chain.execute_transfer_plan.call_args
    restored_media = execute_call.kwargs["mediainfo"]
    assert restored_media.library_category == "电影/动画"
    assert restored_media.classification is not None
    assert restored_media.classification.policy_revision == 17
    assert restored_media.classification.effective == ClassificationSelection(
        category_id="movie.frozen",
        category_path=["电影", "动画"],
        rule_id="rule.frozen",
        source="manual",
    )
    chain.plan_transfer.assert_not_called()
    chain._transfer_admissions.checkpoint_plan.assert_not_called()


def test_provider_pending_host_promotion_preserves_original_snapshot() -> None:
    """provider 未接管后的宿主升级只能沿用已提交分类，不得按新策略改写。"""
    provider_checkpoint = _provider_v2_checkpoint()
    promoted: list[object] = []
    task = _task()
    task.meta = MetaBase("Drifted.Movie.2026.mkv")
    task.mediainfo = _classified_media(
        category_id="movie.drifted",
        category_path=("电影", "漂移分类"),
        rule_id="rule.drifted",
        revision=99,
    )
    task.bind_admission_task_id("task-provider-promotion")
    _bind_planning_input(task, provider_checkpoint.planning_input)
    _bind_checkpoint(task, provider_checkpoint)
    repository = _capture_checkpoint_repository(task, promoted)
    host_checkpoint = replace(
        _checkpoint(),
        planning_input=provider_checkpoint.planning_input,
    )
    chain = _chain(repository=repository, checkpoint=host_checkpoint)
    chain._module_dispatcher.execute_frozen_plugin_providers.return_value = None

    chain._plan_checkpoint_and_execute(task)

    assert len(promoted) == 1
    assert promoted[0].is_provider_pending is False
    _assert_classification_snapshot(promoted[0], FROZEN_CLASSIFICATION)
    executed = chain.execute_transfer_plan.call_args.args[0]
    _assert_classification_snapshot(executed, FROZEN_CLASSIFICATION)


def test_v1_payload_derives_snapshot_from_persisted_effective_media() -> None:
    """v1 双读只能从自身媒体 payload 的 effective 构造冻结分类。"""
    payload = _resolved_checkpoint().to_payload()
    payload["schema_version"] = 1
    payload.pop("classification_snapshot", None)
    payload["resolved_mediainfo"] = _classified_media(
        category_id="movie.v1",
        category_path=("电影", "旧检查点"),
        rule_id="rule.v1",
        revision=8,
        source="subscription",
    ).to_dict()

    restored = transfer_application.TransferPlanCheckpoint.from_payload(payload)

    assert restored.schema_version == 1
    _assert_classification_snapshot(
        restored,
        {
            "category_id": "movie.v1",
            "library_category": "电影/旧检查点",
            "classification_rule_id": "rule.v1",
            "classification_policy_revision": 8,
            "classification_source": "subscription",
        },
        expect_payload=False,
    )


def test_v1_payload_ignores_recommended_and_uses_legacy_library_path() -> None:
    """v1 无 effective 时不得冻结推荐或元数据分类，只兼容安全库路径。"""
    media = _classified_media()
    assert media.classification is not None
    media.classification.effective = None
    media.library_category = "电影/旧目录"
    media.metadata_category = "metadata/禁止使用"
    payload = _resolved_checkpoint().to_payload()
    payload["schema_version"] = 1
    payload.pop("classification_snapshot", None)
    payload["resolved_mediainfo"] = media.to_dict()

    restored = transfer_application.TransferPlanCheckpoint.from_payload(payload)

    _assert_classification_snapshot(
        restored,
        {
            "category_id": None,
            "library_category": "电影/旧目录",
            "classification_rule_id": None,
            "classification_policy_revision": None,
            "classification_source": "legacy",
        },
        expect_payload=False,
    )


@pytest.mark.parametrize(
    "unsafe_path",
    ("../逃逸", "/绝对路径", "电影\\逃逸"),
)
def test_v1_unsafe_classification_is_ignored_without_changing_plan_identity(
    unsafe_path: str,
) -> None:
    """v1 历史脏分类应降级为空快照，同时保持原 payload 与计划指纹。"""
    media = _classified_media()
    assert media.classification is not None
    media.classification.effective = None
    media.library_category = unsafe_path
    payload = _resolved_checkpoint().to_payload()
    payload["schema_version"] = 1
    payload.pop("classification_snapshot", None)
    payload["resolved_mediainfo"] = media.to_dict()
    expected_fingerprint = build_transfer_checkpoint_fingerprint(payload)

    restored = transfer_application.TransferPlanCheckpoint.from_payload(payload)

    _assert_classification_snapshot(
        restored,
        EMPTY_CLASSIFICATION,
        expect_payload=False,
    )
    assert restored.to_payload() == payload
    assert restored.fingerprint == expected_fingerprint


def test_recognition_rejection_persists_explicit_empty_classification_snapshot() -> None:
    """尚无媒体对象的确定性拒绝也必须保存五字段显式空快照。"""
    captured: list[object] = []
    task = _task()
    task.meta = MetaBase("Unrecognized.Movie.2026.mkv")
    task.mediainfo = None
    task.bind_admission_task_id("task-empty-classification-rejection")
    planning_input = replace(_planning_input(), mediainfo=None)
    _bind_planning_input(task, planning_input)
    repository = _capture_checkpoint_repository(task, captured)
    chain = _chain(repository=repository)

    result = chain._TransferChain__checkpoint_planning_rejection(
        task,
        "未识别到媒体信息",
    )

    assert result.success is False
    assert len(captured) == 1
    assert captured[0].rejection_error == "未识别到媒体信息"
    _assert_classification_snapshot(captured[0], EMPTY_CLASSIFICATION)
