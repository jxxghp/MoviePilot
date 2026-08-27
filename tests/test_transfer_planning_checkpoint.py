"""整理规划检查点的事务顺序、恢复与无副作用边界合同。"""

import ast
import inspect
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.transfer import workflow as transfer_application
from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionSnapshot,
    TransferExecutionState,
    TransferExecutionStep,
    TransferSettlementResult,
    TransferStepState,
)
from app.application.transfer.workflow import TransferTask
from app.chain.transfer import TransferChain
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.runtime.extensions.module.dispatcher import (
    ModuleInvocationDispatcher,
)
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import TransferInfo


def _planning_contracts():
    """延迟解析新合同，使未落地时每个 pytest 用例给出精确缺口。"""
    return (
        getattr(transfer_application, "TransferPlanningInput"),
        getattr(transfer_application, "TransferPlanItem"),
        getattr(transfer_application, "TransferPlanCheckpoint"),
    )


def _source_snapshot() -> dict[str, object]:
    """返回可独立跨重启还原的源文件快照。"""
    return {
        "storage": "local",
        "path": "/downloads/Movie.2026.mkv",
        "type": "file",
        "name": "Movie.2026.mkv",
        "basename": "Movie.2026",
        "extension": "mkv",
        "size": 1024,
        "modify_time": 1770000000,
        "fileid": "source-1",
    }


def _planning_input(*, preview: bool = False, target_path: str = "/library"):
    """构造包含旧调用意图、媒体提示和目标配置的规划输入。"""
    TransferPlanningInput, _TransferPlanItem, _TransferPlanCheckpoint = (
        _planning_contracts()
    )
    return TransferPlanningInput(
        source_fileitem=_source_snapshot(),
        meta={"title": "Movie.2026", "year": "2026"},
        mediainfo={
            "media_source": "themoviedb",
            "media_id": "123",
            "type": "电影",
        },
        target_directory={"library_path": target_path},
        target_storage="local",
        target_path=target_path,
        requested_transfer_type="copy",
        media_source="themoviedb",
        media_id="123",
        media_type="电影",
        need_scrape=True,
        need_rename=True,
        need_notify=True,
        overwrite_mode="always",
        episodes_info=(),
        preview=preview,
        options={
            "manual": True,
            "background": False,
            "cleanup_dest_fileitem": None,
        },
    )


def _checkpoint(*, preview: bool = False, target_path: str = "/library/Movie (2026)/Movie.mkv"):
    """构造带完整输入和有序执行项的冻结计划。"""
    _TransferPlanningInput, TransferPlanItem, TransferPlanCheckpoint = (
        _planning_contracts()
    )
    planning_input = _planning_input(preview=preview)
    item = TransferPlanItem(
        sequence=0,
        source_fileitem=_source_snapshot(),
        target_storage="local",
        target_path=target_path,
        action="transfer",
    )
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path=target_path,
        resolved_transfer_type="copy",
        items=(item,),
        resolved_meta={
            "org_string": "Movie.2026.mkv",
            "title": "Movie 2026",
            "type": "电影",
        },
        resolved_meta_kind="MetaVideo",
        resolved_mediainfo={
            "title": "Movie",
            "year": "2026",
            "type": "电影",
            "media_source": "themoviedb",
            "media_id": "123",
        },
        resolved_mediainfo_kind="MediaInfo",
        need_scrape=True,
        need_rename=True,
        need_notify=True,
        overwrite_mode="always",
        preview=preview,
    )


def _resolved_checkpoint():
    """构造包含在线识别完成后领域快照的 planned checkpoint。"""
    return replace(
        _checkpoint(),
        resolved_meta={
            "org_string": "Frozen.Show.S01E01.mkv",
            "title": "Frozen Show S01E01",
            "name": "Frozen Show",
            "year": "2026",
            "type": "电视剧",
            "begin_season": 1,
            "begin_episode": 1,
            "episode_list": [1],
        },
        resolved_mediainfo={
            "title": "Frozen Show",
            "year": "2026",
            "type": "电视剧",
            "media_source": "themoviedb",
            "media_id": "987",
            "tmdb_id": 987,
        },
        resolved_episodes_info=(
            {
                "episode_number": 1,
                "season_number": 1,
                "name": "Frozen Episode",
            },
        ),
    )


def _task(*, preview: bool = False) -> TransferTask:
    """构造不触碰真实文件系统的规划任务。"""
    return TransferTask(
        fileitem=FileItem(**_source_snapshot()),
        target_storage="local",
        target_path=Path("/library"),
        transfer_type="copy",
        scrape=True,
        manual=True,
        background=False,
        preview=preview,
    )


def _bind_planning_input(task: TransferTask, planning_input) -> None:
    """通过正式绑定接口保存私有输入，禁止测试写入 Pydantic 内部字典。"""
    binder = getattr(task, "bind_planning_input")
    binder(planning_input)


def _bind_checkpoint(task: TransferTask, checkpoint) -> None:
    """通过正式绑定接口模拟 planned 任务的跨重启恢复。"""
    binder = getattr(task, "bind_plan_checkpoint")
    binder(checkpoint)


def _planned_admission(task: TransferTask, checkpoint):
    """构造仓储 checkpoint 提交后返回的冻结投影。"""
    return SimpleNamespace(
        task_id=task.admission_task_id,
        state="planned",
        planning_input=task.planning_input,
        checkpoint=checkpoint,
    )


class _ExecutionRepositoryStub:
    """为规划编排测试提供严格但内存化的 execution repository。"""

    def __init__(self) -> None:
        """初始化空步骤集合与未启动执行态。"""
        self.steps = {}
        self.state = TransferExecutionState.NOT_STARTED
        self.checkpoint = None

    def get_snapshot(self, *, task_id):
        """返回当前任务的类型化执行投影。"""
        return TransferExecutionSnapshot(
            task_id=task_id,
            state=self.state,
            checkpoint=self.checkpoint,
            retry_generation=0,
            retry_count=0,
            retry_due_at=None,
            settlement_revision=0,
            terminal_history_id=None,
            last_error=None,
            steps=tuple(self.steps.values()),
        )

    def prepare_step(self, *, task_id, lease_token, intent):
        """幂等保存准备态步骤。"""
        del lease_token
        existing = self.steps.get(intent.operation_id)
        if existing is not None:
            return existing
        step = TransferExecutionStep(
            task_id=task_id,
            operation_id=intent.operation_id,
            checkpoint_fingerprint=intent.checkpoint_fingerprint,
            ordinal=intent.ordinal,
            phase=intent.phase,
            kind=intent.kind,
            state=TransferStepState.PREPARED,
            attempt_token=None,
            attempt_count=0,
            intent=intent,
            result=None,
            last_error=None,
            prepared_at="2026-08-27 10:00:00",
            started_at=None,
            completed_at=None,
            updated_at="2026-08-27 10:00:00",
        )
        self.steps[intent.operation_id] = step
        return step

    def start_step(
            self,
            *,
            task_id,
            lease_token,
            operation_id,
            attempt_token,
    ):
        """把准备态步骤推进到已开始。"""
        del task_id, lease_token
        step = replace(
            self.steps[operation_id],
            state=TransferStepState.STARTED,
            attempt_token=attempt_token,
            attempt_count=1,
            started_at="2026-08-27 10:00:01",
        )
        self.steps[operation_id] = step
        self.state = TransferExecutionState.RUNNING
        return step

    def complete_step(
            self,
            *,
            task_id,
            lease_token,
            operation_id,
            attempt_token,
            result,
    ):
        """以当前 attempt 提交成功证据。"""
        del task_id, lease_token
        assert self.steps[operation_id].attempt_token == attempt_token
        step = replace(
            self.steps[operation_id],
            state=TransferStepState.SUCCEEDED,
            result=result,
            completed_at="2026-08-27 10:00:02",
        )
        self.steps[operation_id] = step
        return step

    def checkpoint_execution(self, *, task_id, lease_token, checkpoint):
        """保存可重放终态的聚合检查点。"""
        del lease_token
        self.state = TransferExecutionState.SETTLING
        self.checkpoint = checkpoint
        return self.get_snapshot(task_id=task_id)

    def mark_manual_review(
            self,
            *,
            task_id,
            lease_token,
            operation_id,
            attempt_token,
            error,
            evidence,
    ):
        """把执行结果不确定的步骤隔离到人工复核态。"""
        del lease_token
        step = self.steps[operation_id]
        assert step.attempt_token == attempt_token
        self.steps[operation_id] = replace(
            step,
            state=TransferStepState.MANUAL_REVIEW,
            result=evidence,
            last_error=error,
        )
        self.state = TransferExecutionState.MANUAL_REVIEW
        return self.get_snapshot(task_id=task_id)


