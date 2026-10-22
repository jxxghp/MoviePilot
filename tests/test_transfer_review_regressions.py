"""
验证整理恢复、目录覆盖和上下文传递的审查回归。
"""

import os
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.network.http import RequestUtils
from app.api.endpoints.transferhistory import restore_manual_transfer_history_batch
from app.application.transfer.execution import TransferExecutionConflictError, TransferExecutionState
from app.application.transfer.history import success_fields
from app.application.transfer.models import TransferPlanCheckpoint, TransferPlanItem, TransferPlanningInput
from app.chain.scraping import ScrapingChain
from app.chain.transfer.execution import _TransferManualReviewRequired
from app.domain.context import MediaInfo
from app.domain.meta.metavideo import MetaVideo
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.storages.alist import Alist
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.file import FileItem
from app.schemas.transfer import ManualTransferItem, TransferInfo
from app.schemas.types import MediaType
from tests.test_transfer_safe_overwrite import _checkpoint, _durable_runner, _execute
from tests.test_transfer_safe_overwrite import repository_factory as _repository_factory


@pytest.fixture
def repository_factory():
    """
    复用真实 SQLite 整理账本载具，每个用例结束时关闭独立引擎。
    """
    yield from _repository_factory.__wrapped__()


def test_success_history_preserves_episode_group():
    """
    整理成功历史应与失败历史一样保存自定义剧集组。
    """
    media = MediaInfo(type=MediaType.TV, title="Show", episode_group="group")
    result = success_fields(FileItem(path="/source"), "copy", MetaVideo("Show.S01E01"), media, TransferInfo())
    assert result["episode_group"] == "group"


@pytest.mark.parametrize("groups, expected", [(["group", "group"], "group"), (["a", "b"], None)])
def test_batch_retransfer_restores_only_common_episode_group(groups, expected):
    """
    批量重整只复用共同剧集组，混合批次不误用单条记录。
    """
    histories = [SimpleNamespace(status=True, mode="copy", src_fileitem={"path": f"/{index}"},
                                 type="电视剧", media_source="themoviedb", media_id="123",
                                 music_type=None, downloader=None, download_hash=None, episode_group=group)
                 for index, group in enumerate(groups)]
    request = ManualTransferItem(logids=list(range(len(groups))), from_history=True)
    result = restore_manual_transfer_history_batch(request, SimpleNamespace(get=lambda identifier: histories[identifier]))
    assert result[-1] is None
    assert request.episode_group == expected


def test_episode_scraping_preserves_explicit_source(monkeypatch):
    """
    单集重新识别后继续使用本次请求指定的刮削来源。
    """
    media = MediaInfo(type=MediaType.TV, media_source="themoviedb", media_id="123", scrape_source="imdb")
    recognized = MediaInfo(type=MediaType.TV, media_source="themoviedb", media_id="123")
    monkeypatch.setattr("app.chain.scraping.MediaChain", lambda: SimpleNamespace(recognize_media=lambda **_kwargs: recognized))
    chain = object.__new__(ScrapingChain)
    nfo, images = Mock(), Mock()
    monkeypatch.setattr(chain, "_scrape_nfo_generic", nfo)
    monkeypatch.setattr(chain, "_scrape_images_generic", images)
    chain._handle_tv_episode_file(FileItem(path="/Show.S01E01.mkv"), Path("/Show.S01E01.mkv"), media, None, False)
    assert nfo.call_args.kwargs["mediainfo"].scrape_source == "imdb"
    assert images.call_args.kwargs["mediainfo"].scrape_source == "imdb"


def test_local_exists_includes_all_episodes(monkeypatch):
    """
    合并集媒体应将文件中声明的全部剧集加入本地查重结果。
    """
    module = object.__new__(FileManagerModule)
    monkeypatch.setattr("app.modules.filemanager.module.get_runtime_setting", lambda *_args: True)
    monkeypatch.setattr(module, "media_files", lambda _media: [FileItem(basename="Show.S01E01-E02")])
    assert module.media_exists(MediaInfo(type=MediaType.TV, title="Show")).seasons == {1: [1, 2]}


