"""
整理规划输入、接纳快照与持久步骤仓储；不注册测试用例。
"""

import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from app.application.transfer import workflow as transfer_application
from app.application.transfer.execution import TransferExecutionSnapshot, TransferExecutionState, TransferExecutionStep, TransferStepState
from app.application.transfer.workflow import TransferTask
from app.chain.transfer.facade import TransferChain
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo


def _planning_contracts():
    """
    延迟解析新合同，使未落地时每个 pytest 用例给出精确缺口。
    """
    return (
        getattr(transfer_application, "TransferPlanningInput"),
        getattr(transfer_application, "TransferPlanItem"),
        getattr(transfer_application, "TransferPlanCheckpoint"),
    )


def _source_snapshot() -> dict[str, object]:
    """
    返回可独立跨重启还原的源文件快照。
    """
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
    """
    构造包含旧调用意图、媒体提示和目标配置的规划输入。
    """
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
    """
    构造带完整输入和有序执行项的冻结计划。
    """
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
    """
    构造包含在线识别完成后领域快照的 planned checkpoint。
    """
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
    """
    构造不触碰真实文件系统的规划任务。
    """
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
    """
    通过正式绑定接口保存私有输入，禁止测试写入 Pydantic 内部字典。
    """
    binder = getattr(task, "bind_planning_input")
    binder(planning_input)


def _bind_checkpoint(task: TransferTask, checkpoint) -> None:
    """
    通过正式绑定接口模拟 planned 任务的跨重启恢复。
    """
    binder = getattr(task, "bind_plan_checkpoint")
    binder(checkpoint)


def _planned_admission(task: TransferTask, checkpoint):
    """
    构造仓储 checkpoint 提交后返回的冻结投影。
    """
    return SimpleNamespace(
        task_id=task.admission_task_id,
        state="planned",
        planning_input=task.planning_input,
        checkpoint=checkpoint,
    )


class _ExecutionRepositoryStub:
    """
    为规划编排测试提供严格但内存化的 execution repository。
    """

    def __init__(self) -> None:
        """
        初始化空步骤集合与未启动执行态。
        """
        self.steps = {}
        self.state = TransferExecutionState.NOT_STARTED
        self.checkpoint = None

    def get_snapshot(self, *, task_id):
        """
        返回当前任务的类型化执行投影。
        """
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
        """
        幂等保存准备态步骤。
        """
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
        """
        把准备态步骤推进到已开始。
        """
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
        """
        以当前 attempt 提交成功证据。
        """
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
        """
        保存可重放终态的聚合检查点。
        """
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
        """
        把执行结果不确定的步骤隔离到人工复核态。
        """
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
    """
    构造只保留规划编排依赖的 TransferChain 骨架。
    """
    chain = object.__new__(TransferChain)
    chain.transfer_history_repository = Mock()
    chain.transfer_execution_repository = Mock()
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
        """
        为规划测试返回与进程 owner 匹配的稳定 claim。
        """
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
        """
        让规划测试沿正式模块入口调用其可观察的宿主执行替身。
        """
        assert method == "execute_transfer_plan"
        checkpoint_arg = kwargs.pop("checkpoint")
        return chain.execute_transfer_plan(checkpoint_arg, *args, **kwargs)

    chain.run_module = Mock(side_effect=run_module)
    return chain