def _chain(*, repository=None, checkpoint=None, result=None) -> TransferChain:
    """构造只保留规划编排依赖的 TransferChain 骨架。"""
    chain = object.__new__(TransferChain)
    chain._transfer_admissions = repository or Mock()
    chain._transfer_executions = _ExecutionRepositoryStub()
    chain.durable_event_writer = Mock()
    chain._worker_owner_id = "planning-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain._closing = False
    chain._recovery_wakeup_event = threading.Event()
    chain._TransferChain__ensure_lease_heartbeat_owner = Mock()

    def claim_task(**kwargs):
        """为规划测试返回与进程 owner 匹配的稳定 claim。"""
        return transfer_application.TransferAdmission(
            task_id=kwargs["task_id"],
            storage="local",
            src_path="/downloads/Movie.2026.mkv",
            state="accepted",
            created_at="2026-08-27 10:00:00",
            updated_at="2026-08-27 10:00:00",
            planning_input=_planning_input(),
            lease_owner=kwargs["owner_id"],
            lease_token=f"lease-{kwargs['task_id']}",
            lease_expires_at="2026-08-27 10:02:00.000000",
            heartbeat_at="2026-08-27 10:00:00.000000",
            attempt_count=1,
        )

    chain._transfer_admissions.claim_task.side_effect = claim_task
    chain._module_dispatcher = Mock()
    chain._module_dispatcher.freeze_plugin_providers.return_value = ()
    chain.eventmanager = Mock()
    chain.eventmanager.send_event.return_value = None
    chain.plan_transfer = Mock(return_value=checkpoint or _checkpoint())
    chain.execute_transfer_plan = Mock(
        return_value=result or TransferInfo(
            success=True,
            fileitem=_task().fileitem,
            transfer_type="copy",
        )
    )

    def run_module(method, *args, **kwargs):
        """让规划测试沿正式模块入口调用其可观察的宿主执行替身。"""
        assert method == "execute_transfer_plan"
        checkpoint_arg = kwargs.pop("checkpoint")
        return chain.execute_transfer_plan(checkpoint_arg, *args, **kwargs)

    chain.run_module = Mock(side_effect=run_module)
    return chain


def _replay_chain(repository) -> TransferChain:
    """构造绑定固定恢复 owner 且不启动真实 heartbeat 线程的测试链。"""
    chain = object.__new__(TransferChain)
    chain._transfer_admissions = repository
    chain._transfer_executions = Mock()
    chain._transfer_executions.get_snapshot.side_effect = (
        lambda *, task_id: TransferExecutionSnapshot(
            task_id=task_id,
            state=TransferExecutionState.NOT_STARTED,
            checkpoint=None,
            retry_generation=0,
            retry_count=0,
            retry_due_at=None,
            settlement_revision=0,
            terminal_history_id=None,
            last_error=None,
            steps=(),
        )
    )
    chain._worker_owner_id = "replay-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain._closing = False
    chain._recovery_wakeup_event = threading.Event()
    chain._TransferChain__ensure_lease_heartbeat_owner = Mock()
    return chain


def _real_dispatcher(plugins: dict) -> ModuleInvocationDispatcher:
    """构造使用真实冻结解析与执行内核的内存插件调度器。"""
    plugin_catalog = Mock()
    plugin_catalog.get_plugin_modules.return_value = plugins
    return ModuleInvocationDispatcher(
        module_catalog=Mock(),
        plugin_catalog=plugin_catalog,
        plugin_error_handler=Mock(),
        system_error_handler=Mock(),
        rate_limit_handler=Mock(),
    )


def test_non_preview_missing_durable_writer_stops_before_planning_or_execution():
    """缺少原子 writer 时，持久任务取得租约后也不得开始任何外部流程。"""
    task = _task()
    task.bind_admission_task_id("task-missing-writer")
    _bind_planning_input(task, _planning_input())
    chain = _chain()
    chain.durable_event_writer = None

    with pytest.raises(RuntimeError, match="缺少 durable 原子写入端口"):
        chain._plan_checkpoint_and_execute(task)

    chain._module_dispatcher.freeze_plugin_providers.assert_not_called()
    chain.plan_transfer.assert_not_called()
    chain.execute_transfer_plan.assert_not_called()


def test_non_preview_missing_execution_repository_stops_before_side_effects():
    """缺少 execution repository 时不得调用 provider 或文件执行器。"""
    task = _task()
    task.bind_admission_task_id("task-missing-execution-repository")
    _bind_planning_input(task, _planning_input())
    _bind_checkpoint(task, _checkpoint())
    chain = _chain()
    chain._transfer_executions = None
    chain._TransferChain__restore_planned_task = Mock()

    with pytest.raises(RuntimeError, match="缺少 execution repository"):
        chain._plan_checkpoint_and_execute(
            task,
            source_oper=object(),
            target_oper=object(),
        )

    chain._module_dispatcher.execute_frozen_plugin_providers.assert_not_called()
    chain.execute_transfer_plan.assert_not_called()


def test_legacy_provider_runs_only_after_checkpoint_commit_and_short_circuits_host():
    """旧插件 provider 必须随计划冻结，并在 CAS 提交后才能接管执行。"""
    calls = []
    task = _task()
    task.meta = MetaBase("Movie.2026.mkv")
    task.mediainfo = MediaInfo(title="Movie")
    task.bind_admission_task_id("task-plugin-short-circuit")
    _bind_planning_input(task, _planning_input())
    checkpoint = _checkpoint()
    provider = SimpleNamespace(
        plugin_id="LegacyTransfer",
        plugin_name="旧整理插件",
        method="transfer",
    )
    plugin_result = TransferInfo(
        success=True,
        fileitem=task.fileitem,
        transfer_type="copy",
    )
    repository = Mock()

    def checkpoint_plan(**kwargs):
        """记录 CAS 顺序并回读包含 provider 路由的持久检查点。"""
        calls.append("checkpoint")
        persisted_checkpoint = kwargs["checkpoint"]
        assert persisted_checkpoint.legacy_transfer_providers[0].plugin_id == (
            "LegacyTransfer"
        )
        return _planned_admission(task, persisted_checkpoint)

    repository.checkpoint_plan.side_effect = checkpoint_plan
    chain = _chain(repository=repository, checkpoint=checkpoint)
    chain._module_dispatcher.freeze_plugin_providers.return_value = (provider,)
    chain._module_dispatcher.execute_frozen_plugin_providers.side_effect = (
        lambda *_args, **_kwargs: calls.append("plugin") or plugin_result
    )

    returned = chain._plan_checkpoint_and_execute(task)

    assert returned == plugin_result
    assert calls == ["checkpoint", "plugin"]
    chain.plan_transfer.assert_not_called()
    chain.execute_transfer_plan.assert_not_called()
    frozen_call = chain._module_dispatcher.execute_frozen_plugin_providers.call_args
    assert frozen_call.args[0] == "transfer"
    assert frozen_call.args[1][0].plugin_id == "LegacyTransfer"
    assert frozen_call.kwargs["target_path"] == Path("/library")
    assert frozen_call.kwargs["preview"] is False


