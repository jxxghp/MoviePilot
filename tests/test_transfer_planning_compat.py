"""整理规划重构必须保持的旧调用与事件兼容合同。"""

from inspect import Parameter, signature
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.application.transfer import TransferTask
from app.chain import ChainBase
from app.chain.transfer import TransferChain
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import EventType


def _assert_signature(callable_object, *parameters: tuple[str, object]) -> None:
    """断言公开参数顺序和默认值，不把测试辅助哨兵泄漏到失败输出。"""
    actual = signature(callable_object).parameters
    assert tuple(actual) == tuple(name for name, _default in parameters)
    for name, expected_default in parameters:
        parameter = actual[name]
        if expected_default is ...:
            assert parameter.default is Parameter.empty
        else:
            assert parameter.default == expected_default


def _fileitem() -> FileItem:
    """构造无需文件系统 I/O 的最小旧整理源对象。"""
    return FileItem(
        storage="local",
        path="/downloads/Movie.2026.mkv",
        type="file",
        name="Movie.2026.mkv",
        basename="Movie.2026",
        extension="mkv",
        size=1024,
        modify_time=1770000000,
        fileid="source-1",
    )


def test_transfer_task_to_dict_keeps_exact_legacy_fields():
    """内部准入和规划快照不得进入插件可见的旧任务字典。"""
    task = TransferTask(
        fileitem=_fileitem(),
        target_storage="local",
        target_path=Path("/library/Movie (2026)"),
        transfer_type="copy",
        scrape=True,
        manual=True,
        background=False,
    )
    task.bind_admission_task_id("task-stable")

    values = task.to_dict()

    assert set(values) == {
        "fileitem",
        "meta",
        "mediainfo",
        "media_source",
        "media_id",
        "mtype",
        "target_directory",
        "target_storage",
        "target_path",
        "transfer_type",
        "scrape",
        "library_type_folder",
        "library_category_folder",
        "episodes_info",
        "username",
        "downloader",
        "download_hash",
        "download_history",
        "transfer_batch_id",
        "manual",
        "background",
        "preview",
    }
    assert values["fileitem"] == task.fileitem.model_dump()
    assert values["target_path"] == Path("/library/Movie (2026)")
    assert "admission_task_id" not in values
    assert "planning_input" not in values
    assert "plan_checkpoint" not in values


def test_transfer_chain_do_transfer_keeps_legacy_signature():
    """公开整理入口必须继续接受原调用方的全部关键字参数。"""
    _assert_signature(
        TransferChain.do_transfer,
        ("self", ...),
        ("fileitem", ...),
        ("meta", None),
        ("mediainfo", None),
        ("mtype", None),
        ("media_source", None),
        ("media_id", None),
        ("target_directory", None),
        ("target_storage", None),
        ("target_path", None),
        ("transfer_type", None),
        ("scrape", None),
        ("library_type_folder", None),
        ("library_category_folder", None),
        ("season", None),
        ("epformat", None),
        ("min_filesize", 0),
        ("downloader", None),
        ("download_hash", None),
        ("force", False),
        ("background", True),
        ("manual", False),
        ("preview", False),
        ("sync_extra_files", False),
        ("cleanup_dest_fileitem", None),
        ("continue_callback", None),
        ("reorganize", False),
    )


def test_chain_base_transfer_keeps_legacy_signature():
    """仅 Chain 对外兼容层保留旧整理参数，宿主模块不再重复导出。"""
    _assert_signature(
        ChainBase.transfer,
        ("self", ...),
        ("fileitem", ...),
        ("meta", ...),
        ("mediainfo", ...),
        ("target_directory", None),
        ("target_storage", None),
        ("target_path", None),
        ("transfer_type", None),
        ("scrape", None),
        ("library_type_folder", None),
        ("library_category_folder", None),
        ("episodes_info", None),
        ("source_oper", None),
        ("target_oper", None),
        ("preview", False),
    )


