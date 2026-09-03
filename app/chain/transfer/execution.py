"""持久整理步骤执行、探测与检查点推进。"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple, Union, cast

from app.application.directory import DirectoryHelper
from app.application.history import (
    max_failed_retries,
)
from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionCommand,
    TransferExecutionRepository,
    TransferExecutionSnapshot,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferStepIntent,
    TransferStepResult,
    TransferStepState,
)
from app.application.transfer.workflow import (
    TransferAdmission,
    TransferLeaseLostError,
    TransferTask,
)
from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.chain.transfer.records import apply_download_history_classification
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.schemas.transfer import TransferInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
)


class _TransferRetryDeferred(RuntimeError):
    """表示已把确定失败交回唯一持久重试调度器。"""


class _TransferManualReviewRequired(RuntimeError):
    """表示外部结果无法严格判定，任务已隔离等待人工复核。"""


class _TransferRetryExhausted(RuntimeError):
    """表示步骤重试预算耗尽，任务可进入原子失败结算。"""

    def __init__(self, message: str, snapshot: TransferExecutionSnapshot) -> None:
        """保存失败原因和数据库建立的结算检查点。"""
        super().__init__(message)
        self.snapshot = snapshot


class _DurableTransferStepRunner:
    """以稳定顺序、lease 和 attempt fencing 执行整理外部步骤。"""

    _RETRY_DELAY_SECONDS = 30

    def __init__(
            self,
            *,
            task_id: str,
            lease_token: str,
            checkpoint_fingerprint: str,
            repository: TransferExecutionRepository,
    ) -> None:
        """绑定单个任务的持久执行命令与全局步骤序号。"""
        self._task_id = task_id
        self._lease_token = lease_token
        self._checkpoint_fingerprint = checkpoint_fingerprint
        self._repository = repository
        self._command = TransferExecutionCommand(self._repository)
        snapshot = self._repository.get_snapshot(task_id=task_id)
        existing_steps = list(snapshot.steps) if snapshot else []
        matching_ordinals = [
            step.ordinal
            for step in existing_steps
            if step.checkpoint_fingerprint == checkpoint_fingerprint
        ]
        self._ordinal = (
            min(matching_ordinals)
            if matching_ordinals
            else max((step.ordinal for step in existing_steps), default=-1) + 1
        )
        self._operation_ids = [
            step.operation_id
            for step in existing_steps
            if step.ordinal < self._ordinal
        ]

    @staticmethod
    def _retry_due_at() -> str:
        """生成固定退避后的 UTC 调度时间。"""
        return (
            datetime.now(timezone.utc)
            + timedelta(seconds=_DurableTransferStepRunner._RETRY_DELAY_SECONDS)
        ).strftime("%Y-%m-%d %H:%M:%S.%f")

    def _execute_attempt(
            self,
            *,
            step: Any,
            execute: Callable[[], TransferStepResult],
            observe: Callable[[], TransferOperationObservation],
    ) -> TransferStepResult:
        """在事务外执行副作用，异常后先探测结果再决定重试或人工复核。"""
        try:
            result = execute()
        except Exception as error:
            try:
                observation = observe()
            except Exception as observe_error:
                observation = TransferOperationObservation(
                    state=TransferOperationObservationState.UNKNOWN,
                    evidence=TransferStepResult(payload={
                        "execute_error": str(error),
                        "observe_error": str(observe_error),
                    }),
                )
            if observation.state is TransferOperationObservationState.APPLIED:
                completed = self._command.complete(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    result=observation.evidence,
                )
                if completed.result is None:
                    raise RuntimeError("探测完成的整理步骤缺少结果证据") from error
                return completed.result
            if observation.state is not TransferOperationObservationState.NOT_APPLIED:
                reason = (
                    f"整理步骤 {step.operation_id} 执行异常后外部结果为 "
                    f"{observation.state.value}，禁止自动重放：{error}"
                )
                self._command.manual_review(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    error=reason,
                    evidence=observation.evidence,
                )
                raise _TransferManualReviewRequired(reason) from error
            snapshot = self._repository.get_snapshot(task_id=self._task_id)
            if snapshot is None:
                raise RuntimeError("整理执行快照在步骤失败后丢失") from error
            if snapshot.retry_count >= max_failed_retries():
                exhausted = self._command.exhaust(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    error=str(error),
                    evidence=observation.evidence,
                )
                raise _TransferRetryExhausted(str(error), exhausted) from error
            self._command.defer(
                task_id=self._task_id,
                lease_token=self._lease_token,
                step=step,
                error=str(error),
                retry_due_at=self._retry_due_at(),
                evidence=observation.evidence,
            )
            raise _TransferRetryDeferred(str(error)) from error
        completed = self._command.complete(
            task_id=self._task_id,
            lease_token=self._lease_token,
            step=step,
            result=result,
        )
        if completed.result is None:
            raise RuntimeError("整理步骤完成后缺少持久结果")
        return completed.result

    def run(
            self,
            *,
            phase: str,
            kind: str,
            payload: Mapping[str, Any],
            execute: Callable[[], TransferStepResult],
            observe: Callable[[], TransferOperationObservation],
    ) -> TransferStepResult:
        """幂等消费一个步骤，遗留 STARTED 必须先取得严格观察结论。"""
        intent = TransferStepIntent.create(
            task_id=self._task_id,
            checkpoint_fingerprint=self._checkpoint_fingerprint,
            ordinal=self._ordinal,
            phase=phase,
            kind=kind,
            payload=payload,
        )
        self._ordinal += 1
        self._operation_ids.append(intent.operation_id)
        step = self._command.prepare(
            task_id=self._task_id,
            lease_token=self._lease_token,
            intent=intent,
        )
        if step.state is TransferStepState.SUCCEEDED:
            if step.result is None:
                raise RuntimeError("已成功整理步骤缺少结果证据")
            return step.result
        if step.state is TransferStepState.MANUAL_REVIEW:
            raise _TransferManualReviewRequired(
                step.last_error or "整理步骤正在等待人工复核"
            )
        if step.state is TransferStepState.PREPARED:
            step = self._command.begin(
                task_id=self._task_id,
                lease_token=self._lease_token,
                operation_id=step.operation_id,
            )
        elif step.state is TransferStepState.FAILED:
            step = self._command.resume_failed(
                task_id=self._task_id,
                lease_token=self._lease_token,
                step=step,
            )
        elif step.state is TransferStepState.STARTED:
            observation = observe()
            if observation.state is TransferOperationObservationState.APPLIED:
                completed = self._command.complete(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    result=observation.evidence,
                )
                if completed.result is None:
                    raise RuntimeError("探测完成的整理步骤缺少结果证据")
                return completed.result
            if observation.state is TransferOperationObservationState.NOT_APPLIED:
                step = self._command.restart_after_not_applied(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    evidence=observation.evidence,
                )
            else:
                reason = (
                    f"遗留步骤 {step.operation_id} 外部结果为 "
                    f"{observation.state.value}，禁止自动重放"
                )
                self._command.manual_review(
                    task_id=self._task_id,
                    lease_token=self._lease_token,
                    step=step,
                    error=reason,
                    evidence=observation.evidence,
                )
                raise _TransferManualReviewRequired(reason)
        else:
            raise RuntimeError(f"不支持的整理步骤状态：{step.state}")
        return self._execute_attempt(step=step, execute=execute, observe=observe)

    def checkpoint(self, transferinfo: TransferInfo) -> TransferExecutionCheckpoint:
        """提交足以独立重放终态结算的聚合执行结果。"""
        outcome = (
            "overwrite_skipped"
            if transferinfo.overwrite_skipped
            else "succeeded" if transferinfo.success else "failed"
        )
        checkpoint = TransferExecutionCheckpoint.create(
            payload={
                "outcome": outcome,
                "transferinfo": transferinfo.model_dump(mode="json"),
            },
            operation_ids=tuple(self._operation_ids),
            skip_reason=(
                (transferinfo.message or "zero-side-effect transfer")
                if not self._operation_ids
                else None
            ),
        )
        snapshot = self._command.checkpoint(
            task_id=self._task_id,
            lease_token=self._lease_token,
            checkpoint=checkpoint,
        )
        if snapshot.checkpoint is None:
            raise RuntimeError("整理执行检查点提交后无法回读")
        return snapshot.checkpoint



class TransferExecutionOwner(_TransferOwnerBase):
    """持有整理准入租约、外部步骤执行与结果检查点。"""

    def _TransferChain__register_claimed_admission(
            self,
            admission: TransferAdmission,
    ) -> None:
        """把仓储 claim 加入续期集合，覆盖恢复构造阶段的同步 I/O。"""
        if (
                admission.lease_owner != self._worker_owner_id
                or not admission.lease_token
        ):
            raise TransferLeaseLostError(
                f"整理任务 claim 投影无效：{admission.task_id}"
            )
        with self._worker_state_lock:
            self._owned_leases[admission.task_id] = (
                admission.lease_token,
                time.monotonic() + self._WORKER_LEASE_SECONDS,
            )
        self._TransferChain__ensure_lease_heartbeat_owner()

    def _TransferChain__bind_claimed_admission(
            self,
            task: TransferTask,
            admission: TransferAdmission,
    ) -> None:
        """校验仓储 claim 投影并把 token 私有绑定到执行任务。"""
        if admission.task_id != task.admission_task_id:
            raise TransferLeaseLostError(
                f"整理任务 claim 投影无效：{admission.task_id}"
            )
        self._TransferChain__register_claimed_admission(admission)
        assert admission.lease_owner is not None
        assert admission.lease_token is not None
        task.bind_execution_lease(
            owner_id=admission.lease_owner,
            lease_token=admission.lease_token,
        )

    def _TransferChain__claim_admitted_task(
            self,
            task: TransferTask,
            task_id: str,
    ) -> None:
        """取得已准入任务的唯一租约，并绑定到内存任务。"""
        claimed = cast(
            Optional[TransferAdmission],
            self._transfer_admissions.claim_task(
                task_id=task_id,
                owner_id=self._worker_owner_id,
                lease_seconds=self._WORKER_LEASE_SECONDS,
            ),
        )
        if claimed is None:
            raise TransferLeaseLostError(f"整理任务已由其他 worker claim：{task_id}")
        try:
            self._TransferChain__bind_claimed_admission(task, claimed)
        except Exception as err:
            self._TransferChain__release_admission_claim(claimed, error=str(err))
            raise

    def _TransferChain__admit_transfer(self, task: TransferTask) -> TransferAdmission:
        """持久化源文件并在内存入队前取得执行租约。"""
        fileitem = task.fileitem if task else None
        if not fileitem or not fileitem.storage or not fileitem.path:
            raise ValueError("整理任务缺少源文件身份")
        planning_input = task.planning_input or self._TransferChain__build_planning_input(task)
        task.bind_planning_input(planning_input)
        admission = cast(
            TransferAdmission,
            self._transfer_admissions.admit(
                storage=fileitem.storage,
                src_path=fileitem.path,
                planning_input=planning_input,
            ),
        )
        task.bind_admission_task_id(admission.task_id)
        self._TransferChain__claim_admitted_task(task, admission.task_id)
        return admission

    def _TransferChain__claim_task_for_execution(self, task: TransferTask) -> None:
        """校验队列任务既有租约，并为兼容旧队列项补取唯一租约。"""
        if task.preview:
            return
        self._TransferChain__ensure_lease_runtime_state()
        if task.lease_token:
            self._TransferChain__assert_owned_lease(task)
            return
        if not task.admission_task_id:
            self._TransferChain__admit_transfer(task)
            return
        self._TransferChain__claim_admitted_task(task, task.admission_task_id)


    def _TransferChain__handle_transfer(
            self, task: TransferTask, callback: Optional[Callable] = None
    ) -> Optional[Tuple[bool, str]]:
        """执行整理并统一记录 checkpoint 前的返回失败或异常。"""
        try:
            result = self._TransferChain__perform_transfer(task, callback)
        except Exception as error:
            if not getattr(error, "_transfer_planning_failure_recorded", False):
                self._TransferChain__record_uncheckpointed_failure(task, error)
            raise
        if result and not result[0]:
            self._TransferChain__record_uncheckpointed_failure(task, result[1])
        return result

    def _TransferChain__perform_transfer(
            self, task: TransferTask, callback: Optional[Callable] = None
    ) -> Optional[Tuple[bool, str]]:
        """
        处理整理任务
        """
        try:
            if task.plan_checkpoint is not None:
                return self._TransferChain__handle_planned_transfer(task, callback)
            # 识别
            transferhis = self.transfer_history_repository
            # 显式标注联合：下面既会赋回音乐识别结果（MusicInfo），也会赋回影视识别
            # 结果（MediaInfo），不标注时会被推断成其中一种，另一种就成了假错误
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = task.mediainfo
            mediainfo_changed = False
            need_obtain_images = False
            if not mediainfo:
                download_history = task.download_history
                # 下载用户
                if download_history:
                    task.username = download_history.username
                    # 识别媒体信息
                    history_year_conflict = self._is_movie_year_conflict(
                        task.meta, download_history
                    )
                    if (
                            download_history.media_source
                            and download_history.media_id
                            and not history_year_conflict
                    ):
                        # 下载记录中已存在识别信息。这里不再重复标注类型：函数开头
                        # 已把 mediainfo 声明为 MediaInfo | MusicInfo | None，重复
                        # 声明会遮蔽它，把音乐识别结果判成类型错误
                        mediainfo = MediaChain().recognize_media(
                            mtype=task.mtype or MediaType(download_history.type),
                            media_source=download_history.media_source,
                            media_id=download_history.media_id,
                            music_type=self._download_history_music_type(download_history),
                            episode_group=download_history.episode_group,
                        )
                        need_obtain_images = True
                        if mediainfo:
                            mediainfo = apply_download_history_classification(mediainfo, download_history)
                    else:
                        if history_year_conflict:
                            logger.info(
                                f"{task.fileitem.name} 文件年份 {task.meta.year} 与下载记录年份 "
                                f"{download_history.year} 不一致，按文件名重新识别"
                            )
                        recognize_kwargs = {"obtain_images": True}
                        if task.media_source:
                            recognize_kwargs["media_source"] = task.media_source
                        if task.mtype:
                            recognize_kwargs["mtype"] = task.mtype
                        mediainfo = MediaChain().recognize_by_meta(
                            task.meta, **recognize_kwargs
                        )
                        if mediainfo:
                            mediainfo = apply_download_history_classification(mediainfo, download_history)
                else:
                    # 识别媒体信息
                    recognize_kwargs = {"obtain_images": True}
                    if task.media_source:
                        recognize_kwargs["media_source"] = task.media_source
                    if task.mtype:
                        recognize_kwargs["mtype"] = task.mtype
                    mediainfo = MediaChain().recognize_by_meta(
                        task.meta, **recognize_kwargs
                    )

                # 音乐必须先经过音乐元数据模块识别；远端不可用时再保留本地标签结果，
                # 避免因离线兜底提前赋值而跳过音乐识别链。
                if not mediainfo and isinstance(task.meta, MetaMusic):
                    mediainfo = self._music_info_from_meta(task.meta)

                # 按名称识别时已在识别链路补图，这里只补齐显式ID识别的场景。
                if mediainfo and need_obtain_images:
                    self.obtain_images(mediainfo=mediainfo)

                if mediainfo and task.media_source:
                    mediainfo.scrape_source = task.media_source

                if not mediainfo:
                    if task.preview:
                        return False, "未识别到媒体信息"
                    transferinfo = self._TransferChain__checkpoint_planning_rejection(
                        task,
                        "未识别到媒体信息",
                    )
                    if callback:
                        return cast(Tuple[bool, str], callback(task, transferinfo))
                    return transferinfo.success, transferinfo.message or ""

                mediainfo_changed = True

            # accepted/planned 恢复使用持久快照；在线补充会让首次执行与重放产生漂移。
            if not task.planning_context_restored:
                mediainfo = MediaChain().supplement_tmdb_info(mediainfo, task.meta)
            task.mediainfo = mediainfo

            # 只有 TMDB 主源沿用历史 TMDB 标题，避免辅助 ID 改写其它识别源标题。
            if (
                    not self.runtime_config.scrape_follow_tmdb
                    and mediainfo.media_source == MediaSource.TMDB
            ):
                transfer_history = transferhis.get_by_media_identity(
                    media_source=mediainfo.media_source.value,
                    media_id=mediainfo.media_id,
                    mtype=mediainfo.type.value,
                )
                if transfer_history and mediainfo.title != transfer_history.title:
                    mediainfo.title = transfer_history.title
                    mediainfo_changed = True

            if mediainfo_changed:
                # 更新任务信息
                task.mediainfo = mediainfo
                # 更新队列任务
                if not self.jobview.migrate_task(task):
                    logger.info(f"{task.fileitem.name} 已存在整理任务，跳过重复处理")
                    return False, f"{task.fileitem.name} 已在整理队列中"

            # 获取集数据
            if (
                    task.mediainfo.type == MediaType.TV
                    and task.mediainfo.tmdb_id
                    and not task.episodes_info
                    and not task.planning_context_restored
            ):
                # 判断注意season为0的情况
                season_num = task.mediainfo.season
                if season_num is None and task.meta.season_seq:
                    if task.meta.season_seq.isdigit():
                        season_num = int(task.meta.season_seq)
                # 默认值1
                if season_num is None:
                    season_num = 1
                task.episodes_info = TmdbChain().tmdb_episodes(
                    tmdbid=task.mediainfo.tmdb_id,
                    season=season_num,
                    episode_group=task.mediainfo.episode_group,
                )

            # 查询整理目标目录
            if not task.target_directory:
                if task.target_path:
                    # 指定目标路径，`手动整理`场景下使用，忽略源目录匹配，使用指定目录匹配
                    task.target_directory = DirectoryHelper().get_dir(
                        media=task.mediainfo,
                        dest_path=task.target_path,
                        target_storage=task.target_storage,
                    )
                else:
                    # 启用源目录匹配时，根据源目录匹配下载目录，否则按源目录同盘优先原则，如无源目录，则根据媒体信息获取目标目录
                    task.target_directory = DirectoryHelper().get_dir(
                        media=task.mediainfo,
                        storage=task.fileitem.storage,
                        src_path=Path(task.fileitem.path),
                        target_storage=task.target_storage,
                    )
            if not task.target_storage and task.target_directory:
                task.target_storage = task.target_directory.library_storage

            if self._requires_automatic_category(task) and not task.mediainfo.category:
                # MusicInfo 无 tmdb_id 字段，但模型 __getattr__ 已兜底返回 None
                if task.mediainfo.tmdb_id:
                    error_message = "TMDB 信息未匹配到媒体分类，无法按媒体类别整理"
                else:
                    error_message = "未识别到 TMDB 辅助信息，无法按媒体类别整理"
                logger.error(f"{task.fileitem.name} {error_message}")
                if task.preview:
                    return False, error_message
                transferinfo = self._TransferChain__checkpoint_planning_rejection(
                    task,
                    error_message,
                )
                if callback:
                    return cast(Tuple[bool, str], callback(task, transferinfo))
                return transferinfo.success, transferinfo.message or ""

            # 正在处理
            self.jobview.running_task(task)

            # 广播事件，请示额外的源、目标存储支持。
            source_oper = self._TransferChain__select_storage_oper(task.fileitem.storage)
            target_oper = self._TransferChain__select_storage_oper(task.target_storage)

            # 纯规划先提交 durable checkpoint，任何文件副作用只能发生在提交之后。
            transferinfo = self._plan_checkpoint_and_execute(
                task,
                source_oper=source_oper,
                target_oper=target_oper,
            )
            if not transferinfo:
                logger.error("文件整理模块运行失败")
                return False, "文件整理模块运行失败"

            # 回调，位置传参：任务、整理结果
            if callback:
                return callback(task, transferinfo)

            return transferinfo.success, transferinfo.message

        finally:
            # 移除已完成的任务
            self.jobview.try_remove_job(task)
            self._finish_scrape_batch_task(task)