def test_legacy_provider_cleanup_runs_before_provider_without_duplicate_intercept():
    """旧 provider 前只做 cleanup，不重复发送插件自行负责的真实目标拦截事件。"""
    calls = []
    cleanup_item = FileItem(
        storage="plugin-storage",
        path="/old-library/old.mkv",
        name="old.mkv",
        type="file",
        extension="mkv",
    )
    resolved = _resolved_checkpoint()
    planning_input = replace(
        resolved.planning_input,
        options={
            **resolved.planning_input.options,
            "cleanup_dest_fileitem": cleanup_item.model_dump(mode="json"),
        },
    )
    invocation = transfer_application.TransferProviderInvocationSnapshot(
        fileitem=_source_snapshot(),
        meta=resolved.resolved_meta,
        meta_kind=resolved.resolved_meta_kind,
        mediainfo=resolved.resolved_mediainfo,
        mediainfo_kind=resolved.resolved_mediainfo_kind,
        target_directory={
            "library_storage": "local",
            "library_path": "/library",
            "transfer_type": "copy",
        },
        target_storage=None,
        target_path=None,
        transfer_type=None,
        scrape=True,
        library_type_folder=False,
        library_category_folder=False,
        episodes_info=resolved.resolved_episodes_info,
        preview=False,
    )
    provider = transfer_application.TransferProviderReference(
        plugin_id="LegacyTransfer",
        plugin_name="旧整理插件",
    )
    checkpoint = transfer_application.TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="",
        root_target_path="",
        final_target_path="",
        resolved_transfer_type="",
        items=(),
        legacy_transfer_providers=(provider,),
        provider_invocation=invocation,
        need_notify=True,
    )
    task = _task()
    task.meta = MetaBase("Drifted.mkv")
    task.mediainfo = MediaInfo(title="Drifted")
    chain = object.__new__(TransferChain)
    chain.eventmanager = Mock()
    storage_chain = Mock()
    storage_chain.get_file_item_strict.side_effect = (
        lambda **_kwargs: calls.append("lookup") or cleanup_item
    )
    storage_chain.delete_media_file.side_effect = (
        lambda _fileitem: calls.append("cleanup") or True
    )
    chain._transfer_storage_chain = Mock(return_value=storage_chain)
    provider_result = TransferInfo(
        success=True,
        fileitem=task.fileitem,
        transfer_type="copy",
    )

    def execute_provider(*_args, **kwargs):
        """记录 provider 文件副作用入口并核对旧 ABI 的原始 None 语义。"""
        calls.append("provider")
        assert kwargs["target_storage"] is None
        assert kwargs["target_path"] is None
        assert kwargs["transfer_type"] is None
        return provider_result

    chain._module_dispatcher = _real_dispatcher(
        {
            ("LegacyTransfer", "旧整理插件"): {
                "transfer": execute_provider,
            }
        }
    )

    returned = chain._TransferChain__execute_legacy_transfer_providers(
        task,
        checkpoint=checkpoint,
        source_oper=None,
        target_oper=None,
    )

    assert returned is provider_result
    assert calls == ["lookup", "cleanup", "provider"]
    chain.eventmanager.send_event.assert_not_called()


def test_empty_frozen_legacy_providers_fall_back_to_host_checkpoint_executor():
    """provider 空结果在已完成 cleanup 后持久提升宿主计划且只清理一次。"""
    calls = []
    task = _task()
    task.meta = MetaBase("Movie.2026.mkv")
    task.mediainfo = MediaInfo(title="Movie")
    task.bind_admission_task_id("task-plugin-fallback")
    cleanup_item = FileItem(
        storage="local",
        path="/old-library/old.mkv",
        name="old.mkv",
        type="file",
        extension="mkv",
    )
    planning_input = replace(
        _planning_input(),
        options={
            **_planning_input().options,
            "cleanup_dest_fileitem": cleanup_item.model_dump(mode="json"),
        },
    )
    _bind_planning_input(task, planning_input)
    checkpoint = _checkpoint()
    provider = SimpleNamespace(
        plugin_id="EmptyTransfer",
        plugin_name="空整理插件",
        method="transfer",
    )
    repository = Mock()

    def checkpoint_plan(**kwargs):
        """记录宿主 fallback 前的 CAS 提交。"""
        calls.append("checkpoint")
        if not kwargs["checkpoint"].is_provider_pending:
            assert kwargs["checkpoint"].pre_execution_cleanup_completed is True
        return _planned_admission(task, kwargs["checkpoint"])

    repository.checkpoint_plan.side_effect = checkpoint_plan
    chain = _chain(repository=repository, checkpoint=checkpoint)
    chain._module_dispatcher = _real_dispatcher(
        {
            (provider.plugin_id, provider.plugin_name): {
                "transfer": lambda **_kwargs: calls.append("plugin")
            }
        }
    )
    storage_chain = Mock()
    storage_chain.get_file_item_strict.side_effect = (
        lambda **_kwargs: calls.append("lookup") or cleanup_item
    )
    storage_chain.delete_media_file.side_effect = (
        lambda _fileitem: calls.append("cleanup") or True
    )
    chain._transfer_storage_chain = Mock(return_value=storage_chain)
    chain.execute_transfer_plan.side_effect = (
        lambda *_args, **_kwargs: calls.append("host")
        or TransferInfo(
            success=True,
            fileitem=task.fileitem,
            transfer_type="copy",
        )
    )

    returned = chain._plan_checkpoint_and_execute(task)

    assert returned.success is True
    assert calls == [
        "checkpoint",
        "lookup",
        "cleanup",
        "plugin",
        "checkpoint",
        "host",
    ]
    chain.execute_transfer_plan.assert_called_once()
    executed_checkpoint = chain.execute_transfer_plan.call_args.args[0]
    assert executed_checkpoint.pre_execution_cleanup_completed is True
    storage_chain.get_file_item_strict.assert_called_once()
    storage_chain.delete_media_file.assert_called_once_with(cleanup_item)


def test_missing_frozen_provider_keeps_pending_and_skips_cleanup() -> None:
    """提交后任一冻结引用缺失时应保留 pending 且不得提前 cleanup。"""
    calls = []
    task = _task()
    task.meta = MetaBase("Movie.2026.mkv")
    task.mediainfo = MediaInfo(title="Movie")
    task.bind_admission_task_id("task-provider-missing")
    cleanup_item = FileItem(
        storage="local",
        path="/old-library/old.mkv",
        name="old.mkv",
        type="file",
        extension="mkv",
    )
    planning_input = replace(
        _planning_input(),
        options={
            **_planning_input().options,
            "cleanup_dest_fileitem": cleanup_item.model_dump(mode="json"),
        },
    )
    _bind_planning_input(task, planning_input)
    first_provider = Mock(return_value=None)
    plugins = {
        ("ProviderOne", "插件一"): {"transfer": first_provider},
        ("ProviderTwo", "插件二"): {"transfer": Mock(return_value=None)},
    }
    repository = Mock()

    def checkpoint_plan(**kwargs):
        """模拟提交后第二个 provider 被卸载的崩溃恢复窗口。"""
        calls.append("checkpoint")
        plugins.pop(("ProviderTwo", "插件二"))
        return _planned_admission(task, kwargs["checkpoint"])

    repository.checkpoint_plan.side_effect = checkpoint_plan
    chain = _chain(repository=repository)
    chain._module_dispatcher = _real_dispatcher(plugins)
    storage_chain = Mock()
    chain._transfer_storage_chain = Mock(return_value=storage_chain)

    with pytest.raises(
        RuntimeError,
        match=r"禁止自动重放.*ProviderTwo/插件二\.transfer",
    ):
        chain._plan_checkpoint_and_execute(task)

    assert calls == ["checkpoint"]
    assert task.plan_checkpoint is not None
    assert task.plan_checkpoint.is_provider_pending is True
    repository.checkpoint_plan.assert_called_once()
    repository.record_planning_failure.assert_not_called()
    storage_chain.get_file_item_strict.assert_not_called()
    storage_chain.delete_media_file.assert_not_called()
    first_provider.assert_not_called()
    chain.plan_transfer.assert_not_called()
    chain.execute_transfer_plan.assert_not_called()


def test_frozen_provider_failure_keeps_pending_without_host_fallback() -> None:
    """真实冻结调度器必须传播 provider 异常并保留可重放检查点。"""
    calls = []
    task = _task()
    task.meta = MetaBase("Movie.2026.mkv")
    task.mediainfo = MediaInfo(title="Movie")
    task.bind_admission_task_id("task-provider-failed")
    cleanup_item = FileItem(
        storage="local",
        path="/old-library/old.mkv",
        name="old.mkv",
        type="file",
        extension="mkv",
    )
    planning_input = replace(
        _planning_input(),
        options={
            **_planning_input().options,
            "cleanup_dest_fileitem": cleanup_item.model_dump(mode="json"),
        },
    )
    _bind_planning_input(task, planning_input)
    repository = Mock()

    def checkpoint_plan(**kwargs):
        """记录 provider_pending 已在插件副作用前完成提交。"""
        calls.append("checkpoint")
        return _planned_admission(task, kwargs["checkpoint"])

    def failed_provider(**_kwargs):
        """模拟冻结旧插件在 cleanup 完成后的执行失败。"""
        calls.append("provider")
        raise RuntimeError("legacy provider failed")

    repository.checkpoint_plan.side_effect = checkpoint_plan
    chain = _chain(repository=repository)
    chain._module_dispatcher = _real_dispatcher(
        {
            ("FailedProvider", "失败插件"): {
                "transfer": failed_provider,
            }
        }
    )
    storage_chain = Mock()
    storage_chain.get_file_item_strict.side_effect = (
        lambda **_kwargs: calls.append("lookup") or cleanup_item
    )
    storage_chain.delete_media_file.side_effect = (
        lambda _fileitem: calls.append("cleanup") or True
    )
    chain._transfer_storage_chain = Mock(return_value=storage_chain)

    with pytest.raises(RuntimeError, match="legacy provider failed"):
        chain._plan_checkpoint_and_execute(task)

    assert calls == ["checkpoint", "lookup", "cleanup", "provider"]
    assert task.plan_checkpoint is not None
    assert task.plan_checkpoint.is_provider_pending is True
    repository.checkpoint_plan.assert_called_once()
    repository.record_planning_failure.assert_not_called()
    chain.plan_transfer.assert_not_called()
    chain.execute_transfer_plan.assert_not_called()


