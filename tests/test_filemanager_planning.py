from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
)
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.filemanager import transhandler as transhandler_module
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.runtime.events import eventmanager
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import ChainEventType, MediaType
from app.schemas.workflow import FileItem


class ReadOnlyTreeStorage:
    """提供目录只读遍历，并让任何写接口调用立即失败。"""

    def __init__(self, children: dict[str, list[FileItem]]):
        """保存按源目录路径索引的测试树。"""
        self.children = children
        self.reads: list[str] = []

    def list(self, fileitem: FileItem) -> list[FileItem]:
        """返回目录孩子并记录只读访问。"""
        self.reads.append(fileitem.path)
        return self.children.get(fileitem.path, [])

    def __getattr__(self, name: str):
        """拒绝规划期意外访问的存储接口。"""
        raise AssertionError(f"规划阶段不应访问存储接口：{name}")


class RecordingStorage:
    """记录执行期存储调用顺序并模拟同一网盘复制。"""

    def __init__(self, calls: list[str], existing: FileItem = None):
        """保存调用日志与可选同名目标。"""
        self.calls = calls
        self.existing = existing

    def get_item_strict(self, path: Path):
        """记录严格目标查询。"""
        self.calls.append("strict")
        return self.existing

    def get_item(self, path: Path):
        """记录普通目标查询。"""
        self.calls.append("get_item")
        return self.existing

    def get_folder(self, path: Path) -> FileItem:
        """记录可能创建目录的接口。"""
        self.calls.append("get_folder")
        return FileItem(
            storage="alist",
            path=path.as_posix(),
            name=path.name,
            type="dir",
        )

    def delete(self, fileitem: FileItem) -> bool:
        """记录删除副作用。"""
        self.calls.append("delete")
        self.existing = None
        return True

    def is_support_transtype(self, transfer_type: str) -> bool:
        """声明测试存储支持复制。"""
        return transfer_type == "copy"

    def copy(self, fileitem: FileItem, path: Path, name: str) -> bool:
        """记录复制副作用。"""
        self.calls.append(f"copy:{(path / name).as_posix()}")
        return True


def _build_media() -> tuple[MetaBase, MediaInfo]:
    """构造文件规划所需的最小电视剧领域对象。"""
    meta = MetaBase("Test.Show.S01E01.mkv")
    meta.type = MediaType.TV
    meta.name = "Test Show"
    meta.year = "2026"
    meta.begin_season = 1
    meta.begin_episode = 1
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Test Show",
        year="2026",
        tmdb_id=12345,
    )
    return meta, mediainfo


def _build_fileitem() -> FileItem:
    """构造无需访问宿主文件系统的网盘源文件。"""
    return FileItem(
        storage="alist",
        path="/downloads/Test.Show.S01E01.mkv",
        name="Test.Show.S01E01.mkv",
        basename="Test.Show.S01E01",
        extension="mkv",
        type="file",
        size=1024,
    )


def _build_input(fileitem: FileItem, **overrides) -> TransferPlanningInput:
    """构造可持久化规划输入。"""
    values = {
        "source_fileitem": fileitem.model_dump(mode="json"),
        "target_storage": "alist",
        "target_path": "/library",
        "requested_transfer_type": "copy",
        "need_rename": True,
        "overwrite_mode": "always",
    }
    values.update(overrides)
    return TransferPlanningInput(**values)


def _plan_file(
    handler: TransHandler,
    planning_input: TransferPlanningInput,
    meta: MetaBase,
    mediainfo: MediaInfo,
    source_oper,
):
    """使用测试固定策略规划单个文件。"""
    return handler.plan_transfer(
        planning_input,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=source_oper,
        target_storage="alist",
        target_path=Path("/library"),
        transfer_type="copy",
        need_scrape=False,
        need_rename=True,
        need_notify=True,
        overwrite_mode="always",
        episodes_info=None,
        preview=False,
    )


