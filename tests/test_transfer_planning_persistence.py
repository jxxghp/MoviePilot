"""整理规划输入与原子检查点持久化测试。"""

from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.application.transfer.workflow import (
    TRANSFER_ADMISSION_ACCEPTED,
    TRANSFER_ADMISSION_PLANNED,
    TRANSFER_ADMISSION_PROVIDER_PENDING,
    TRANSFER_PLAN_CHECKPOINT_VERSION,
    TransferAdmissionConflictError,
    TransferAdmissionProjectionError,
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
    TransferPlanningStateError,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
)
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending


@pytest.fixture
def repository(tmp_path):
    """创建只服务单个测试的 SQLite 整理计划仓储。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'transfer-planning.db'}")
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    return TransactionalTransferAdmissionRepository(sessionmaker(bind=engine))


def _claim(repository, task_id: str):
    """为需要变更规划状态的测试取得独占租约。"""
    claimed = repository.claim_task(
        task_id=task_id,
        owner_id="planning-test-worker",
        lease_seconds=3600,
    )
    assert claimed is not None
    assert claimed.lease_token
    return claimed


def _pending_snapshot(repository, task_id: str) -> dict[str, object]:
    """使用隔离 Session 冻结测试所需的持久状态字段。"""
    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(
            select(TransferPending).where(TransferPending.task_id == task_id)
        ).scalar_one()
        return {
            "state": pending.state,
            "last_error": pending.last_error,
            "checkpoint_payload": pending.checkpoint_payload,
        }


def _planning_input(*, target_path: str = "/library/Movies") -> TransferPlanningInput:
    """构造包含恢复所需媒体上下文的完整规划输入。"""
    return TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": "/downloads/Movie.2026.mkv",
            "type": "file",
            "size": 1024,
        },
        meta={"name": "Movie", "year": 2026},
        mediainfo={"title": "Movie", "tmdb_id": 42},
        target_directory={"storage": "local", "path": "/library"},
        target_storage="local",
        target_path=target_path,
        requested_transfer_type="copy",
        media_source="themoviedb",
        media_id="42",
        media_type="电影",
        need_scrape=True,
        need_rename=True,
        need_notify=True,
        overwrite_mode="always",
        episodes_info=({"season_number": 1, "episode_number": 1},),
        options={"username": "admin", "download_hash": "hash-1"},
    )


def _checkpoint(planning_input: TransferPlanningInput) -> TransferPlanCheckpoint:
    """构造可直接执行且不会再次触发 rename 的计划检查点。"""
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path="/library/Movies/Movie (2026)/Movie.mkv",
        resolved_transfer_type="copy",
        items=(
            TransferPlanItem(
                sequence=0,
                source_fileitem=planning_input.source_fileitem,
                target_storage="local",
                target_path="/library/Movies/Movie (2026)/Movie.mkv",
            ),
        ),
        resolved_meta=planning_input.meta,
        resolved_meta_kind="MetaVideo",
        resolved_mediainfo=planning_input.mediainfo,
        resolved_mediainfo_kind="MediaInfo",
        resolved_episodes_info=planning_input.episodes_info,
        legacy_transfer_providers=(
            TransferProviderReference(
                plugin_id="builtin-filemanager",
                plugin_name="FileManager",
            ),
            TransferProviderReference(
                plugin_id="plugin-provider-a",
                plugin_name="Provider A",
            ),
        ),
        need_scrape=True,
        need_rename=False,
        need_notify=True,
        overwrite_mode="always",
    )


def _provider_checkpoint(
        planning_input: TransferPlanningInput,
) -> TransferPlanCheckpoint:
    """构造只冻结旧 ABI、尚未生成宿主文件计划的检查点。"""
    invocation = TransferProviderInvocationSnapshot(
        fileitem=planning_input.source_fileitem,
        meta=planning_input.meta,
        meta_kind="MetaVideo",
        mediainfo=planning_input.mediainfo,
        mediainfo_kind="MediaInfo",
        target_directory={
            "library_storage": "local",
            "library_path": "/library/Movies",
            "transfer_type": "copy",
        },
        target_storage="local",
        target_path=None,
        transfer_type=None,
        scrape=None,
        library_type_folder=False,
        library_category_folder=None,
        episodes_info=planning_input.episodes_info,
        preview=False,
    )
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="",
        root_target_path="",
        final_target_path="",
        resolved_transfer_type="",
        items=(),
        resolved_meta=invocation.meta,
        resolved_meta_kind=invocation.meta_kind,
        resolved_mediainfo=invocation.mediainfo,
        resolved_mediainfo_kind=invocation.mediainfo_kind,
        resolved_episodes_info=invocation.episodes_info,
        legacy_transfer_providers=(
            TransferProviderReference(
                plugin_id="plugin-provider-a",
                plugin_name="Provider A",
            ),
        ),
        provider_invocation=invocation,
    )


def test_planning_dtos_round_trip_versioned_json() -> None:
    """输入和检查点应完整往返 JSON，并保留有序叶操作。"""
    planning_input = _planning_input()
    checkpoint = _checkpoint(planning_input)

    restored_input = TransferPlanningInput.from_payload(planning_input.to_payload())
    restored_checkpoint = TransferPlanCheckpoint.from_payload(checkpoint.to_payload())

    assert restored_input == planning_input
    assert restored_input.fingerprint == planning_input.fingerprint
    assert restored_checkpoint == checkpoint
    assert [item.sequence for item in restored_checkpoint.items] == [0]
    assert restored_checkpoint.planning_input == planning_input
    assert restored_checkpoint.resolved_mediainfo["tmdb_id"] == 42
    assert restored_checkpoint.resolved_meta_kind == "MetaVideo"
    assert restored_checkpoint.resolved_mediainfo_kind == "MediaInfo"
    assert restored_checkpoint.resolved_episodes_info == planning_input.episodes_info
    assert restored_checkpoint.legacy_transfer_providers == (
        TransferProviderReference("builtin-filemanager", "FileManager"),
        TransferProviderReference("plugin-provider-a", "Provider A"),
    )
    assert restored_checkpoint.legacy_transfer_providers[0].method == "transfer"


def test_provider_invocation_snapshot_round_trip_preserves_optional_values() -> None:
    """旧 ABI 快照往返 JSON 后必须保留 None、False 和自动目录原始值。"""
    checkpoint = _provider_checkpoint(_planning_input())

    restored = TransferPlanCheckpoint.from_payload(checkpoint.to_payload())

    assert restored == checkpoint
    assert restored.is_provider_pending is True
    assert restored.provider_invocation.target_path is None
    assert restored.provider_invocation.transfer_type is None
    assert restored.provider_invocation.scrape is None
    assert restored.provider_invocation.library_type_folder is False
    assert restored.provider_invocation.library_category_folder is None

    invalid_payload = checkpoint.to_payload()
    invalid_payload["provider_invocation"]["schema_version"] = 0
    with pytest.raises(ValueError, match="调用快照版本"):
        TransferPlanCheckpoint.from_payload(invalid_payload)


def test_legacy_checkpoint_payload_defaults_resolved_context() -> None:
    """旧检查点缺少 resolved 字段时仍应恢复为兼容空快照。"""
    payload = _checkpoint(_planning_input()).to_payload()
    for key in (
            "resolved_meta",
            "resolved_meta_kind",
            "resolved_mediainfo",
            "resolved_mediainfo_kind",
            "resolved_episodes_info",
            "legacy_transfer_providers",
    ):
        payload.pop(key)

    restored = TransferPlanCheckpoint.from_payload(payload)

    assert restored.resolved_meta is None
    assert restored.resolved_meta_kind is None
    assert restored.resolved_mediainfo is None
    assert restored.resolved_mediainfo_kind is None
    assert restored.resolved_episodes_info == ()
    assert restored.legacy_transfer_providers == ()


@pytest.mark.parametrize(
    ("plugin_id", "plugin_name", "method"),
    [
        ("", "Provider A", "transfer"),
        ("   ", "Provider A", "transfer"),
        ("provider-a", "", "transfer"),
        ("provider-a", "   ", "transfer"),
        ("provider-a", "Provider A", "delete"),
    ],
)
def test_transfer_provider_reference_rejects_invalid_fields(
        plugin_id,
        plugin_name,
        method,
) -> None:
    """旧 provider 引用必须具有稳定插件身份且只能指向 transfer 方法。"""
    with pytest.raises(ValueError, match="provider"):
        TransferProviderReference(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            method=method,
        )


def test_checkpoint_rejects_duplicate_legacy_provider_plugin_id() -> None:
    """同一 checkpoint 不得以不同名称重复冻结同一插件身份。"""
    with pytest.raises(ValueError, match="plugin_id.*重复"):
        replace(
            _checkpoint(_planning_input()),
            legacy_transfer_providers=(
                TransferProviderReference("provider-a", "Provider A"),
                TransferProviderReference("provider-a", "Provider A Renamed"),
            ),
        )


def test_checkpoint_rejects_non_array_legacy_provider_payload() -> None:
    """JSON 恢复边界不得把单个对象等非数组值当成 provider 序列。"""
    payload = _checkpoint(_planning_input()).to_payload()
    payload["legacy_transfer_providers"] = {
        "plugin_id": "provider-a",
        "plugin_name": "Provider A",
    }

    with pytest.raises(ValueError, match="legacy_transfer_providers"):
        TransferPlanCheckpoint.from_payload(payload)


def test_resolved_context_does_not_change_admission_fingerprint(repository) -> None:
    """规划后上下文属于 checkpoint，不得反向改变已提交的准入指纹。"""
    planning_input = _planning_input()
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    claimed = _claim(repository, admitted.task_id)
    checkpoint = replace(
        _checkpoint(planning_input),
        resolved_meta={"name": "Resolved Movie", "year": 2026},
        resolved_meta_kind="MetaAnime",
        resolved_mediainfo={"title": "Resolved Movie", "tmdb_id": 84},
        resolved_mediainfo_kind="MediaInfo",
        resolved_episodes_info=({"season_number": 2, "episode_number": 3},),
    )

    planned = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=checkpoint,
    )

    assert planned.input_fingerprint == planning_input.fingerprint
    assert planned.planning_input == planning_input
    assert planned.checkpoint.resolved_meta_kind == "MetaAnime"
    assert planned.checkpoint.resolved_mediainfo["tmdb_id"] == 84

    with pytest.raises(TransferPlanningStateError):
        repository.checkpoint_plan(
            task_id=admitted.task_id,
            lease_token=claimed.lease_token,
            input_fingerprint=planning_input.fingerprint,
            checkpoint=replace(
                checkpoint,
                resolved_mediainfo={"title": "Different", "tmdb_id": 85},
            ),
        )


def test_admit_reuses_identical_input_and_rejects_conflict(repository) -> None:
    """同一源文件只允许复用完全相同的规划输入。"""
    planning_input = _planning_input()
    first = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    repeated = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=TransferPlanningInput.from_payload(planning_input.to_payload()),
    )

    assert repeated == first
    with pytest.raises(TransferAdmissionConflictError):
        repository.admit(
            storage="local",
            src_path="/downloads/Movie.2026.mkv",
            planning_input=_planning_input(target_path="/other-library"),
        )


def test_admit_allows_new_generation_when_history_has_previous_task(repository) -> None:
    """历史保留上一代任务投影时，同源新事实仍可形成新任务世代。"""
    with repository._session_factory() as session:  # noqa: SLF001
        session.add(TransferHistory(
            transfer_task_id="settled-task",
            transfer_settlement_revision=1,
            src="/downloads/Movie.2026.mkv",
            src_storage="local",
            status=True,
        ))
        session.commit()

    admission = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=_planning_input(),
    )

    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(select(TransferPending)).scalar_one()
    assert admission.task_id == pending.task_id
    assert admission.task_id != "settled-task"


def test_checkpoint_atomically_advances_and_is_idempotent(repository) -> None:
    """完整计划和 planned 状态应同事务提交且允许相同检查点重试。"""
    planning_input = _planning_input()
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    claimed = _claim(repository, admitted.task_id)
    checkpoint = _checkpoint(planning_input)

    planned = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=checkpoint,
    )
    repeated = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=checkpoint,
    )

    assert planned.state == TRANSFER_ADMISSION_PLANNED
    assert planned.checkpoint == checkpoint
    assert planned.checkpoint.items[0].target_path.endswith("Movie.mkv")
    assert tuple(
        provider.plugin_id
        for provider in planned.checkpoint.legacy_transfer_providers
    ) == (
        "builtin-filemanager",
        "plugin-provider-a",
    )
    assert repeated == planned
    assert _pending_snapshot(repository, admitted.task_id)["state"] == (
        TRANSFER_ADMISSION_PLANNED
    )


def test_provider_pending_checkpoint_atomically_upgrades_to_host_plan(repository) -> None:
    """崩溃可恢复的 provider 快照只能经 CAS 升级为宿主 planned 计划。"""
    planning_input = _planning_input()
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    claimed = _claim(repository, admitted.task_id)
    provider_checkpoint = _provider_checkpoint(planning_input)

    provider_pending = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=provider_checkpoint,
    )

    assert provider_pending.state == TRANSFER_ADMISSION_PROVIDER_PENDING
    assert provider_pending.checkpoint == provider_checkpoint

    repository.record_planning_failure(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        error="host planning unavailable",
    )
    assert repository.release_claim(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        error="host planning unavailable",
    )
    failed = repository.claim_recoverable(
        owner_id="planning-recovery-worker",
        limit=1,
        lease_seconds=3600,
    )[0]
    assert failed.state == TRANSFER_ADMISSION_PROVIDER_PENDING
    assert failed.checkpoint == provider_checkpoint
    assert failed.last_error == "host planning unavailable"

    host_checkpoint = _checkpoint(planning_input)
    planned = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=failed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=host_checkpoint,
    )
    repeated = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=failed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=host_checkpoint,
    )

    assert planned.state == TRANSFER_ADMISSION_PLANNED
    assert planned.checkpoint == host_checkpoint
    assert planned.last_error is None
    assert repeated == planned
    with pytest.raises(TransferPlanningStateError):
        repository.checkpoint_plan(
            task_id=admitted.task_id,
            lease_token=failed.lease_token,
            input_fingerprint=planning_input.fingerprint,
            checkpoint=provider_checkpoint,
        )


def test_checkpoint_rejects_fingerprint_without_partial_state(repository) -> None:
    """错误输入指纹不能写入部分计划或改变 accepted 状态。"""
    planning_input = _planning_input()
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    claimed = _claim(repository, admitted.task_id)

    with pytest.raises(TransferAdmissionConflictError):
        repository.checkpoint_plan(
            task_id=admitted.task_id,
            lease_token=claimed.lease_token,
            input_fingerprint="0" * 64,
            checkpoint=_checkpoint(planning_input),
        )

    recovered = _pending_snapshot(repository, admitted.task_id)
    assert recovered["state"] == TRANSFER_ADMISSION_ACCEPTED
    assert recovered["checkpoint_payload"] is None


def test_planning_failure_stays_accepted_until_success(repository) -> None:
    """规划失败只留痕，后续成功规划应清错并原子推进状态。"""
    planning_input = _planning_input()
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    claimed = _claim(repository, admitted.task_id)

    repository.record_planning_failure(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        error="rename failed",
    )
    failed = _pending_snapshot(repository, admitted.task_id)
    assert failed["state"] == TRANSFER_ADMISSION_ACCEPTED
    assert failed["last_error"] == "rename failed"
    assert failed["checkpoint_payload"] is None

    planned = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=_checkpoint(planning_input),
    )
    assert planned.state == TRANSFER_ADMISSION_PLANNED
    assert planned.last_error is None


def test_checkpoint_rejects_missing_task(repository) -> None:
    """不存在的稳定任务身份不能凭空创建已规划记录。"""
    planning_input = _planning_input()
    with pytest.raises(TransferPlanningStateError):
        repository.checkpoint_plan(
            task_id="missing",
            lease_token="missing-token",
            input_fingerprint=planning_input.fingerprint,
            checkpoint=_checkpoint(planning_input),
        )


def test_canonical_admission_requires_explicit_versioned_planning_input(
        tmp_path,
) -> None:
    """直接 ORM 写入不得伪造默认输入，canonical 仓储必须显式保存完整快照。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'orm-defaults.db'}")
    factory = sessionmaker(bind=engine)
    TransferPending.__table__.create(engine)
    with factory() as session:
        pending = TransferPending(
            storage="local",
            src_path="/downloads/legacy.mkv",
            state=TRANSFER_ADMISSION_ACCEPTED,
            created_at="2026-08-27 10:00:00",
            updated_at="2026-08-27 10:00:00",
        )
        session.add(pending)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    repository = TransactionalTransferAdmissionRepository(factory)
    planning_input = replace(
        _planning_input(),
        source_fileitem={
            "storage": "local",
            "path": "/downloads/legacy.mkv",
            "type": "file",
        },
    )
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/legacy.mkv",
        planning_input=planning_input,
    )

    assert admitted is not None
    assert admitted.planning_input == planning_input
    assert admitted.input_fingerprint == admitted.planning_input.fingerprint
    engine.dispose()