def test_auto_directory_provider_empty_preserves_legacy_none_values_for_fallback():
    """自动目录 provider 与空结果后的宿主规划都必须保留旧 ABI 的可空原值。"""
    calls = []
    task = _task()
    task.meta = MetaBase("Movie.2026.mkv")
    task.mediainfo = MediaInfo(title="Movie")
    task.target_directory = TransferDirectoryConf(
        name="auto-library",
        transfer_type="copy",
        library_path="/library/Movies",
        library_storage="alist",
        renaming=True,
        scraping=True,
        notify=True,
    )
    task.target_storage = "alist"
    task.target_path = None
    task.transfer_type = None
    task.scrape = None
    task.library_type_folder = False
    task.library_category_folder = None
    task.bind_admission_task_id("task-auto-provider-fallback")
    planning_input = replace(
        _planning_input(),
        target_directory=None,
        target_storage=None,
        target_path=None,
        requested_transfer_type=None,
    )
    _bind_planning_input(task, planning_input)
    host_checkpoint = replace(_checkpoint(), planning_input=planning_input)
    provider = SimpleNamespace(
        plugin_id="AutoDirectoryProvider",
        plugin_name="自动目录插件",
        method="transfer",
    )
    repository = Mock()

    def checkpoint_plan(**kwargs):
        """回读每个阶段检查点并记录 provider_pending 到 planned 的顺序。"""
        checkpoint = kwargs["checkpoint"]
        calls.append(
            "commit-provider" if checkpoint.is_provider_pending else "commit-host"
        )
        return _planned_admission(task, checkpoint)

    def execute_provider(*_args, **kwargs):
        """核对自动目录只冻结解析目录，不改写旧 ABI 的可空参数。"""
        calls.append("provider")
        assert kwargs["target_directory"].name == "auto-library"
        assert kwargs["target_storage"] == "alist"
        assert kwargs["target_path"] is None
        assert kwargs["transfer_type"] is None
        assert kwargs["scrape"] is None
        assert kwargs["library_type_folder"] is False
        assert kwargs["library_category_folder"] is None
        return None

    def plan_host(**kwargs):
        """核对 provider 重放恢复未用空宿主字段污染 fallback 参数。"""
        calls.append("plan-host")
        assert kwargs["target_directory"].name == "auto-library"
        assert kwargs["target_storage"] == "alist"
        assert kwargs["target_path"] is None
        assert kwargs["transfer_type"] is None
        assert kwargs["scrape"] is None
        assert kwargs["library_type_folder"] is False
        assert kwargs["library_category_folder"] is None
        return host_checkpoint

    repository.checkpoint_plan.side_effect = checkpoint_plan
    chain = _chain(repository=repository, checkpoint=host_checkpoint)
    chain._module_dispatcher.freeze_plugin_providers.return_value = (provider,)
    chain._module_dispatcher.execute_frozen_plugin_providers.side_effect = (
        execute_provider
    )
    chain.plan_transfer.side_effect = plan_host
    chain.execute_transfer_plan.side_effect = (
        lambda *_args, **_kwargs: calls.append("execute-host")
        or TransferInfo(
            success=True,
            fileitem=task.fileitem,
            transfer_type="copy",
        )
    )

    returned = chain._plan_checkpoint_and_execute(task)

    assert returned.success is True
    assert calls == [
        "commit-provider",
        "provider",
        "plan-host",
        "commit-host",
        "execute-host",
    ]
    promoted = repository.checkpoint_plan.call_args_list[1].kwargs["checkpoint"]
    assert promoted.pre_execution_cleanup_completed is True


def test_provider_empty_fallback_cas_failure_blocks_host_execution():
    """provider 空结果后的 host checkpoint 升级失败时不得执行宿主副作用。"""
    task = _task()
    task.meta = MetaBase("Movie.2026.mkv")
    task.mediainfo = MediaInfo(title="Movie")
    task.bind_admission_task_id("task-provider-cas-failure")
    planning_input = _planning_input()
    _bind_planning_input(task, planning_input)
    host_checkpoint = replace(_checkpoint(), planning_input=planning_input)
    provider = SimpleNamespace(
        plugin_id="EmptyProvider",
        plugin_name="空结果插件",
        method="transfer",
    )
    repository = Mock()
    checkpoint_calls = []

    def checkpoint_plan(**kwargs):
        """只允许 provider_pending 提交，模拟 host promotion CAS 竞争失败。"""
        checkpoint_calls.append(kwargs["checkpoint"])
        if len(checkpoint_calls) == 1:
            return _planned_admission(task, kwargs["checkpoint"])
        raise transfer_application.TransferPlanningStateError("CAS failed")

    repository.checkpoint_plan.side_effect = checkpoint_plan
    chain = _chain(repository=repository, checkpoint=host_checkpoint)
    chain._module_dispatcher.freeze_plugin_providers.return_value = (provider,)
    chain._module_dispatcher.execute_frozen_plugin_providers.return_value = None

    with pytest.raises(
        transfer_application.TransferPlanningStateError,
        match="CAS failed",
    ):
        chain._plan_checkpoint_and_execute(task)

    assert checkpoint_calls[0].is_provider_pending is True
    assert checkpoint_calls[1].is_provider_pending is False
    chain.plan_transfer.assert_called_once()
    chain.execute_transfer_plan.assert_not_called()
    repository.record_planning_failure.assert_called_once_with(
        task_id="task-provider-cas-failure",
        lease_token="lease-task-provider-cas-failure",
        error="CAS failed",
    )


def test_provider_pending_crash_replay_executes_snapshot_without_host_planning():
    """provider 提交后崩溃重放必须直接执行冻结调用，不重新冻结或宿主规划。"""
    planning_input = _planning_input()
    provider = SimpleNamespace(
        plugin_id="CrashProvider",
        plugin_name="崩溃恢复插件",
        method="transfer",
    )
    first_task = _task()
    first_task.meta = MetaBase("Movie.2026.mkv")
    first_task.mediainfo = MediaInfo(title="Movie")
    first_task.bind_admission_task_id("task-provider-replay")
    _bind_planning_input(first_task, planning_input)
    first_repository = Mock()
    first_repository.checkpoint_plan.side_effect = lambda **kwargs: (
        _planned_admission(first_task, kwargs["checkpoint"])
    )
    first_chain = _chain(repository=first_repository)
    first_chain._module_dispatcher.freeze_plugin_providers.return_value = (provider,)
    first_chain._module_dispatcher.execute_frozen_plugin_providers.side_effect = (
        RuntimeError("process crashed")
    )

    with pytest.raises(RuntimeError, match="process crashed"):
        first_chain._plan_checkpoint_and_execute(first_task)

    provider_checkpoint = first_task.plan_checkpoint
    assert provider_checkpoint is not None
    assert provider_checkpoint.is_provider_pending is True
    first_chain.plan_transfer.assert_not_called()

    recovered_task = _task()
    recovered_task.meta = MetaBase("Drifted.mkv")
    recovered_task.mediainfo = MediaInfo(title="Drifted")
    recovered_task.bind_admission_task_id("task-provider-replay")
    _bind_planning_input(recovered_task, planning_input)
    _bind_checkpoint(recovered_task, provider_checkpoint)
    recovered_result = TransferInfo(
        success=True,
        fileitem=recovered_task.fileitem,
        transfer_type="copy",
    )
    recovered_chain = _chain(repository=Mock())
    recovered_chain._module_dispatcher.execute_frozen_plugin_providers.return_value = (
        recovered_result
    )

    returned = recovered_chain._plan_checkpoint_and_execute(recovered_task)

    assert returned == recovered_result
    recovered_chain._module_dispatcher.freeze_plugin_providers.assert_not_called()
    recovered_chain.plan_transfer.assert_not_called()
    recovered_chain._transfer_admissions.checkpoint_plan.assert_not_called()
    provider_call = (
        recovered_chain._module_dispatcher.execute_frozen_plugin_providers.call_args
    )
    assert provider_call.kwargs["meta"].org_string == "Movie.2026.mkv"
    assert provider_call.kwargs["mediainfo"].title == "Movie"