def test_chain_base_transfer_delegates_exactly_to_injected_command():
    """旧 ABI 必须原样调用注入命令，不得再次进入动态模块调度。"""
    chain = object.__new__(ChainBase)
    result = TransferInfo(success=True, fileitem=_fileitem(), transfer_type="copy")
    command = Mock(return_value=result)
    chain._legacy_transfer_command = command
    chain.run_module = Mock(side_effect=AssertionError("不得调用 run_module"))
    meta = Mock(name="meta")
    mediainfo = Mock(name="mediainfo")
    target_directory = Mock(name="target_directory")
    source_oper = Mock(name="source_oper")
    target_oper = Mock(name="target_oper")
    episodes_info = [Mock(name="episode")]

    returned = chain.transfer(
        fileitem=result.fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        target_storage="alist",
        target_path=Path("/library/Movie (2026)"),
        transfer_type="copy",
        scrape=True,
        library_type_folder=False,
        library_category_folder=True,
        episodes_info=episodes_info,
        source_oper=source_oper,
        target_oper=target_oper,
        preview=True,
    )

    assert returned is result
    command.assert_called_once_with(
        fileitem=result.fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        target_path=Path("/library/Movie (2026)"),
        target_storage="alist",
        transfer_type="copy",
        scrape=True,
        library_type_folder=False,
        library_category_folder=True,
        episodes_info=episodes_info,
        source_oper=source_oper,
        target_oper=target_oper,
        preview=True,
    )
    chain.run_module.assert_not_called()


def test_chain_base_plan_transfer_keeps_internal_dto_type_only():
    """规划入口运行时只做内部调度，不要求导入或重复导出 DTO。"""
    chain = object.__new__(ChainBase)
    checkpoint = Mock(name="checkpoint")
    chain.run_module = Mock(return_value=checkpoint)
    meta = Mock(name="meta")
    mediainfo = Mock(name="mediainfo")

    returned = chain.plan_transfer(
        fileitem=_fileitem(),
        meta=meta,
        mediainfo=mediainfo,
    )

    assert returned is checkpoint
    chain.run_module.assert_called_once_with(
        "plan_transfer",
        fileitem=_fileitem(),
        meta=meta,
        mediainfo=mediainfo,
        target_directory=None,
        target_path=None,
        target_storage=None,
        transfer_type=None,
        scrape=None,
        library_type_folder=None,
        library_category_folder=None,
        episodes_info=None,
        source_oper=None,
        preview=False,
        planning_input=None,
    )


def test_filemanager_module_has_no_legacy_transfer_provider():
    """FileManager 宿主只暴露规划与执行阶段，不再注册旧 transfer provider。"""
    assert not hasattr(FileManagerModule, "transfer")
    assert not hasattr(TransHandler, "transfer_media")
    assert callable(FileManagerModule.plan_transfer)
    assert callable(FileManagerModule.execute_transfer_plan)


@pytest.mark.parametrize(
    ("event_type", "success"),
    [
        (EventType.TransferComplete, True),
        (EventType.TransferFailed, False),
    ],
)
def test_transfer_result_event_keeps_single_exact_legacy_payload(event_type, success):
    """规划与回放不得重复发送结果事件或改变插件读取的 payload。"""
    chain = object.__new__(TransferChain)
    chain.eventmanager = Mock()
    task = TransferTask(
        fileitem=_fileitem(),
        downloader="qbittorrent",
        download_hash="download-1",
    )
    transferinfo = TransferInfo(
        success=success,
        fileitem=task.fileitem,
        transfer_type="copy",
        message="" if success else "copy failed",
    )
    payload = chain._transfer_result_payload(
        task,
        transferinfo,
        history_id=42,
    )

    chain._publish_transfer_result(event_type, payload)

    chain.eventmanager.send_event.assert_called_once_with(event_type, payload)
    assert set(payload) == {
        "fileitem",
        "meta",
        "mediainfo",
        "transferinfo",
        "downloader",
        "download_hash",
        "transfer_history_id",
    }
    assert payload["fileitem"] is task.fileitem
    assert payload["transferinfo"] is transferinfo
    assert payload["downloader"] == "qbittorrent"
    assert payload["download_hash"] == "download-1"
    assert payload["transfer_history_id"] == 42