def test_directory_planning_only_reads_source_and_freezes_ordered_leaf_operations():
    root = FileItem(storage="alist", path="/source/disc", name="disc", type="dir")
    nested = FileItem(storage="alist", path="/source/disc/BDMV", name="BDMV", type="dir")
    first = FileItem(
        storage="alist",
        path="/source/disc/BDMV/index.bdmv",
        name="index.bdmv",
        type="file",
        extension="bdmv",
    )
    second = FileItem(
        storage="alist",
        path="/source/disc/MovieObject.bdmv",
        name="MovieObject.bdmv",
        type="file",
        extension="bdmv",
    )
    storage = ReadOnlyTreeStorage(
        {
            root.path: [nested, second],
            nested.path: [first],
        }
    )
    meta, mediainfo = _build_media()
    planning_input = _build_input(root, need_rename=False)

    checkpoint = TransHandler().plan_transfer(
        planning_input,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=storage,
        target_storage="alist",
        target_path=Path("/library"),
        transfer_type="copy",
        need_scrape=False,
        need_rename=False,
        need_notify=True,
        overwrite_mode="never",
        episodes_info=None,
        preview=False,
    )

    assert storage.reads == [root.path, nested.path]
    assert [item.sequence for item in checkpoint.items] == [0, 1]
    assert [item.source_fileitem["path"] for item in checkpoint.items] == [
        first.path,
        second.path,
    ]
    assert [item.target_path for item in checkpoint.items] == [
        "/library/disc/BDMV/index.bdmv",
        "/library/disc/MovieObject.bdmv",
    ]


def test_execute_uses_frozen_target_and_intercepts_before_directory_or_delete(
    monkeypatch,
):
    calls: list[str] = []
    fileitem = _build_fileitem()
    meta, mediainfo = _build_media()

    def record_event(event_type, event_data):
        """记录规划和执行事件顺序。"""
        calls.append(event_type.value)
        return None

    monkeypatch.setattr(eventmanager, "send_event", record_event)
    handler = TransHandler()
    checkpoint = _plan_file(
        handler,
        _build_input(fileitem),
        meta,
        mediainfo,
        ReadOnlyTreeStorage({}),
    )
    frozen_target = checkpoint.final_target_path
    assert calls == [
        ChainEventType.TransferRenameBuild.value,
        ChainEventType.TransferRename.value,
    ]
    calls.clear()
    original_get_runtime_setting = transhandler_module.get_runtime_setting

    def drifted_runtime_setting(name: str):
        """让执行期重新读取重命名配置时立即失败。"""
        if name == "RENAME_FORMAT":
            raise AssertionError("执行冻结计划不应重新读取重命名配置")
        return original_get_runtime_setting(name)

    monkeypatch.setattr(
        transhandler_module,
        "get_runtime_setting",
        drifted_runtime_setting,
    )
    existing = FileItem(
        storage="alist",
        path=frozen_target,
        name=Path(frozen_target).name,
        type="file",
        extension="mkv",
        size=100,
    )
    storage = RecordingStorage(calls, existing=existing)

    result = handler.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=storage,
        target_oper=storage,
    )

    assert result.success is True
    assert ChainEventType.TransferRenameBuild.value not in calls
    assert ChainEventType.TransferRename.value not in calls
    assert ChainEventType.TransferOverwriteCheck.value in calls
    intercept_index = calls.index(ChainEventType.TransferIntercept.value)
    assert intercept_index < calls.index("get_folder")
    assert intercept_index < calls.index("delete")
    assert f"copy:{frozen_target}" in calls


def test_module_preserves_admission_input_while_freezing_resolved_directory():
    fileitem = _build_fileitem()
    meta, mediainfo = _build_media()
    admission_input = _build_input(
        fileitem,
        target_storage=None,
        target_path=None,
        requested_transfer_type=None,
        target_directory={"name": "library"},
    )
    fingerprint = admission_input.fingerprint
    target_directory = TransferDirectoryConf(
        name="library",
        transfer_type="copy",
        overwrite_mode="never",
        library_path="/resolved-library",
        library_storage="alist",
        renaming=True,
        scraping=False,
        notify=True,
    )

    checkpoint = FileManagerModule().plan_transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=ReadOnlyTreeStorage({}),
        planning_input=admission_input,
    )

    assert checkpoint.planning_input is admission_input
    assert checkpoint.planning_input.fingerprint == fingerprint
    assert checkpoint.root_target_path == "/resolved-library"
    assert checkpoint.final_target_path.startswith("/resolved-library/")
    assert checkpoint.resolved_meta_kind == "MetaBase"
    assert checkpoint.resolved_meta["begin_episode"] == 1
    assert checkpoint.resolved_mediainfo_kind == "MediaInfo"
    assert checkpoint.resolved_mediainfo["title"] == "Test Show"


def _build_cleanup_checkpoint(monkeypatch, calls: list[str]):
    """构造带冻结旧目标清理意图的单文件检查点。"""
    fileitem = _build_fileitem()
    meta, mediainfo = _build_media()
    cleanup_item = FileItem(
        storage="cleanup",
        path="/old-library/old.mkv",
        name="old.mkv",
        type="file",
        extension="mkv",
    )
    planning_input = _build_input(
        fileitem,
        options={"cleanup_dest_fileitem": cleanup_item.model_dump(mode="json")},
    )

    def record_event(event_type, event_data):
        """记录执行事件顺序。"""
        calls.append(event_type.value)
        return None

    monkeypatch.setattr(eventmanager, "send_event", record_event)
    checkpoint = _plan_file(
        TransHandler(),
        planning_input,
        meta,
        mediainfo,
        ReadOnlyTreeStorage({}),
    )
    calls.clear()
    return checkpoint, meta, mediainfo, cleanup_item