def test_legacy_transfer_command_uses_durable_pipeline_and_settles_pending():
    """旧同步调用必须经过 admission、checkpoint、执行和原子终态结算。"""
    calls = []
    repository = Mock()
    repository.admit.side_effect = (
        lambda **_kwargs: calls.append("admit")
        or SimpleNamespace(task_id="task-legacy-command")
    )

    def checkpoint_plan(**kwargs):
        """回读刚提交的检查点并记录顺序。"""
        calls.append("checkpoint")
        return SimpleNamespace(checkpoint=kwargs["checkpoint"])

    repository.checkpoint_plan.side_effect = checkpoint_plan
    result = TransferInfo(
        success=True,
        fileitem=_task().fileitem,
        transfer_type="copy",
    )
    chain = _chain(repository=repository, checkpoint=_checkpoint(), result=result)
    execution_checkpoint = TransferExecutionCheckpoint.create(
        payload={
            "outcome": "succeeded",
            "transferinfo": result.model_dump(mode="json"),
        },
        operation_ids=("operation-legacy-command",),
    )
    step_runner = Mock()
    step_runner.checkpoint.return_value = execution_checkpoint
    chain._TransferChain__build_durable_step_runner = Mock(return_value=step_runner)
    chain.run_module = Mock(
        side_effect=lambda *_args, **_kwargs: calls.append("execute") or result
    )
    chain.durable_event_writer = Mock()

    def settle_result(**kwargs):
        """执行历史暂存并模拟 writer 返回 task-aware 结算投影。"""
        staging = Mock()
        staging.add_force.return_value = SimpleNamespace(
            id=31,
            status=True,
            src=result.fileitem.path,
            src_storage=result.fileitem.storage,
            src_fileitem=result.fileitem.model_dump(mode="json"),
        )
        history = kwargs["stage_history"](staging)
        assert history.status is True
        calls.append("settle")
        return TransferSettlementResult(
            history_id=history.id,
            settlement_revision=1,
            pending_deleted=True,
        )

    chain.durable_event_writer.transfer_result.side_effect = settle_result
    meta = MetaBase("Movie.2026.mkv")
    mediainfo = MediaInfo()

    returned = chain.execute_legacy_transfer_command(
        fileitem=_task().fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_storage="local",
        target_path=Path("/library"),
        transfer_type="copy",
    )

    assert returned is result
    assert calls == ["admit", "checkpoint", "execute", "settle"]
    repository.abandon_unstarted.assert_not_called()
    writer_call = chain.durable_event_writer.transfer_result.call_args.kwargs
    assert writer_call["topic"] is None
    assert writer_call["publish"] is None
    assert writer_call["settlement"].task_id == "task-legacy-command"
    assert writer_call["settlement"].outcome == "succeeded"


def test_cleanup_destination_is_idempotent_and_uses_storage_safety_policy():
    """cleanup 不存在时成功，存在时必须委托 StorageChain 安全删除。"""
    cleanup_item = FileItem(**_source_snapshot())
    chain = object.__new__(TransferChain)
    storage_chain = Mock()
    chain._transfer_storage_chain = Mock(return_value=storage_chain)
    storage_chain.get_file_item_strict.return_value = None

    assert chain._TransferChain__cleanup_transfer_destination(cleanup_item) is True
    storage_chain.delete_media_file.assert_not_called()

    current_item = cleanup_item.model_copy(update={"fileid": "current"})
    storage_chain.get_file_item_strict.return_value = current_item
    storage_chain.delete_media_file.return_value = True

    assert chain._TransferChain__cleanup_transfer_destination(cleanup_item) is True
    storage_chain.delete_media_file.assert_called_once_with(current_item)


def test_cleanup_destination_propagates_strict_lookup_failure() -> None:
    """cleanup 查询失败必须保留 planned，不能被投影成目标不存在。"""
    cleanup_item = FileItem(**_source_snapshot())
    chain = object.__new__(TransferChain)
    storage_chain = Mock()
    chain._transfer_storage_chain = Mock(return_value=storage_chain)
    storage_chain.get_file_item_strict.side_effect = StorageQueryError(
        "provider lookup failed"
    )

    with pytest.raises(StorageQueryError, match="provider lookup failed"):
        chain._TransferChain__cleanup_transfer_destination(cleanup_item)

    storage_chain.delete_media_file.assert_not_called()


def test_planning_payload_round_trip_is_self_contained_and_stable():
    """planned 行仅凭 checkpoint payload 即可还原输入、目标与有序动作。"""
    TransferPlanningInput, _TransferPlanItem, TransferPlanCheckpoint = (
        _planning_contracts()
    )
    checkpoint = _checkpoint()

    restored = TransferPlanCheckpoint.from_payload(checkpoint.to_payload())

    assert restored == checkpoint
    assert isinstance(restored.planning_input, TransferPlanningInput)
    assert restored.planning_input.source_fileitem == _source_snapshot()
    assert restored.planning_input.fingerprint == checkpoint.planning_input.fingerprint
    assert tuple(item.sequence for item in restored.items) == (0,)
    assert restored.final_target_path == "/library/Movie (2026)/Movie.mkv"


def test_repository_rejects_checkpoint_with_mismatched_planning_fingerprint(tmp_path):
    """checkpoint 内嵌输入与 accepted 指纹不一致时必须拒绝状态跃迁。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'fingerprint.db'}")
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    repository = TransactionalTransferAdmissionRepository(sessionmaker(bind=engine))
    accepted_input = _planning_input(target_path="/library/A")
    admission = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=accepted_input,
    )
    claimed = repository.claim_task(
        task_id=admission.task_id,
        owner_id="fingerprint-test",
        lease_seconds=120,
    )
    assert claimed is not None
    assert claimed.lease_token is not None
    mismatched = _checkpoint(target_path="/library/B/Movie.mkv")

    with pytest.raises(ValueError, match="指纹|fingerprint|规划输入"):
        repository.checkpoint_plan(
            task_id=admission.task_id,
            lease_token=claimed.lease_token,
            input_fingerprint=accepted_input.fingerprint,
            checkpoint=mismatched,
        )

    assert repository.release_claim(
        task_id=admission.task_id,
        lease_token=claimed.lease_token,
    ) is True
    recovered = repository.claim_recoverable(
        owner_id="fingerprint-recovery",
        limit=10,
        lease_seconds=120,
    )
    assert len(recovered) == 1
    assert recovered[0].state == "accepted"
    assert recovered[0].checkpoint is None
    engine.dispose()


def test_repository_round_trips_accepted_and_planned_recovery_states(tmp_path):
    """仓储必须同时恢复 accepted 输入和 planned 自包含 checkpoint。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'recoverable.db'}")
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    repository = TransactionalTransferAdmissionRepository(sessionmaker(bind=engine))
    planning_input = _planning_input()
    admission = repository.admit(
        storage="local",
        src_path="/downloads/Movie.2026.mkv",
        planning_input=planning_input,
    )

    claimed = repository.claim_task(
        task_id=admission.task_id,
        owner_id="roundtrip-owner",
        lease_seconds=120,
    )
    assert claimed is not None
    assert claimed.lease_token is not None
    assert claimed.state == "accepted"
    assert claimed.planning_input == planning_input
    assert claimed.checkpoint is None

    repository.record_planning_failure(
        task_id=admission.task_id,
        lease_token=claimed.lease_token,
        error="rename unavailable",
    )
    with sessionmaker(bind=engine)() as session:
        retryable = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == admission.task_id
            )
        ).scalar_one()
        assert retryable.state == "accepted"
        assert retryable.last_error == "rename unavailable"

    checkpoint = _checkpoint()
    planned = repository.checkpoint_plan(
        task_id=admission.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=checkpoint,
    )

    assert planned.state == "planned"
    assert planned.checkpoint == checkpoint
    assert planned.last_error is None
    assert repository.release_claim(
        task_id=admission.task_id,
        lease_token=claimed.lease_token,
    ) is True
    recovered = repository.claim_recoverable(
        owner_id="roundtrip-recovery",
        limit=10,
        lease_seconds=120,
    )
    assert len(recovered) == 1
    assert recovered[0].state == "planned"
    assert recovered[0].checkpoint == checkpoint
    engine.dispose()


