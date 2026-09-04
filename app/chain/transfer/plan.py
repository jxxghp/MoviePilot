"""持久整理计划构建、恢复与兼容 provider 执行。"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple, Union, cast

from app.application.classification.reference import (
    EffectiveClassificationSnapshot,
    apply_persisted_classification_snapshot,
    effective_classification_snapshot,
)
from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferExecutionOutcome,
    TransferExecutionRepository,
    TransferExecutionState,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferPlanningRejectedError,
    TransferStepResult,
)
from app.application.transfer.workflow import (
    TransferLeaseLostError,
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferPlanningStateError,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
    TransferTask,
)
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metaanime import MetaAnime
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.meta.metavideo import MetaVideo
from app.runtime.log import logger
from app.schemas.event import StorageOperSelectionEventData
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import (
    ChainEventType,
    MediaSource,
    MediaType,
)
from app.schemas.workflow import FileItem

from .checkpoint import build_planning_rejection_checkpoint, restore_planned_task
from .execution import _DurableTransferStepRunner, _TransferRetryExhausted


class TransferPlanningOwner(_TransferOwnerBase):
    """唯一持有整理准入后的冻结计划与 provider 选择。"""

    def _TransferChain__build_planning_input(
            self,
            task: TransferTask,
            *,
            cleanup_dest_fileitem: Optional[FileItem] = None,
    ) -> TransferPlanningInput:
        """冻结准入时已知的请求参数，供 accepted 任务跨重启重新规划。"""
        target_directory = task.target_directory
        options = {
            "scrape": task.scrape,
            "library_type_folder": task.library_type_folder,
            "library_category_folder": task.library_category_folder,
            "manual": task.manual,
            "background": task.background,
            "username": task.username,
            "downloader": task.downloader,
            "download_hash": task.download_hash,
            "cleanup_dest_fileitem": self._TransferChain__json_snapshot(cleanup_dest_fileitem),
            "_meta_kind": type(task.meta).__name__ if task.meta else None,
            "_mediainfo_kind": (
                type(task.mediainfo).__name__ if task.mediainfo else None
            ),
        }
        return TransferPlanningInput(
            source_fileitem=self._TransferChain__json_snapshot(task.fileitem),
            meta=self._TransferChain__json_snapshot(task.meta),
            mediainfo=self._TransferChain__json_snapshot(task.mediainfo),
            target_directory=self._TransferChain__json_snapshot(target_directory),
            target_storage=task.target_storage,
            target_path=task.target_path.as_posix() if task.target_path else None,
            requested_transfer_type=task.transfer_type,
            media_source=task.media_source.value if task.media_source else None,
            media_id=task.media_id,
            media_type=task.mtype.value if task.mtype else None,
            need_scrape=bool(task.scrape),
            need_rename=bool(target_directory.renaming) if target_directory else True,
            need_notify=bool(target_directory.notify) if target_directory else False,
            overwrite_mode=(target_directory.overwrite_mode if target_directory else None),
            episodes_info=tuple(
                self._TransferChain__json_snapshot(episode) for episode in (task.episodes_info or [])
            ),
            preview=bool(task.preview),
            options=options,
        )

    def _TransferChain__build_provider_invocation_snapshot(
            self,
            task: TransferTask,
    ) -> TransferProviderInvocationSnapshot:
        """冻结目录解析后的旧 ABI 参数，并保留 None 与 False 的原值差异。"""
        return TransferProviderInvocationSnapshot(
            fileitem=self._TransferChain__json_snapshot(task.fileitem),
            meta=self._TransferChain__json_snapshot(task.meta),
            meta_kind=type(task.meta).__name__ if task.meta else None,
            mediainfo=self._TransferChain__json_snapshot(task.mediainfo),
            mediainfo_kind=(
                type(task.mediainfo).__name__ if task.mediainfo else None
            ),
            target_directory=self._TransferChain__json_snapshot(task.target_directory),
            target_storage=task.target_storage,
            target_path=task.target_path.as_posix() if task.target_path else None,
            transfer_type=task.transfer_type,
            scrape=task.scrape,
            library_type_folder=task.library_type_folder,
            library_category_folder=task.library_category_folder,
            episodes_info=tuple(
                self._TransferChain__json_snapshot(episode) for episode in (task.episodes_info or [])
            ),
            preview=bool(task.preview),
        )

    @staticmethod
    def _TransferChain__restore_meta_snapshot(
            payload: Optional[dict[str, Any]],
            kind: object,
    ) -> MetaBase:
        """从持久快照恢复元数据对象，不重新运行文件名解析。"""
        if not payload:
            raise TransferPlanningStateError("整理持久快照缺少已解析元数据")
        if kind == "MetaMusic" or payload.get("type") == MediaType.MUSIC.value:
            return MetaMusic.from_dict(payload)
        meta_class = (
            MetaAnime
            if kind == "MetaAnime"
            else MetaVideo
        )
        meta = meta_class("")
        for key, value in payload.items():
            descriptor = getattr(meta_class, key, None)
            if isinstance(descriptor, property) and descriptor.fset is None:
                continue
            if key == "type" and isinstance(value, str):
                value = MediaType(value)
            elif key == "media_source" and isinstance(value, str):
                value = MediaSource(value)
            setattr(meta, key, value)
        return meta

    @staticmethod
    def _TransferChain__restore_mediainfo_snapshot(
            payload: Optional[dict[str, Any]],
            kind: object,
    ) -> Union[MediaInfo, MusicInfo]:
        """从持久快照恢复媒体信息，不访问任何在线数据源。"""
        if not payload:
            raise TransferPlanningStateError("整理持久快照缺少已识别媒体信息")
        if kind == "MusicInfo" or payload.get("type") == MediaType.MUSIC.value:
            return MusicInfo.from_dict(payload)
        mediainfo = MediaInfo()
        mediainfo.from_dict(payload)
        return mediainfo

    def _TransferChain__restore_planned_task(self, task: TransferTask) -> None:
        """用冻结检查点覆盖易受配置和在线识别变化影响的任务字段。"""
        restore_planned_task(
            task,
            restore_meta=self._TransferChain__restore_meta_snapshot,
            restore_media=self._TransferChain__restore_mediainfo_snapshot,
        )

    def _TransferChain__select_storage_oper(self, storage: Optional[str]) -> Any:
        """按冻结存储标识请求插件存储适配器，未接管时返回空值。"""
        event_data = StorageOperSelectionEventData(storage=storage)
        event = self.eventmanager.send_event(
            ChainEventType.StorageOperSelection,
            event_data,
        )
        if event and event.event_data:
            resolved = event.event_data
            return resolved.storage_oper
        return None

    def _TransferChain__record_uncheckpointed_failure(
            self,
            task: TransferTask,
            error: object,
    ) -> None:
        """记录 checkpoint 前失败，保留 accepted 任务供后续重新规划。"""
        if (
                task.preview
                or task.plan_checkpoint
                or not task.admission_task_id
                or not task.lease_token
        ):
            return
        try:
            self._transfer_admissions.record_planning_failure(
                task_id=task.admission_task_id,
                lease_token=task.lease_token,
                error=str(error),
            )
        except Exception as record_error:
            logger.error(f"记录整理规划前失败原因出错：{record_error}")

    def _TransferChain__checkpoint_planning_rejection(
            self,
            task: TransferTask,
            error: str,
    ) -> TransferInfo:
        """把确定性规划拒绝冻结为零文件副作用计划并走统一终态结算。"""
        if task.preview:
            return TransferInfo(
                success=False,
                message=error,
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=task.transfer_type,
                need_notify=False,
            )
        self._TransferChain__claim_task_for_execution(task)
        self._TransferChain__assert_owned_lease(task)
        if getattr(self, "durable_event_writer", None) is None:
            raise RuntimeError(
                "非预览整理缺少 durable 原子写入端口，拒绝提交规划终态"
            )
        planning_input = task.planning_input or self._TransferChain__build_planning_input(task)
        task.bind_planning_input(planning_input)
        checkpoint = build_planning_rejection_checkpoint(
            task,
            error=error,
            planning_input=planning_input,
            classification_snapshot=effective_classification_snapshot(
                task.mediainfo
            ),
        )
        persisted = self._TransferChain__persist_transfer_checkpoint(
            task,
            planning_input=planning_input,
            checkpoint=checkpoint,
        )
        task.bind_plan_checkpoint(persisted)
        return self._plan_checkpoint_and_execute(task)

    def _TransferChain__execute_planning_rejection(
            self,
            task: TransferTask,
            checkpoint: TransferPlanCheckpoint,
    ) -> TransferInfo:
        """以可重放内部步骤确认冻结拒绝，并建立统一 execution checkpoint。"""
        error = checkpoint.rejection_error
        if not error:
            raise TransferPlanningStateError("整理拒绝计划缺少失败原因")
        step_runner = self._TransferChain__build_durable_step_runner(task, checkpoint)
        if step_runner is None:
            raise RuntimeError("非预览整理拒绝缺少 durable 步骤 runner")
        evidence = TransferStepResult(payload={"error": error})
        step_runner.run(
            phase="planning",
            kind="reject",
            payload={"error": error},
            execute=lambda: evidence,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.APPLIED,
                evidence=evidence,
            ),
        )
        transferinfo = TransferInfo(
            success=False,
            message=error,
            fileitem=task.fileitem,
            fail_list=[task.fileitem.path],
            transfer_type=checkpoint.resolved_transfer_type,
            need_notify=checkpoint.need_notify,
        )
        task.bind_execution_checkpoint(step_runner.checkpoint(transferinfo))
        return transferinfo

    def _TransferChain__handle_planned_transfer(
            self,
            task: TransferTask,
            callback: Optional[Callable[[TransferTask, TransferInfo], Tuple[bool, str]]],
    ) -> Tuple[bool, str]:
        """直接执行已提交检查点，不再触发识别、分类、选目录或重命名。"""
        self._TransferChain__restore_planned_task(task)
        self.jobview.running_task(task)
        settling_result = self._TransferChain__restore_settling_transfer_result(task)
        if settling_result is not None:
            if callback:
                return callback(task, settling_result)
            return settling_result.success, settling_result.message or ""
        checkpoint = task.plan_checkpoint
        assert checkpoint is not None
        target_storage = (
            checkpoint.provider_invocation.target_storage
            if checkpoint.provider_invocation
            else checkpoint.target_storage
        )
        transferinfo = self._plan_checkpoint_and_execute(
            task,
            source_oper=self._TransferChain__select_storage_oper(task.fileitem.storage),
            target_oper=self._TransferChain__select_storage_oper(target_storage),
        )
        if not transferinfo:
            raise RuntimeError("整理服务没有返回有效结果，请稍后重试")
        if callback:
            return callback(task, transferinfo)
        return transferinfo.success, transferinfo.message or ""

    def _TransferChain__restore_settling_transfer_result(
            self,
            task: TransferTask,
    ) -> Optional[TransferInfo]:
        """从 execution checkpoint 恢复终态结果，禁止再次执行外部步骤。"""
        repository: Optional[TransferExecutionRepository] = getattr(
            self,
            "_transfer_executions",
            None,
        )
        task_id = task.admission_task_id
        if repository is None or not task_id:
            if task.execution_checkpoint is not None:
                raise TransferExecutionConflictError(
                    "绑定执行检查点的整理任务缺少持久状态仓储"
                )
            return None
        snapshot = repository.get_snapshot(task_id=task_id)
        if snapshot is None:
            if task.execution_checkpoint is not None:
                raise TransferExecutionConflictError(
                    "绑定执行检查点的整理任务缺少持久执行状态"
                )
            return None
        if snapshot.state is not TransferExecutionState.SETTLING:
            return None
        checkpoint = snapshot.checkpoint
        bound_checkpoint = task.execution_checkpoint
        if (
                bound_checkpoint is not None
                and checkpoint is not None
                and bound_checkpoint.fingerprint != checkpoint.fingerprint
        ):
            raise TransferExecutionConflictError(
                "内存整理任务与持久结算检查点不一致"
            )
        if checkpoint is None:
            raise TransferExecutionConflictError(
                "settling 整理任务缺少可重放终态检查点"
            )
        self._TransferChain__assert_owned_lease(task)
        payload = checkpoint.payload
        raw_outcome = payload.get("outcome")
        try:
            outcome = TransferExecutionOutcome(
                raw_outcome if isinstance(raw_outcome, str) else ""
            )
        except (TypeError, ValueError) as error:
            raise TransferExecutionConflictError(
                "整理执行检查点缺少确定终态"
            ) from error
        transfer_payload = payload.get("transferinfo")
        if isinstance(transfer_payload, dict):
            transferinfo = cast(
                TransferInfo,
                TransferInfo.model_validate(transfer_payload),
            )
        elif outcome is TransferExecutionOutcome.FAILED:
            plan_checkpoint = task.plan_checkpoint
            transferinfo = TransferInfo(
                success=False,
                message=str(payload.get("error") or "整理失败"),
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=(
                    plan_checkpoint.resolved_transfer_type
                    if plan_checkpoint is not None
                    else None
                ),
                need_notify=(
                    plan_checkpoint.need_notify
                    if plan_checkpoint is not None
                    else False
                ),
            )
        else:
            raise TransferExecutionConflictError(
                "成功整理执行检查点缺少可重放 TransferInfo"
            )
        expected_success = outcome is TransferExecutionOutcome.SUCCEEDED
        expected_overwrite_skip = (
            outcome is TransferExecutionOutcome.OVERWRITE_SKIPPED
        )
        if (
                bool(transferinfo.success) != expected_success
                or bool(transferinfo.overwrite_skipped) != expected_overwrite_skip
        ):
            raise TransferExecutionConflictError(
                "整理执行检查点终态与 TransferInfo 不一致"
            )
        task.bind_execution_checkpoint(checkpoint)
        return transferinfo

    def _TransferChain__build_durable_step_runner(
            self,
            task: TransferTask,
            checkpoint: TransferPlanCheckpoint,
    ) -> Optional[_DurableTransferStepRunner]:
        """为非预览持久任务构造绑定计划指纹和当前租约的步骤 runner。"""
        if task.preview:
            return None
        repository = getattr(self, "_transfer_executions", None)
        if repository is None:
            raise RuntimeError("非预览整理缺少 execution repository")
        if not task.admission_task_id or not task.lease_token:
            raise TransferLeaseLostError("整理执行缺少持久任务身份或租约")
        return _DurableTransferStepRunner(
            task_id=task.admission_task_id,
            lease_token=task.lease_token,
            checkpoint_fingerprint=self._TransferChain__transfer_plan_fingerprint(checkpoint),
            repository=repository,
        )

    def _TransferChain__execute_host_transfer_plan(
            self,
            task: TransferTask,
            checkpoint: TransferPlanCheckpoint,
            *,
            source_oper: Any,
            target_oper: Any,
            step_runner: Optional[_DurableTransferStepRunner],
    ) -> Optional[TransferInfo]:
        """通过模块边界执行宿主计划，并只向持久任务注入内部步骤 runner。"""
        if step_runner is None:
            return cast(
                Optional[TransferInfo],
                self.execute_transfer_plan(
                    checkpoint,
                    meta=task.meta,
                    mediainfo=task.mediainfo,
                    source_oper=source_oper,
                    target_oper=target_oper,
                    cleanup_media_file=self._TransferChain__cleanup_transfer_destination,
                ),
            )
        return cast(
            Optional[TransferInfo],
            self.run_module(
                "execute_transfer_plan",
                checkpoint=checkpoint,
                meta=task.meta,
                mediainfo=task.mediainfo,
                source_oper=source_oper,
                target_oper=target_oper,
                cleanup_media_file=self._TransferChain__cleanup_transfer_destination,
                observe_cleanup_media_file=self._TransferChain__observe_cleanup_destination,
                step_runner=step_runner,
            ),
        )

    def _plan_checkpoint_and_execute(
            self,
            task: TransferTask,
            *,
            source_oper: Any = None,
            target_oper: Any = None,
    ) -> TransferInfo:
        """先提交冻结 provider 调用，空结果时再提交并执行宿主计划。"""
        planning_input = task.planning_input or self._TransferChain__build_planning_input(task)
        task.bind_planning_input(planning_input)
        if not task.preview:
            self._TransferChain__claim_task_for_execution(task)
            self._TransferChain__assert_owned_lease(task)
            if getattr(self, "durable_event_writer", None) is None:
                raise RuntimeError(
                    "非预览整理缺少 durable 原子写入端口，拒绝开始外部执行"
                )

        checkpoint = task.plan_checkpoint
        if checkpoint is None:
            try:
                classification_snapshot = effective_classification_snapshot(
                    task.mediainfo
                )
                frozen_providers = tuple(
                    TransferProviderReference(
                        plugin_id=provider.plugin_id,
                        plugin_name=provider.plugin_name,
                        method=provider.method,
                    )
                    for provider in self._module_dispatcher.freeze_plugin_providers(
                        "transfer"
                    )
                )
                if frozen_providers:
                    invocation = self._TransferChain__build_provider_invocation_snapshot(task)
                    checkpoint = TransferPlanCheckpoint(
                        planning_input=planning_input,
                        target_storage="",
                        root_target_path="",
                        final_target_path="",
                        resolved_transfer_type="",
                        items=(),
                        classification_snapshot=classification_snapshot,
                        resolved_meta=invocation.meta,
                        resolved_meta_kind=invocation.meta_kind,
                        resolved_mediainfo=invocation.mediainfo,
                        resolved_mediainfo_kind=invocation.mediainfo_kind,
                        resolved_episodes_info=invocation.episodes_info,
                        legacy_transfer_providers=frozen_providers,
                        provider_invocation=invocation,
                        preview=invocation.preview,
                    )
                else:
                    checkpoint = self._TransferChain__plan_host_transfer(
                        task,
                        planning_input=planning_input,
                        source_oper=source_oper,
                        classification_snapshot=classification_snapshot,
                    )
                checkpoint = self._TransferChain__persist_transfer_checkpoint(
                    task,
                    planning_input=planning_input,
                    checkpoint=checkpoint,
                )
                task.bind_plan_checkpoint(checkpoint)
            except Exception as error:
                self._TransferChain__record_checkpoint_failure(task, error)
                raise

        self._TransferChain__restore_planned_task(task)
        self._TransferChain__assert_owned_lease(task)

        if checkpoint.rejection_error:
            return self._TransferChain__execute_planning_rejection(task, checkpoint)

        step_runner = self._TransferChain__build_durable_step_runner(task, checkpoint)
        try:
            legacy_result = self._TransferChain__execute_legacy_transfer_providers(
                task,
                checkpoint=checkpoint,
                source_oper=source_oper,
                target_oper=target_oper,
                step_runner=step_runner,
            )
        except _TransferRetryExhausted as error:
            assert error.snapshot.checkpoint is not None
            task.bind_execution_checkpoint(error.snapshot.checkpoint)
            return TransferInfo(
                success=False,
                message="整理操作多次失败，请稍后重试",
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=checkpoint.resolved_transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if legacy_result is not None:
            if step_runner is not None:
                task.bind_execution_checkpoint(step_runner.checkpoint(legacy_result))
            return legacy_result

        if checkpoint.is_provider_pending:
            try:
                checkpoint = replace(
                    self._TransferChain__plan_host_transfer(
                        task,
                        planning_input=planning_input,
                        source_oper=source_oper,
                        classification_snapshot=checkpoint.classification_snapshot,
                    ),
                    pre_execution_cleanup_completed=True,
                )
                checkpoint = self._TransferChain__persist_transfer_checkpoint(
                    task,
                    planning_input=planning_input,
                    checkpoint=checkpoint,
                )
                task.bind_plan_checkpoint(checkpoint)
                self._TransferChain__restore_planned_task(task)
                step_runner = self._TransferChain__build_durable_step_runner(task, checkpoint)
            except Exception as error:
                self._TransferChain__record_checkpoint_failure(task, error)
                raise

        self._TransferChain__assert_owned_lease(task)
        try:
            result = self._TransferChain__execute_host_transfer_plan(
                task,
                checkpoint,
                source_oper=source_oper,
                target_oper=target_oper,
                step_runner=step_runner,
            )
        except _TransferRetryExhausted as error:
            assert error.snapshot.checkpoint is not None
            task.bind_execution_checkpoint(error.snapshot.checkpoint)
            return TransferInfo(
                success=False,
                message="整理操作多次失败，请稍后重试",
                fileitem=task.fileitem,
                fail_list=[task.fileitem.path],
                transfer_type=checkpoint.resolved_transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if result is None:
            raise RuntimeError("整理服务没有返回有效结果，请稍后重试")
        if step_runner is not None:
            task.bind_execution_checkpoint(step_runner.checkpoint(result))
        return result

    def _TransferChain__plan_host_transfer(
            self,
            task: TransferTask,
            *,
            planning_input: TransferPlanningInput,
            source_oper: Any,
            classification_snapshot: EffectiveClassificationSnapshot,
    ) -> TransferPlanCheckpoint:
        """在 provider 未接管时生成纯宿主计划，不执行任何文件写入。"""
        try:
            checkpoint = cast(
                Optional[TransferPlanCheckpoint],
                self.plan_transfer(
                    fileitem=task.fileitem,
                    meta=cast(MetaBase, task.meta),
                    mediainfo=cast(Union[MediaInfo, MusicInfo], task.mediainfo),
                    target_directory=task.target_directory,
                    target_storage=task.target_storage,
                    target_path=task.target_path,
                    transfer_type=task.transfer_type,
                    episodes_info=task.episodes_info,
                    scrape=task.scrape,
                    library_type_folder=task.library_type_folder,
                    library_category_folder=task.library_category_folder,
                    source_oper=source_oper,
                    preview=bool(task.preview),
                    planning_input=planning_input,
                ),
            )
        except TransferPlanningRejectedError as error:
            return build_planning_rejection_checkpoint(
                task,
                error=str(error),
                planning_input=planning_input,
                classification_snapshot=classification_snapshot,
            )
        if checkpoint is None:
            raise RuntimeError("整理服务暂时无法生成有效计划，请稍后重试")
        return replace(
            checkpoint,
            classification_snapshot=classification_snapshot,
        )

    def _TransferChain__persist_transfer_checkpoint(
            self,
            task: TransferTask,
            *,
            planning_input: TransferPlanningInput,
            checkpoint: TransferPlanCheckpoint,
    ) -> TransferPlanCheckpoint:
        """提交当前阶段检查点，并只消费仓储回读的持久投影。"""
        if task.preview:
            return checkpoint
        if not task.admission_task_id:
            raise RuntimeError("整理计划提交前缺少持久任务身份")
        self._TransferChain__assert_owned_lease(task)
        assert task.lease_token is not None
        persisted = self._transfer_admissions.checkpoint_plan(
            task_id=task.admission_task_id,
            lease_token=task.lease_token,
            input_fingerprint=planning_input.fingerprint,
            checkpoint=checkpoint,
        )
        if persisted.checkpoint is None:
            raise TransferPlanningStateError("持久投影缺少整理检查点")
        return cast(TransferPlanCheckpoint, persisted.checkpoint)

    def _TransferChain__record_checkpoint_failure(
            self,
            task: TransferTask,
            error: Exception,
    ) -> None:
        """记录当前 checkpoint 阶段失败并保留原状态供重启恢复。"""
        if task.preview or not task.admission_task_id or not task.lease_token:
            return
        try:
            self._transfer_admissions.record_planning_failure(
                task_id=task.admission_task_id,
                lease_token=task.lease_token,
                error=str(error),
            )
            setattr(error, "_transfer_planning_failure_recorded", True)
        except Exception as record_error:
            logger.error(f"记录整理规划失败原因出错：{record_error}")

    def _TransferChain__execute_legacy_transfer_providers(
            self,
            task: TransferTask,
            *,
            checkpoint: TransferPlanCheckpoint,
            source_oper: Any,
            target_oper: Any,
            step_runner: Optional[_DurableTransferStepRunner] = None,
    ) -> Optional[TransferInfo]:
        """在 checkpoint 提交后按冻结身份执行旧插件 transfer provider。"""
        providers = checkpoint.legacy_transfer_providers
        if not providers:
            return None
        invocation = checkpoint.provider_invocation
        if invocation is None:
            raise TransferPlanningStateError(
                "冻结旧 transfer provider 缺少调用快照"
            )
        target_directory = (
            TransferDirectoryConf.model_validate(invocation.target_directory)
            if invocation.target_directory
            else None
        )
        provider_meta = (
            self._TransferChain__restore_meta_snapshot(invocation.meta, invocation.meta_kind)
            if invocation.meta
            else task.meta
        )
        provider_mediainfo = (
            self._TransferChain__restore_mediainfo_snapshot(
                invocation.mediainfo,
                invocation.mediainfo_kind,
            )
            if invocation.mediainfo
            else task.mediainfo
        )
        provider_mediainfo = cast(
            Optional[Union[MediaInfo, MusicInfo]],
            apply_persisted_classification_snapshot(
                provider_mediainfo,
                checkpoint.classification_snapshot,
            ),
        )
        provider_episodes = [
            TmdbEpisode.model_validate(item)
            for item in invocation.episodes_info
        ]
        def invoke_provider_sequence() -> Optional[TransferInfo]:
            """按冻结顺序调用旧 provider，并校验其兼容返回类型。"""
            result = self._module_dispatcher.execute_frozen_plugin_providers(
                "transfer",
                providers,
                before_invoke=lambda: self._TransferChain__prepare_legacy_provider_execution(
                    checkpoint=checkpoint
                ),
                fileitem=FileItem.model_validate(invocation.fileitem),
                meta=provider_meta,
                mediainfo=provider_mediainfo,
                target_directory=target_directory,
                target_storage=invocation.target_storage,
                target_path=(Path(invocation.target_path) if invocation.target_path else None),
                transfer_type=invocation.transfer_type,
                scrape=invocation.scrape,
                library_type_folder=invocation.library_type_folder,
                library_category_folder=invocation.library_category_folder,
                episodes_info=provider_episodes,
                source_oper=source_oper,
                target_oper=target_oper,
                preview=invocation.preview,
            )
            if result is not None and not isinstance(result, TransferInfo):
                raise TypeError("旧插件 transfer provider 返回了不支持的结果类型")
            return result

        def execute_provider_sequence() -> TransferStepResult:
            """执行冻结旧 provider 序列，并冻结是否接管及兼容结果。"""
            result = invoke_provider_sequence()
            return TransferStepResult(payload={
                "handled": result is not None,
                "transferinfo": (
                    result.model_dump(mode="json") if result is not None else None
                ),
            })

        if step_runner is None:
            return invoke_provider_sequence()
        provider_result = step_runner.run(
            phase="provider",
            kind="legacy_transfer_provider_sequence",
            payload={
                "providers": [provider.to_payload() for provider in providers],
                "invocation": invocation.to_payload(),
            },
            execute=execute_provider_sequence,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={
                    "reason": "legacy provider has no stable operation receipt",
                }),
            ),
        )
        if not provider_result.payload.get("handled"):
            return None
        payload = provider_result.payload.get("transferinfo")
        if not isinstance(payload, dict):
            raise TransferPlanningStateError("旧 provider 成功证据缺少整理结果")
        validated_result: TransferInfo = TransferInfo.model_validate(payload)
        return validated_result

    def _TransferChain__prepare_legacy_provider_execution(
            self,
            *,
            checkpoint: TransferPlanCheckpoint,
    ) -> None:
        """按旧批次语义在 provider 副作用前执行冻结 cleanup intent。"""
        invocation = checkpoint.provider_invocation
        if invocation is None:
            raise TransferPlanningStateError("旧 provider 执行缺少调用快照")
        if invocation.preview:
            return

        cleanup_payload = checkpoint.planning_input.options.get(
            "cleanup_dest_fileitem"
        )
        if isinstance(cleanup_payload, dict):
            cleanup_fileitem = FileItem.model_validate(cleanup_payload)
            if not self._TransferChain__cleanup_transfer_destination(cleanup_fileitem):
                raise RuntimeError(
                    f"{cleanup_fileitem.path} 删除失败，整理计划保留待重试"
                )

    def _TransferChain__cleanup_transfer_destination(self, fileitem: FileItem) -> bool:
        """经插件兼容的 StorageChain 幂等删除旧目标并治理父空目录。"""
        storage_chain = self._transfer_storage_chain()
        if not fileitem.path:
            return False
        current_item = storage_chain.get_file_item_strict(
            storage=fileitem.storage,
            path=Path(fileitem.path),
        )
        if current_item is None:
            return True
        return bool(storage_chain.delete_media_file(current_item))

    def _TransferChain__observe_cleanup_destination(self, fileitem: FileItem) -> bool:
        """只读确认统一清理目标已经不存在，查询错误由步骤 runner 隔离。"""
        if not fileitem.path:
            return False
        return self._transfer_storage_chain().get_file_item_strict(
            storage=fileitem.storage,
            path=Path(fileitem.path),
        ) is None

    def execute_legacy_transfer_command(
            self,
            fileitem: FileItem,
            meta: MetaBase,
            mediainfo: Union[MediaInfo, MusicInfo],
            target_directory: Optional[TransferDirectoryConf] = None,
            target_storage: Optional[str] = None,
            target_path: Optional[Path] = None,
            transfer_type: Optional[str] = None,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            source_oper: Any = None,
            target_oper: Any = None,
            preview: bool = False,
    ) -> TransferInfo:
        """把旧同步 ABI 适配为准入、检查点和终态结算共用的唯一命令。"""
        task = TransferTask(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            target_directory=target_directory,
            target_storage=target_storage,
            target_path=target_path,
            transfer_type=transfer_type,
            scrape=scrape,
            library_type_folder=library_type_folder,
            library_category_folder=library_category_folder,
            episodes_info=episodes_info,
            manual=True,
            background=False,
            preview=preview,
        )
        try:
            result = self._plan_checkpoint_and_execute(
                task,
                source_oper=source_oper,
                target_oper=target_oper,
            )
        except Exception as error:
            logger.error(f"旧整理兼容命令执行失败：{error}")
            self._TransferChain__release_task_claim(task, error=str(error))
            return TransferInfo(
                success=False,
                message="整理失败，请稍后重试",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
            )
        if preview:
            return result
        try:
            self._TransferChain__settle_legacy_transfer_result(task, result)
        except Exception as error:
            diagnostic = f"旧整理兼容命令 durable 终态结算失败：{error}"
            logger.error(diagnostic, exc_info=True)
            self._TransferChain__release_task_claim(task, error=diagnostic)
            return TransferInfo(
                success=False,
                message="整理结果确认失败，后台将自动重试",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
            )
        return result