def test_projection_rejects_input_version_and_fingerprint_corruption(tmp_path) -> None:
    """列版本、JSON 和指纹任一不一致时都不得返回伪冻结 DTO。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'input-corruption.db'}")
    factory = sessionmaker(bind=engine)
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    repository = TransactionalTransferAdmissionRepository(factory)
    planning_input = _planning_input()
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    with factory() as session:
        row = session.execute(
            select(TransferPending).where(TransferPending.task_id == admitted.task_id)
        ).scalar_one()
        row.input_version = 2
        session.commit()

    with pytest.raises(TransferAdmissionProjectionError, match="版本"):
        repository.claim_task(
            task_id=admitted.task_id,
            owner_id="corruption-worker",
            lease_seconds=3600,
        )

    with factory() as session:
        row = session.execute(
            select(TransferPending).where(TransferPending.task_id == admitted.task_id)
        ).scalar_one()
        row.input_version = 1
        corrupted = planning_input.to_payload()
        corrupted["media_id"] = "different"
        row.planning_input = corrupted
        session.commit()

    with pytest.raises(TransferAdmissionProjectionError, match="指纹"):
        repository.claim_task(
            task_id=admitted.task_id,
            owner_id="corruption-worker",
            lease_seconds=3600,
        )
    engine.dispose()


def test_projection_rejects_checkpoint_version_corruption(tmp_path) -> None:
    """planned 行的列版本与自包含 checkpoint JSON 必须严格一致。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'checkpoint-corruption.db'}")
    factory = sessionmaker(bind=engine)
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    repository = TransactionalTransferAdmissionRepository(factory)
    planning_input = _planning_input()
    admitted = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )
    claimed = _claim(repository, admitted.task_id)
    repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=_checkpoint(planning_input),
    )
    with factory() as session:
        row = session.execute(
            select(TransferPending).where(TransferPending.task_id == admitted.task_id)
        ).scalar_one()
        row.checkpoint_version = TRANSFER_PLAN_CHECKPOINT_VERSION + 1
        row.lease_expires_at = "2000-01-01 00:00:00.000000"
        session.commit()

    with pytest.raises(TransferAdmissionProjectionError, match="版本"):
        repository.claim_task(
            task_id=admitted.task_id,
            owner_id="direct-corruption-test-worker",
            lease_seconds=3600,
        )
    assert repository.claim_recoverable(
        owner_id="batch-corruption-test-worker",
        limit=1,
        lease_seconds=3600,
    ) == []
    engine.dispose()