def test_checkpoint_commit_precedes_executor_and_sync_task_is_admitted():
    """同步非 preview 任务也必须按 admit、plan、commit、execute 顺序运行。"""
    order = []
    repository = Mock()
    task = _task()
    planning_input = _planning_input()
    _bind_planning_input(task, planning_input)
    checkpoint = _checkpoint()
    result = TransferInfo(success=True, fileitem=task.fileitem, transfer_type="copy")

    def admit(**_kwargs):
        """记录同步任务首次持久准入。"""
        order.append("admit")
        return SimpleNamespace(
            task_id="task-sync",
            state="accepted",
            planning_input=planning_input,
            checkpoint=None,
        )

    def checkpoint_plan(**_kwargs):
        """记录 durable checkpoint 已经提交并返回 planned 投影。"""
        order.append("commit-checkpoint")
        return _planned_admission(task, checkpoint)

    repository.admit.side_effect = admit
    repository.checkpoint_plan.side_effect = checkpoint_plan
    chain = _chain(repository=repository, checkpoint=checkpoint, result=result)
    claim_task = repository.claim_task.side_effect
    repository.claim_task.side_effect = lambda **kwargs: (
        order.append("claim") or claim_task(**kwargs)
    )
    chain.plan_transfer.side_effect = lambda *_args, **_kwargs: (
        order.append("plan") or checkpoint
    )
    chain.execute_transfer_plan.side_effect = lambda *args, **_kwargs: (
        order.append("execute") or result
    )

    returned = chain._plan_checkpoint_and_execute(task)

    assert returned is result
    assert order == ["admit", "claim", "plan", "commit-checkpoint", "execute"]
    assert task.admission_task_id == "task-sync"
    repository.admit.assert_called_once()
    assert repository.admit.call_args.kwargs["planning_input"] is planning_input
    repository.checkpoint_plan.assert_called_once_with(
        task_id="task-sync",
        lease_token="lease-task-sync",
        input_fingerprint=planning_input.fingerprint,
        checkpoint=checkpoint,
    )
    chain.execute_transfer_plan.assert_called_once()


def test_checkpoint_commit_failure_blocks_executor_and_keeps_accepted():
    """checkpoint 提交失败时不得产生文件副作用，错误留在 accepted 记录。"""
    repository = Mock()
    task = _task()
    planning_input = _planning_input()
    checkpoint = _checkpoint()
    task.bind_admission_task_id("task-accepted")
    _bind_planning_input(task, planning_input)
    repository.checkpoint_plan.side_effect = RuntimeError("commit failed")
    chain = _chain(repository=repository, checkpoint=checkpoint)

    with pytest.raises(RuntimeError, match="commit failed"):
        chain._plan_checkpoint_and_execute(task)

    chain.plan_transfer.assert_called_once()
    chain.execute_transfer_plan.assert_not_called()
    repository.record_planning_failure.assert_called_once()
    assert repository.record_planning_failure.call_args.kwargs["task_id"] == (
        "task-accepted"
    )
    assert "commit failed" in repository.record_planning_failure.call_args.kwargs["error"]


def test_post_commit_crash_replays_frozen_plan_without_replanning():
    """commit 后执行崩溃仍保留 planned checkpoint，重启后跳过 rename 规划。"""
    repository = Mock()
    planning_input = _planning_input()
    checkpoint = _checkpoint()
    first_task = _task()
    first_task.bind_admission_task_id("task-planned")
    _bind_planning_input(first_task, planning_input)
    repository.checkpoint_plan.side_effect = lambda **_kwargs: _planned_admission(
        first_task, checkpoint
    )
    first_chain = _chain(repository=repository, checkpoint=checkpoint)
    first_chain.execute_transfer_plan.side_effect = RuntimeError("process crashed")

    with pytest.raises(RuntimeError, match="process crashed"):
        first_chain._plan_checkpoint_and_execute(first_task)

    assert first_task.plan_checkpoint == checkpoint
    repository.record_planning_failure.assert_not_called()

    recovered_task = _task()
    recovered_task.bind_admission_task_id("task-planned")
    _bind_planning_input(recovered_task, planning_input)
    _bind_checkpoint(recovered_task, checkpoint)
    recovered_chain = _chain(repository=Mock(), checkpoint=checkpoint)

    recovered_chain._plan_checkpoint_and_execute(recovered_task)

    recovered_chain.plan_transfer.assert_not_called()
    recovered_chain._transfer_admissions.checkpoint_plan.assert_not_called()
    recovered_chain.execute_transfer_plan.assert_called_once()
    execute_call = recovered_chain.execute_transfer_plan.call_args
    executed_checkpoint = (
        execute_call.args[0]
        if execute_call.args
        else execute_call.kwargs["checkpoint"]
    )
    assert executed_checkpoint is checkpoint


def test_first_execution_and_planned_replay_consume_identical_persisted_context():
    """首次提交后执行与 planned 重放必须消费同一份持久领域上下文。"""
    _TransferPlanningInput, _TransferPlanItem, TransferPlanCheckpoint = (
        _planning_contracts()
    )
    planner_checkpoint = _resolved_checkpoint()
    persisted_checkpoint = TransferPlanCheckpoint.from_payload(
        planner_checkpoint.to_payload()
    )
    planning_input = planner_checkpoint.planning_input

    first_task = _task()
    first_task.meta = MetaBase("Drifted.Before.Commit.mkv")
    first_task.bind_admission_task_id("task-first-context")
    _bind_planning_input(first_task, planning_input)
    first_repository = Mock()
    first_repository.checkpoint_plan.return_value = SimpleNamespace(
        task_id="task-first-context",
        state="planned",
        planning_input=planning_input,
        checkpoint=persisted_checkpoint,
    )
    first_chain = _chain(
        repository=first_repository,
        checkpoint=planner_checkpoint,
    )

    first_chain._plan_checkpoint_and_execute(first_task)

    replay_task = _task()
    replay_task.bind_admission_task_id("task-first-context")
    _bind_planning_input(replay_task, planning_input)
    _bind_checkpoint(replay_task, persisted_checkpoint)
    replay_chain = _chain(repository=Mock(), checkpoint=persisted_checkpoint)

    replay_chain._plan_checkpoint_and_execute(replay_task)

    first_call = first_chain.execute_transfer_plan.call_args
    replay_call = replay_chain.execute_transfer_plan.call_args
    first_meta = first_call.kwargs["meta"]
    replay_meta = replay_call.kwargs["meta"]
    first_media = first_call.kwargs["mediainfo"]
    replay_media = replay_call.kwargs["mediainfo"]
    assert first_task.plan_checkpoint is persisted_checkpoint
    assert first_meta.to_dict() == replay_meta.to_dict()
    assert first_media.to_dict() == replay_media.to_dict()
    assert [item.model_dump() for item in first_task.episodes_info] == [
        item.model_dump() for item in replay_task.episodes_info
    ]
    assert first_meta.title == "Frozen Show S01E01"
    assert first_media.media_id == "987"


def test_accepted_recovery_replans_but_planned_recovery_does_not():
    """accepted 可重新触发 rename 规划，planned 必须只读冻结计划。"""
    planning_input = _planning_input()
    checkpoint = _checkpoint()

    accepted_task = _task()
    accepted_task.bind_admission_task_id("task-accepted")
    _bind_planning_input(accepted_task, planning_input)
    accepted_repository = Mock()
    accepted_repository.checkpoint_plan.side_effect = lambda **_kwargs: (
        _planned_admission(accepted_task, checkpoint)
    )
    accepted_chain = _chain(
        repository=accepted_repository,
        checkpoint=checkpoint,
    )
    accepted_chain._plan_checkpoint_and_execute(accepted_task)

    accepted_chain.plan_transfer.assert_called_once()
    accepted_chain.execute_transfer_plan.assert_called_once()

    planned_task = _task()
    planned_task.bind_admission_task_id("task-planned")
    _bind_planning_input(planned_task, planning_input)
    _bind_checkpoint(planned_task, checkpoint)
    planned_chain = _chain(repository=Mock(), checkpoint=checkpoint)
    planned_chain._plan_checkpoint_and_execute(planned_task)

    planned_chain.plan_transfer.assert_not_called()
    planned_chain._transfer_admissions.checkpoint_plan.assert_not_called()
    planned_chain.execute_transfer_plan.assert_called_once()


