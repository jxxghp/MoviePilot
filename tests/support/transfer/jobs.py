"""
整理任务、检查点、元数据及即时执行载具；不注册测试用例。
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from app.application.transfer.execution import TransferExecutionCheckpoint, TransferSettlementResult
from app.application.transfer.workflow import JobManager, TransferAdmission, TransferTask
from app.chain.transfer.facade import TransferChain
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.config import settings
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType


class FakeMeta(MetaBase):
    """
    整理任务分组需要的最小剧集元数据。

    继承 MetaBase 而非鸭子类型：TransferTask.meta 已标注为 MetaBase，pydantic 会做
    isinstance 校验。下面三个类级属性用来遮蔽 MetaBase 上的同名 property，好让
    __init__ 能直接赋值。

    :ivar name: 测试剧集名称，遮蔽只读基类属性
    :ivar episode_list: 当前任务覆盖的集数列表
    :ivar season_episode: 用于断言作业分组的季集标识
    """

    name = None
    episode_list = None
    season_episode = None

    def __init__(self, episode: int, season: int = 1):
        """
        初始化固定季集解析结果。
        """
        super().__init__(title=f"Test Show S{season:02d}E{episode:02d}")
        self.name = "Test Show"
        self.title = f"Test Show S{season:02d}E{episode:02d}"
        self.year = "2026"
        self.type = MediaType.TV
        self.begin_season = season
        self.end_season = None
        self.total_season = 1
        self.begin_episode = episode
        self.end_episode = None
        self.total_episode = 1
        self.episode_list = [episode]
        self.season_episode = f"S01E{episode:02d}"
        self.part = None

    @property
    def season(self):
        """
        返回用于作业分组的季标识。
        """
        return f"S{self.begin_season:02d}"

    @property
    def episode(self):
        """
        返回用于整理命名的集标识。
        """
        return f"E{self.begin_episode:02d}"

    def to_dict(self):
        """
        按历史事件契约导出测试元数据。
        """
        return {
            "title": self.title,
            "name": self.name,
            "year": self.year,
            "type": self.type.value,
            "begin_season": self.begin_season,
            "end_season": self.end_season,
            "total_season": self.total_season,
            "begin_episode": self.begin_episode,
            "end_episode": self.end_episode,
            "total_episode": self.total_episode,
            "season_episode": self.season_episode,
            "episode_list": self.episode_list,
            "part": self.part,
        }


class FakeMedia(MediaInfo):
    """
    与正式 MediaInfo 身份字段一致的测试媒体对象。

    继承 MediaInfo 而非鸭子类型：TransferTask.mediainfo 已标注为 MusicInfo | MediaInfo，
    pydantic 会做 isinstance 校验。
    """

    def __init__(self, tmdb_id: int = 12345):
        """
        构造与正式 MediaInfo 身份字段一致的测试媒体对象。
        """
        super().__init__()
        self.tmdb_id = tmdb_id
        self.douban_id = None
        self.bangumi_id = None
        self.anilist_id = None
        self.media_source = MediaSource.TMDB
        self.media_id = str(tmdb_id)
        self.type = MediaType.TV
        self.title_year = "Test Show (2026)"

    def clear(self):
        """
        模拟正式媒体对象的清理接口。
        """
        pass

    def to_dict(self):
        """
        返回测试媒体对象的序列化字段。
        """
        return {
            "type": MediaType.TV.value,
            "title": "Test Show",
            "year": "2026",
            "title_year": "Test Show (2026)",
            "media_source": self.media_source,
            "media_id": self.media_id,
            "tmdb_id": self.tmdb_id,
            "douban_id": self.douban_id,
            "bangumi_id": self.bangumi_id,
            "anilist_id": self.anilist_id,
        }


def make_task(episode: int, season: int = 1) -> TransferTask:
    """
    构造单集整理任务及其源文件。
    """
    name = f"Test.Show.S{season:02d}E{episode:02d}.mkv"
    return TransferTask(
        fileitem=FileItem(
            storage="local",
            path=f"/downloads/Test Show/{name}",
            type="file",
            name=name,
            basename=name.removesuffix(".mkv"),
            extension="mkv",
            size=1024,
        ),
        meta=FakeMeta(episode),
    )


def make_transfer_chain() -> TransferChain:
    """
    构造带内存 durable admission 契约的整理链测试骨架。
    """
    chain = object.__new__(TransferChain)
    chain.transfer_history_repository = MagicMock()
    chain.download_history_repository = MagicMock()
    chain.transfer_execution_repository = MagicMock()
    chain.jobview = JobManager()
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._subtitle_exts = settings.RMT_SUBEXT
    chain._lyrics_exts = settings.RMT_LYRICSEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._allowed_exts = (
        chain._media_exts + chain._audio_exts + chain._subtitle_exts + chain._lyrics_exts
    )
    chain._success_target_files = {}
    chain._scrape_batches = {}
    admissions = MagicMock()
    admissions_by_identity = {}
    admissions_by_id = {}

    def admit(*, storage, src_path, planning_input=None, replace_inactive=False):
        """
        按源身份幂等返回测试用 durable admission。
        """
        del replace_inactive
        identity = storage, src_path
        existing = admissions_by_identity.get(identity)
        if existing is not None:
            return existing
        admission = TransferAdmission(
            task_id=f"test-task-{len(admissions_by_id) + 1}",
            storage=storage,
            src_path=src_path,
            state="accepted",
            created_at="2026-08-27 10:00:00",
            updated_at="2026-08-27 10:00:00",
            planning_input=planning_input,
        )
        admissions_by_identity[identity] = admission
        admissions_by_id[admission.task_id] = admission
        return admission

    def claim_task(*, task_id, owner_id, lease_seconds):
        """
        为测试任务返回唯一 token，并保留正式 claim 的参数约束。
        """
        assert lease_seconds > 0
        admission = admissions_by_id[task_id]
        claimed = replace(
            admission,
            lease_owner=owner_id,
            lease_token=f"lease-{task_id}",
            lease_expires_at="2026-08-27 10:02:00.000000",
            heartbeat_at="2026-08-27 10:00:00.000000",
            attempt_count=admission.attempt_count + 1,
        )
        admissions_by_identity[(claimed.storage, claimed.src_path)] = claimed
        admissions_by_id[task_id] = claimed
        return claimed

    def checkpoint_plan(*, task_id, lease_token, input_fingerprint, checkpoint):
        """
        回读带检查点的持久投影，供同步整理测试执行真实编排。
        """
        del input_fingerprint
        admission = admissions_by_id[task_id]
        assert admission.lease_token == lease_token
        planned = replace(
            admission,
            state="provider_pending" if checkpoint.is_provider_pending else "planned",
            checkpoint=checkpoint,
        )
        admissions_by_identity[(planned.storage, planned.src_path)] = planned
        admissions_by_id[task_id] = planned
        return planned

    admissions.admit.side_effect = admit
    admissions.get_task_id_by_identity.side_effect = lambda *, storage, src_path: (
        admissions_by_identity[(storage, src_path)].task_id
        if (storage, src_path) in admissions_by_identity else None
    )
    admissions.claim_task.side_effect = claim_task
    admissions.checkpoint_plan.side_effect = checkpoint_plan
    admissions.abandon_unstarted.return_value = 1
    admissions.release_claim.return_value = True
    chain._transfer_admissions = admissions
    chain.transfer_admission_repository = admissions
    chain._TransferChain__ensure_lease_heartbeat_owner = MagicMock()

    class ImmediateStepRunner:
        """
        为 JobManager 测试提交纯内存步骤与聚合执行检查点。
        """

        def run(self, *, phase, kind, payload, execute, observe):
            """
            立即执行确定性测试步骤，不使用恢复探测。
            """
            del phase, kind, payload, observe
            return execute()

        def checkpoint(self, transferinfo):
            """
            把测试整理结果冻结为可供 task-aware writer 使用的检查点。
            """
            return TransferExecutionCheckpoint.create(
                payload={
                    "outcome": "succeeded" if transferinfo.success else "failed",
                    "transferinfo": transferinfo.model_dump(mode="json"),
                },
                operation_ids=("job-test-operation",),
            )

    step_runner = ImmediateStepRunner()
    chain._TransferChain__build_durable_step_runner = MagicMock(
        return_value=step_runner
    )

    def transfer_result(**kwargs):
        """
        执行测试历史暂存与发布，并返回已删除 pending 的原子回执。
        """
        staging = SimpleNamespace(
            get_success_by_src=lambda *_args, **_kwargs: SimpleNamespace(
                id=99,
                status=True,
            )
        )
        history = kwargs["stage_history"](staging)
        if kwargs["publish"] is not None:
            kwargs["publish"](kwargs["event_payload"])
        settlement = kwargs["settlement"]
        if settlement.outcome == "succeeded":
            completed = admissions_by_id.pop(settlement.task_id, None)
            if completed is not None:
                admissions_by_identity.pop((completed.storage, completed.src_path), None)
        return TransferSettlementResult(
            history_id=getattr(history, "id", 1) if history is not None else 1,
            settlement_revision=1,
            pending_deleted=True,
        )

    chain.durable_event_writer = MagicMock()
    chain.durable_event_writer.transfer_result.side_effect = transfer_result
    return chain


def bind_terminal_checkpoint(
        task: TransferTask,
        transferinfo: TransferInfo,
) -> None:
    """
    为直接回调测试绑定 task identity、lease 与聚合执行检查点。
    """
    task_id = f"terminal-{abs(hash(task.fileitem.path))}"
    task.bind_admission_task_id(task_id)
    task.bind_execution_lease(
        owner_id="job-test-owner",
        lease_token=f"lease-{task_id}",
    )
    task.bind_execution_checkpoint(TransferExecutionCheckpoint.create(
        payload={
            "outcome": (
                "overwrite_skipped"
                if transferinfo.overwrite_skipped
                else "succeeded" if transferinfo.success else "failed"
            ),
            "transferinfo": transferinfo.model_dump(mode="json"),
        },
        operation_ids=("job-test-operation",),
    ))


def make_fileitem(path: str, size: int = 1024) -> FileItem:
    """
    构造带路径和大小的本地测试文件项。
    """
    file_path = path
    name = file_path.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    basename = name[: -(len(suffix) + 1)] if suffix else name
    return FileItem(
        storage="local",
        path=file_path,
        type="file",
        name=name,
        basename=basename,
        extension=suffix,
        size=size,
    )