def test_cleanup_runs_after_intercept_and_before_transfer_side_effects(monkeypatch):
    calls: list[str] = []
    checkpoint, meta, mediainfo, cleanup_item = _build_cleanup_checkpoint(
        monkeypatch,
        calls,
    )
    transfer_storage = RecordingStorage(calls)
    module = FileManagerModule()
    cleaned_items: list[FileItem] = []

    def cleanup_media_file(fileitem: FileItem) -> bool:
        """模拟含目录保护与插件路由的统一删除兼容能力。"""
        calls.append("cleanup_compat")
        cleaned_items.append(fileitem)
        return True

    result = module.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=transfer_storage,
        target_oper=transfer_storage,
        cleanup_media_file=cleanup_media_file,
    )

    assert result.success is True
    intercept_index = calls.index(ChainEventType.TransferIntercept.value)
    assert intercept_index < calls.index("cleanup_compat")
    assert calls.index("cleanup_compat") < calls.index("get_folder")
    assert calls.index("cleanup_compat") < next(
        index for index, call in enumerate(calls) if call.startswith("copy:")
    )
    assert cleaned_items == [cleanup_item]


def test_cleanup_compatibility_capability_can_report_idempotent_success(monkeypatch):
    calls: list[str] = []
    checkpoint, meta, mediainfo, _ = _build_cleanup_checkpoint(monkeypatch, calls)
    transfer_storage = RecordingStorage(calls)
    module = FileManagerModule()

    def cleanup_media_file(fileitem: FileItem) -> bool:
        """统一能力将目标不存在归一为幂等成功。"""
        calls.append("cleanup_compat_missing")
        return True

    result = module.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=transfer_storage,
        target_oper=transfer_storage,
        cleanup_media_file=cleanup_media_file,
    )

    assert result.success is True
    assert "cleanup_compat_missing" in calls


def test_replayed_checkpoint_skips_cleanup_already_completed_before_provider(
    monkeypatch,
):
    """provider 前已完成 cleanup 的持久检查点在宿主重放时不得再次删除。"""
    calls: list[str] = []
    checkpoint, meta, mediainfo, _ = _build_cleanup_checkpoint(monkeypatch, calls)
    persisted_checkpoint = TransferPlanCheckpoint.from_payload(
        replace(
            checkpoint,
            pre_execution_cleanup_completed=True,
        ).to_payload()
    )
    transfer_storage = RecordingStorage(calls)

    def unexpected_cleanup(_fileitem: FileItem) -> bool:
        """若持久完成事实未被消费则立即暴露重复清理。"""
        raise AssertionError("已完成的 provider 前 cleanup 不应在宿主重放时重复执行")

    result = FileManagerModule().execute_transfer_plan(
        persisted_checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=transfer_storage,
        target_oper=transfer_storage,
        cleanup_media_file=unexpected_cleanup,
    )

    assert result.success is True
    assert ChainEventType.TransferIntercept.value in calls
    assert any(call.startswith("copy:") for call in calls)


def test_cleanup_failure_raises_before_directory_or_copy(monkeypatch):
    calls: list[str] = []
    checkpoint, meta, mediainfo, cleanup_item = _build_cleanup_checkpoint(
        monkeypatch,
        calls,
    )
    transfer_storage = RecordingStorage(calls)
    module = FileManagerModule()

    def cleanup_media_file(fileitem: FileItem) -> bool:
        """模拟统一删除能力执行保护治理后的失败结果。"""
        calls.append("cleanup_compat_failed")
        return False

    with pytest.raises(RuntimeError, match="整理计划保留待重试"):
        module.execute_transfer_plan(
            checkpoint,
            meta=meta,
            mediainfo=mediainfo,
            source_oper=transfer_storage,
            target_oper=transfer_storage,
            cleanup_media_file=cleanup_media_file,
        )

    assert ChainEventType.TransferIntercept.value in calls
    assert "cleanup_compat_failed" in calls
    assert "get_folder" not in calls
    assert not any(call.startswith("copy:") for call in calls)