def test_directory_never_overwrite_preserves_library_and_source(tmp_path, monkeypatch):
    """
    原盘目录的 never 策略必须跳过覆盖，且不能误触发移动源清理。
    """
    source, target = tmp_path / "disc", tmp_path / "library"
    source.mkdir()
    target.mkdir()
    leaf, existing = source / "00001.m2ts", target / "00001.m2ts"
    leaf.write_bytes(b"new")
    existing.write_bytes(b"old-library")
    storage = object.__new__(LocalStorage)
    item = storage.get_item_strict(source).model_dump(mode="json")
    checkpoint = replace(_checkpoint(leaf, existing, transfer_type="move", mode="never"),
                         planning_input=TransferPlanningInput(source_fileitem=item), final_target_path=str(target),
                         items=(TransferPlanItem(sequence=0, source_fileitem=storage.get_item_strict(leaf).model_dump(mode="json"),
                                                 target_storage="local", target_path=str(existing)),))
    result = _execute(checkpoint, monkeypatch, None)
    assert existing.read_bytes() == b"old-library"
    assert leaf.read_bytes() == b"new"
    assert not result.success and result.overwrite_skipped


@pytest.mark.parametrize("mutation", ["source", "target"])
def test_recovery_retains_all_copies_after_same_size_mutation(tmp_path, monkeypatch, repository_factory, mutation):
    """
    提升落账后发生等长原地修改，恢复必须隔离任务并保留源和备份。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.2026.mkv"
    source.write_bytes(b"new-version")
    target.write_bytes(b"old-version")
    checkpoint = _checkpoint(source, target, transfer_type="move")
    repository = repository_factory(checkpoint)
    runner = _durable_runner(repository, checkpoint)
    real_run = runner.run

    class ProcessStopped(BaseException):
        """
        模拟删除源之前进程退出，不让执行器转译成普通失败。
        """

    def stop_before_cleanup(**kwargs):
        """
        在提升已有持久回执后中断。
        """
        if kwargs["kind"] == "delete_overwrite_source":
            raise ProcessStopped()
        return real_run(**kwargs)

    monkeypatch.setattr(runner, "run", stop_before_cleanup)
    with pytest.raises(ProcessStopped):
        _execute(checkpoint, monkeypatch, runner)
    backup = next(tmp_path.glob(".mp-backup-*"))
    changed = source if mutation == "source" else target
    before = changed.stat()
    changed.write_bytes(b"NEW-CONTENT" if mutation == "source" else b"CORRUPTION!")
    os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
    with pytest.raises(_TransferManualReviewRequired):
        _execute(checkpoint, monkeypatch, _durable_runner(repository, checkpoint))
    assert repository.get_snapshot(task_id="safe-overwrite").state is TransferExecutionState.MANUAL_REVIEW
    assert source.exists() and backup.exists()
    assert backup.read_bytes() == b"old-version"


@pytest.mark.parametrize("storage", ["u115", "alipan", "alist"])
def test_cloud_cleanup_identity_allows_timestamp_changes(storage):
    """
    云端大小与稳定 ID 相同时保留上传后时间戳变化的兼容合同。
    """
    expected = FileItem(storage=storage, path="/file", type="file", size=10, fileid="id", modify_time=1.0)
    TransHandler._TransHandler__assert_file_identity(expected, expected.model_copy(update={"modify_time": 5.0}))


def test_local_cleanup_rejects_mutation_with_restored_mtime(tmp_path):
    """
    原地改写后还原 mtime 仍会改变 ctime，本地清理必须拒绝这类版本变化。
    """
    path = tmp_path / "file"
    path.write_bytes(b"old")
    local = object.__new__(LocalStorage)
    expected = local.get_item_strict(path)
    before = path.stat()
    time.sleep(0.02)
    path.write_bytes(b"new")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    current = local.get_item_strict(path)
    assert expected.modify_time_ns == current.modify_time_ns
    assert expected.change_time_ns != current.change_time_ns
    with pytest.raises(TransferExecutionConflictError, match="状态时间已变化"):
        TransHandler._TransHandler__assert_file_identity(expected, current)


@pytest.mark.parametrize("failure", ["exception", "detail_size_only"])
def test_openlist_truncated_move_never_deletes_remote_source(tmp_path, monkeypatch, repository_factory, failure):
    """
    真实下载与持久步骤执行器遇到截断流时保留远端原件，不发布本地损坏文件。
    """
    source = object.__new__(Alist)
    target = object.__new__(LocalStorage)
    remote = FileItem(storage="alist", path="/download/Movie.mkv", name="Movie.mkv",
                      basename="Movie", extension="mkv", type="file",
                      size=None if failure == "detail_size_only" else 100, modify_time=1.0)
    payload = remote.model_dump(mode="json")
    destination = tmp_path / "Movie.mkv"
    checkpoint = TransferPlanCheckpoint(
        planning_input=TransferPlanningInput(source_fileitem=payload), target_storage="local",
        root_target_path=str(tmp_path), final_target_path=str(destination), resolved_transfer_type="move",
        overwrite_mode="never", items=(TransferPlanItem(sequence=0, source_fileitem=payload,
                                                        target_storage="local", target_path=str(destination)),),
    )
    repository = repository_factory(checkpoint)
    response = Mock(status_code=200)
    response.json.return_value = {"code": 200, "data": {
        "raw_url": "https://cdn.invalid/file", "sign": "", "size": 100,
    }}
    stream = Mock(headers={} if failure == "detail_size_only" else {"Content-Length": "100"})

    def chunks(**_kwargs):
        """
        在远端下载传输边界回放部分写入后的连接中断。
        """
        yield b"PART"
        if failure == "exception":
            raise ConnectionError("interrupted")

    @contextmanager
    def download_stream(*_args, **_kwargs):
        """
        保留真实下载代码，只替换 HTTP 响应。
        """
        yield stream

    stream.iter_content = chunks
    monkeypatch.setattr(source, "get_conf", lambda: {"url": "https://openlist.invalid", "token": "secret"})
    monkeypatch.setattr(RequestUtils, "post_res", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(RequestUtils, "get_stream", download_stream)
    deleted = Mock(return_value=True)
    monkeypatch.setattr(source, "delete", deleted)
    handler = TransHandler()
    monkeypatch.setattr(handler, "_TransHandler__intercept_transfer", lambda **_kwargs: (True, ""))
    with pytest.raises(RuntimeError, match="下载"):
        handler.execute_transfer_plan(
            checkpoint, meta=MetaVideo("Movie.2026.mkv"), mediainfo=MediaInfo(type=MediaType.MOVIE, title="Movie"),
            source_oper=source, target_oper=target, step_runner=_durable_runner(repository, checkpoint),
        )
    deleted.assert_not_called()
    assert not destination.exists()
    assert not list(tmp_path.glob(".mp-download-*"))
    assert all(step.kind != "delete_move_source" for step in repository.get_snapshot(task_id="safe-overwrite").steps)


def test_local_timestamp_json_roundtrip_keeps_nanosecond_precision():
    """
    纳秒版本证据超过浏览器整数精度时以字符串无损传输，回传后恢复为 Python 整数。
    """
    stamp = 1_792_691_575_120_037_153
    item = FileItem(modify_time_ns=stamp, change_time_ns=stamp + 1)
    payload = item.model_dump(mode="json")
    assert payload["modify_time_ns"] == str(stamp)
    assert payload["change_time_ns"] == str(stamp + 1)
    restored = FileItem.model_validate(payload)
    assert restored.modify_time_ns == stamp
    assert restored.change_time_ns == stamp + 1
    assert restored.model_dump()["modify_time_ns"] == stamp
    assert FileItem().model_dump(mode="json")["modify_time_ns"] is None