def test_accepted_replay_restores_explicit_context_without_online_lookup(
        tmp_path,
        monkeypatch,
):
    """accepted 重放必须恢复显式 meta/media/episodes，并可离线完成重新规划。"""
    media_path = tmp_path / "Frozen.Show.S01E01.mkv"
    media_path.write_bytes(b"frozen")
    planning_input = replace(
        _resolved_checkpoint().planning_input,
        source_fileitem={
            **_source_snapshot(),
            "path": media_path.as_posix(),
            "name": media_path.name,
            "basename": media_path.stem,
        },
        meta=_resolved_checkpoint().resolved_meta,
        mediainfo=_resolved_checkpoint().resolved_mediainfo,
        target_directory={
            "library_path": "/library",
            "library_storage": "local",
            "renaming": True,
            "notify": True,
        },
        episodes_info=_resolved_checkpoint().resolved_episodes_info,
        options={
            **_resolved_checkpoint().planning_input.options,
            "_meta_kind": "MetaVideo",
            "_mediainfo_kind": "MediaInfo",
        },
    )
    admission = transfer_application.TransferAdmission(
        task_id="task-accepted-offline",
        storage="local",
        src_path=media_path.as_posix(),
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        input_fingerprint=planning_input.fingerprint,
        planning_input=planning_input,
        lease_owner="replay-owner",
        lease_token="lease-task-accepted-offline",
        lease_expires_at="2026-08-27 10:02:00.000000",
        heartbeat_at="2026-08-27 10:00:00.000000",
        attempt_count=1,
    )
    replay_repository = Mock()
    replay_repository.claim_recoverable.return_value = [admission]
    replay_chain = _replay_chain(replay_repository)
    replay_chain._execute_transfer = Mock()
    queued_tasks = []
    replay_chain.put_to_queue = Mock(
        side_effect=lambda task: queued_tasks.append(task) or True
    )

    replay_chain._TransferChain__replay_pending()

    assert len(queued_tasks) == 1
    restored_task = queued_tasks[0]
    assert restored_task.admission_task_id == "task-accepted-offline"
    assert restored_task.planning_input is planning_input
    assert restored_task.planning_context_restored is True
    assert restored_task.meta.title == "Frozen Show S01E01"
    assert restored_task.mediainfo.media_id == "987"
    assert restored_task.episodes_info[0].name == "Frozen Episode"
    replay_chain._execute_transfer.assert_not_called()

    checkpoint = replace(
        _resolved_checkpoint(),
        planning_input=planning_input,
    )
    execution_repository = Mock()
    execution_repository.checkpoint_plan.return_value = SimpleNamespace(
        task_id=admission.task_id,
        state="planned",
        planning_input=planning_input,
        checkpoint=checkpoint,
    )
    execution_chain = _chain(
        repository=execution_repository,
        checkpoint=checkpoint,
    )
    execution_chain._worker_owner_id = "replay-owner"
    execution_chain._owned_leases = {
        admission.task_id: (str(admission.lease_token), float("inf"))
    }
    execution_chain.jobview = Mock()
    execution_chain.eventmanager = Mock()
    execution_chain.eventmanager.send_event.return_value = None
    execution_chain.runtime_config = SimpleNamespace(scrape_follow_tmdb=True)
    execution_chain._TransferChain__finish_scrape_batch_task = Mock()
    monkeypatch.setattr(
        "app.chain.transfer.get_chain_transfer_history_port",
        lambda: Mock(),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MediaChain",
        Mock(side_effect=AssertionError("accepted replay 不应重新在线识别")),
    )
    monkeypatch.setattr(
        "app.chain.transfer.TmdbChain",
        Mock(side_effect=AssertionError("accepted replay 不应重新在线获取剧集")),
    )

    result = execution_chain._TransferChain__handle_transfer(restored_task)

    assert result[0] is True
    execution_chain.plan_transfer.assert_called_once()
    execution_chain.execute_transfer_plan.assert_called_once()


def test_accepted_replay_with_explicit_empty_episodes_stays_offline(
        tmp_path,
        monkeypatch,
):
    """显式媒体快照即使没有剧集明细，也不得在 accepted 重放时在线补查。"""
    media_path = tmp_path / "Frozen.Show.S01.mkv"
    media_path.write_bytes(b"frozen")
    resolved = _resolved_checkpoint()
    planning_input = replace(
        resolved.planning_input,
        source_fileitem={
            **_source_snapshot(),
            "path": media_path.as_posix(),
            "name": media_path.name,
            "basename": media_path.stem,
        },
        meta=resolved.resolved_meta,
        mediainfo=resolved.resolved_mediainfo,
        target_directory={
            "library_path": "/library",
            "library_storage": "local",
        },
        episodes_info=(),
        options={
            **resolved.planning_input.options,
            "_meta_kind": "MetaVideo",
            "_mediainfo_kind": "MediaInfo",
        },
    )
    admission = transfer_application.TransferAdmission(
        task_id="task-accepted-empty-episodes",
        storage="local",
        src_path=media_path.as_posix(),
        state="accepted",
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        planning_input=planning_input,
        lease_owner="replay-owner",
        lease_token="lease-task-accepted-empty-episodes",
        lease_expires_at="2026-08-27 10:02:00.000000",
        heartbeat_at="2026-08-27 10:00:00.000000",
        attempt_count=1,
    )
    replay_repository = Mock()
    replay_repository.claim_recoverable.return_value = [admission]
    replay_chain = _replay_chain(replay_repository)
    queued_tasks = []
    replay_chain.put_to_queue = Mock(
        side_effect=lambda task: queued_tasks.append(task) or True
    )

    replay_chain._TransferChain__replay_pending()

    restored_task = queued_tasks[0]
    checkpoint = replace(
        resolved,
        planning_input=planning_input,
        resolved_episodes_info=(),
    )
    execution_repository = Mock()
    execution_repository.checkpoint_plan.return_value = SimpleNamespace(
        task_id=admission.task_id,
        checkpoint=checkpoint,
    )
    execution_chain = _chain(
        repository=execution_repository,
        checkpoint=checkpoint,
    )
    execution_chain._worker_owner_id = "replay-owner"
    execution_chain._owned_leases = {
        admission.task_id: (str(admission.lease_token), float("inf"))
    }
    execution_chain.jobview = Mock()
    execution_chain.eventmanager = Mock()
    execution_chain.eventmanager.send_event.return_value = None
    execution_chain.runtime_config = SimpleNamespace(scrape_follow_tmdb=True)
    execution_chain._TransferChain__finish_scrape_batch_task = Mock()
    monkeypatch.setattr(
        "app.chain.transfer.get_chain_transfer_history_port",
        lambda: Mock(),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MediaChain",
        Mock(side_effect=AssertionError("accepted replay 不应重新在线识别")),
    )
    monkeypatch.setattr(
        "app.chain.transfer.TmdbChain",
        Mock(side_effect=AssertionError("accepted replay 不应重新在线获取剧集")),
    )

    result = execution_chain._TransferChain__handle_transfer(restored_task)

    assert result[0] is True
    assert restored_task.episodes_info == []


def test_planned_execution_restores_resolved_context_without_online_recognition():
    """planned 恢复必须从 checkpoint 还原 meta/media/episodes，不再访问识别服务。"""
    checkpoint = _resolved_checkpoint()
    task = _task()
    task.bind_admission_task_id("task-planned-context")
    _bind_planning_input(task, checkpoint.planning_input)
    _bind_checkpoint(task, checkpoint)
    chain = _chain(repository=Mock(), checkpoint=checkpoint)
    chain.jobview = Mock()
    chain.eventmanager = Mock()
    chain.eventmanager.send_event.return_value = None

    with patch(
        "app.chain.transfer.MediaChain",
        side_effect=AssertionError("planned replay 不应重新在线识别"),
    ):
        result = chain._TransferChain__handle_transfer(task)

    assert result[0] is True
    chain.plan_transfer.assert_not_called()
    chain.execute_transfer_plan.assert_called_once()
    execute_call = chain.execute_transfer_plan.call_args
    restored_meta = execute_call.kwargs["meta"]
    restored_media = execute_call.kwargs["mediainfo"]
    assert isinstance(restored_meta, MetaBase)
    assert restored_meta.title == "Frozen Show S01E01"
    assert restored_meta.begin_episode == 1
    assert restored_media.title == "Frozen Show"
    assert restored_media.media_id == "987"
    assert task.meta is restored_meta
    assert task.mediainfo is restored_media
    assert len(task.episodes_info) == 1
    assert task.episodes_info[0].episode_number == 1
    assert task.episodes_info[0].name == "Frozen Episode"


def test_filemanager_resolves_drifted_target_from_checkpoint(monkeypatch):
    """运行时目录配置漂移后，executor 仍只按 checkpoint.target_storage 选适配器。"""
    checkpoint = _checkpoint()
    frozen_target = object()
    drifted_target = object()
    source_oper = object()
    requested_storages = []
    module = object.__new__(FileManagerModule)

    def get_storage(storage: str):
        """记录执行期选择的存储并为漂移配置提供错误适配器。"""
        requested_storages.append(storage)
        return frozen_target if storage == checkpoint.target_storage else drifted_target

    monkeypatch.setattr(
        module,
        "_FileManagerModule__get_storage_oper",
        get_storage,
    )
    execute = Mock(
        return_value=TransferInfo(
            success=True,
            fileitem=_task().fileitem,
            transfer_type="copy",
        )
    )
    monkeypatch.setattr(TransHandler, "execute_transfer_plan", execute)

    FileManagerModule.execute_transfer_plan(
        module,
        checkpoint,
        meta=None,
        mediainfo=None,
        source_oper=source_oper,
    )

    assert requested_storages == [checkpoint.target_storage]
    assert execute.call_args.kwargs["target_oper"] is frozen_target
    assert drifted_target is not frozen_target