def test_empty_directory_plan_is_zero_operation_without_intercept_or_cleanup(
    monkeypatch,
):
    calls: list[str] = []
    root = FileItem(
        storage="alist",
        path="/downloads/empty",
        name="empty",
        type="dir",
    )
    cleanup_item = FileItem(
        storage="alist",
        path="/library/old.mkv",
        name="old.mkv",
        type="file",
    )
    planning_input = _build_input(
        root,
        need_rename=False,
        options={"cleanup_dest_fileitem": cleanup_item.model_dump(mode="json")},
    )
    meta, mediainfo = _build_media()
    checkpoint = TransHandler().plan_transfer(
        planning_input,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=ReadOnlyTreeStorage({root.path: []}),
        target_storage="alist",
        target_path=Path("/library"),
        transfer_type="copy",
        need_scrape=False,
        need_rename=False,
        need_notify=True,
        overwrite_mode="never",
        episodes_info=None,
        preview=False,
    )
    assert checkpoint.items == ()

    def unexpected_event(event_type, event_data):
        """空计划若触发事件则立即失败。"""
        raise AssertionError(f"空计划不应触发事件：{event_type}")

    monkeypatch.setattr(eventmanager, "send_event", unexpected_event)
    result = FileManagerModule().execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=RecordingStorage(calls),
        target_oper=RecordingStorage(calls),
        cleanup_media_file=lambda _fileitem: calls.append("cleanup") or True,
    )

    assert result.success is True
    assert calls == []


def test_directory_intercept_preserves_legacy_payload(monkeypatch):
    calls: list[str] = []
    root = FileItem(
        storage="alist",
        path="/downloads/disc",
        name="disc",
        type="dir",
    )
    child = FileItem(
        storage="alist",
        path="/downloads/disc/index.bdmv",
        name="index.bdmv",
        type="file",
        extension="bdmv",
    )
    meta, mediainfo = _build_media()
    checkpoint = TransHandler().plan_transfer(
        _build_input(root, need_rename=False),
        meta=meta,
        mediainfo=mediainfo,
        source_oper=ReadOnlyTreeStorage({root.path: [child]}),
        target_storage="alist",
        target_path=Path("/library"),
        transfer_type="copy",
        need_scrape=False,
        need_rename=False,
        need_notify=True,
        overwrite_mode="never",
        episodes_info=None,
        preview=False,
    )
    intercept_payloads = []

    def capture_event(event_type, event_data):
        """捕获目录根拦截事件。"""
        if event_type == ChainEventType.TransferIntercept:
            intercept_payloads.append(event_data)
        return None

    monkeypatch.setattr(eventmanager, "send_event", capture_event)
    result = TransHandler().execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=RecordingStorage(calls),
        target_oper=RecordingStorage(calls),
    )

    assert result.success is True
    assert len(intercept_payloads) == 1
    payload = intercept_payloads[0]
    assert payload.meta is None
    assert payload.options is None
    assert "meta" not in payload.model_fields_set
    assert "options" not in payload.model_fields_set


def test_directory_intercept_step_uses_frozen_source_after_size_projection():
    """目录执行期修正根目录大小时，持久拦截意图仍使用冻结源快照。"""
    root = FileItem(
        storage="alist",
        path="/downloads/disc",
        name="disc",
        type="dir",
    )
    stream = FileItem(
        storage="alist",
        path="/downloads/disc/BDMV/STREAM/00001.m2ts",
        name="00001.m2ts",
        type="file",
        extension="m2ts",
        size=123,
    )
    planning_input = TransferPlanningInput(
        source_fileitem=root.model_dump(mode="json"),
        target_storage="alist",
        target_path="/library",
        requested_transfer_type="copy",
        need_rename=False,
    )
    checkpoint = TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="alist",
        root_target_path="/library/disc",
        final_target_path="/library/disc",
        resolved_transfer_type="copy",
        items=(TransferPlanItem(
            sequence=0,
            source_fileitem=stream.model_dump(mode="json"),
            target_storage="alist",
            target_path="/library/disc/BDMV/STREAM/00001.m2ts",
        ),),
        need_rename=False,
        overwrite_mode="never",
    )
    runner = Mock()
    steps = []

    def run_step(*, phase, kind, payload, execute, observe):
        """模拟 durable runner，并记录其接受的冻结步骤意图。"""
        steps.append((phase, kind, payload))
        return execute()

    runner.run.side_effect = run_step
    storage = RecordingStorage([])

    result = TransHandler().execute_transfer_plan(
        checkpoint,
        meta=_build_media()[0],
        mediainfo=_build_media()[1],
        source_oper=storage,
        target_oper=storage,
        step_runner=runner,
    )

    assert result.success is True
    intercept = next(
        payload
        for _phase, kind, payload in steps
        if kind == "plugin_transfer_intercept"
    )
    assert intercept["source"] == planning_input.source_fileitem