def test_pre_checkpoint_recognition_failure_records_retryable_error(monkeypatch):
    """未识别拒绝必须先建立 plan/execution checkpoint，且不提前写终态副作用。"""
    task = _task()
    task.meta = MetaBase("Unrecognized.Movie.2026.mkv")
    task.bind_admission_task_id("task-before-checkpoint")
    chain = _chain()
    chain._worker_owner_id = "recognition-owner"
    chain.jobview = Mock()
    chain.queue_failed_transfer_notification = Mock()
    chain.runtime_config = SimpleNamespace(
        ai_agent_enable=True,
        ai_agent_retry_transfer=True,
    )
    chain._TransferChain__mark_torrent_completed_if_done = Mock()
    chain._transfer_admissions.checkpoint_plan.side_effect = (
        lambda **kwargs: _planned_admission(task, kwargs["checkpoint"])
    )
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = None
    monkeypatch.setattr("app.chain.transfer.MediaChain", lambda: media_chain)
    monkeypatch.setattr(
        "app.chain.transfer.get_chain_transfer_history_port",
        lambda: SimpleNamespace(),
    )
    record_transfer_failure = Mock()
    add_transfer_fail = Mock()
    monkeypatch.setattr(
        "app.chain.transfer.record_transfer_failure",
        record_transfer_failure,
    )
    monkeypatch.setattr("app.chain.transfer.add_transfer_fail", add_transfer_fail)

    result = chain._TransferChain__handle_transfer(task)

    assert result == (False, "未识别到媒体信息")
    assert task.plan_checkpoint is not None
    assert task.plan_checkpoint.rejection_error == "未识别到媒体信息"
    assert task.plan_checkpoint.items == ()
    assert task.execution_checkpoint is not None
    assert task.execution_checkpoint.payload["outcome"] == "failed"
    assert [step.kind for step in chain._transfer_executions.steps.values()] == [
        "reject"
    ]
    chain._transfer_admissions.record_planning_failure.assert_not_called()
    record_transfer_failure.assert_not_called()
    add_transfer_fail.assert_not_called()
    chain.queue_failed_transfer_notification.assert_not_called()
    chain._TransferChain__mark_torrent_completed_if_done.assert_not_called()


def test_recognition_rejection_without_writer_has_zero_terminal_side_effects(
        monkeypatch,
) -> None:
    """缺 writer 时未识别拒绝不得提交计划、历史、通知或 AI 重试。"""
    task = _task()
    task.meta = MetaBase("Unrecognized.Movie.2026.mkv")
    task.bind_admission_task_id("task-rejection-missing-writer")
    chain = _chain()
    chain.durable_event_writer = None
    chain.jobview = Mock()
    chain.queue_failed_transfer_notification = Mock()
    chain._TransferChain__mark_torrent_completed_if_done = Mock()
    chain.runtime_config = SimpleNamespace(
        ai_agent_enable=True,
        ai_agent_retry_transfer=True,
    )
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = None
    monkeypatch.setattr("app.chain.transfer.MediaChain", lambda: media_chain)
    monkeypatch.setattr(
        "app.chain.transfer.get_chain_transfer_history_port",
        lambda: SimpleNamespace(),
    )
    record_transfer_failure = Mock()
    add_transfer_fail = Mock()
    monkeypatch.setattr(
        "app.chain.transfer.record_transfer_failure",
        record_transfer_failure,
    )
    monkeypatch.setattr("app.chain.transfer.add_transfer_fail", add_transfer_fail)

    with pytest.raises(RuntimeError, match="缺少 durable 原子写入端口"):
        chain._TransferChain__handle_transfer(task)

    assert task.plan_checkpoint is None
    assert task.execution_checkpoint is None
    chain._transfer_admissions.checkpoint_plan.assert_not_called()
    record_transfer_failure.assert_not_called()
    add_transfer_fail.assert_not_called()
    chain.queue_failed_transfer_notification.assert_not_called()
    chain._TransferChain__mark_torrent_completed_if_done.assert_not_called()


def test_planning_rejection_checkpoint_round_trips_and_rejects_file_steps():
    """拒绝原因必须稳定序列化，且不能与真实文件步骤同时存在。"""
    checkpoint = replace(
        _checkpoint(),
        items=(),
        rejection_error="未识别到媒体信息",
    )

    restored = transfer_application.TransferPlanCheckpoint.from_payload(
        checkpoint.to_payload()
    )

    assert restored == checkpoint
    assert restored.rejection_error == "未识别到媒体信息"
    with pytest.raises(ValueError, match="不得包含文件步骤"):
        replace(checkpoint, items=_checkpoint().items)


def test_preview_plans_without_persistence_or_file_side_effects():
    """preview 只生成和投影计划，不得触碰准入仓储或存储执行器。"""
    repository = Mock()
    task = _task(preview=True)
    planning_input = _planning_input(preview=True)
    checkpoint = _checkpoint(preview=True)
    _bind_planning_input(task, planning_input)
    chain = _chain(repository=repository, checkpoint=checkpoint)

    chain._plan_checkpoint_and_execute(task)

    chain.plan_transfer.assert_called_once()
    repository.admit.assert_not_called()
    repository.checkpoint_plan.assert_not_called()
    repository.record_planning_failure.assert_not_called()
    chain.execute_transfer_plan.assert_called_once()
    execute_call = chain.execute_transfer_plan.call_args
    executed_checkpoint = (
        execute_call.args[0]
        if execute_call.args
        else execute_call.kwargs["checkpoint"]
    )
    assert executed_checkpoint.preview is True


def test_preview_executor_projects_result_without_touching_storage():
    """FileManager 的 preview executor 不得查询、创建、复制或删除存储对象。"""
    checkpoint = _checkpoint(preview=True)
    source_oper = Mock()
    target_oper = Mock()

    result = TransHandler().execute_transfer_plan(
        checkpoint,
        meta=None,
        mediainfo=None,
        source_oper=source_oper,
        target_oper=target_oper,
    )

    assert result.success is True
    assert result.target_item.path == checkpoint.final_target_path
    assert result.file_list_new == [checkpoint.final_target_path]
    assert source_oper.mock_calls == []
    assert target_oper.mock_calls == []


def test_filemanager_explicit_plan_then_execute_uses_same_checkpoint():
    """FileManager 的调用方必须显式把冻结计划交给唯一执行入口。"""
    module = object.__new__(FileManagerModule)
    checkpoint = _checkpoint()
    result = TransferInfo(success=True, fileitem=_task().fileitem, transfer_type="copy")
    module.plan_transfer = Mock(return_value=checkpoint)
    module.execute_transfer_plan = Mock(return_value=result)
    fileitem = _task().fileitem

    planned = module.plan_transfer(
        fileitem=fileitem,
        meta=None,
        mediainfo=None,
        target_storage="local",
        target_path=Path("/library"),
        transfer_type="copy",
        preview=False,
    )
    returned = module.execute_transfer_plan(
        planned,
        meta=None,
        mediainfo=None,
    )

    assert returned is result
    module.plan_transfer.assert_called_once()
    module.execute_transfer_plan.assert_called_once()
    execute_call = module.execute_transfer_plan.call_args
    assert execute_call.args[0] is checkpoint


def test_sync_and_worker_paths_share_checkpoint_orchestrator():
    """同步直跑和后台 worker 必须汇入同一个检查点编排，不得旁路旧 transfer。"""
    module = ast.parse(inspect.getsource(inspect.getmodule(TransferChain)))
    functions = {
        node.name: node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    handler = functions["__handle_transfer"]
    handler_calls = {
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    performer = functions["__perform_transfer"]
    performer_calls = {
        node.func.attr
        for node in ast.walk(performer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    execute_batch = functions["_execute_transfer"]
    execute_calls = {
        node.func.attr
        for node in ast.walk(execute_batch)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "__perform_transfer" in handler_calls
    assert "_plan_checkpoint_and_execute" in performer_calls
    assert "transfer" not in handler_calls
    assert "__handle_transfer" in execute_calls
