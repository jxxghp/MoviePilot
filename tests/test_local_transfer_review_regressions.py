"""
验证本地并发暂存、音频链接写入和递归重命名的审查回归。
"""

import errno
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system.fsproxy import fsproxy
from app.adapters.system.host import SystemUtils
from app.api.endpoints import storage as storage_endpoint
from app.application import audio
from app.modules.filemanager.storages.local import LocalStorage
from app.schemas.file import FileItem


@pytest.mark.parametrize("operation", ["copy", "move"])
def test_concurrent_transfers_to_same_target_use_separate_partials(tmp_path, monkeypatch, operation):
    """
    并发同目标复制不能发布另一任务的暂存内容，跨盘移动也不能丢失源内容。
    """
    first, second, target = tmp_path / "a.bin", tmp_path / "b.bin", tmp_path / "target.bin"
    first.write_bytes(b"A" * 64)
    second.write_bytes(b"B" * 64)
    copied_first, copied_second, published_first = Event(), Event(), Event()
    partials = []
    original_copy = fsproxy.copy
    storage = object.__new__(LocalStorage)
    monkeypatch.setattr(storage, "_try_reflink_copy", lambda *_args: False)
    monkeypatch.setattr(storage, "_LocalStorage__should_show_progress", lambda *_args: False)

    def copy(source, destination, **kwargs):
        """
        固定两份内容完成复制和提升的先后顺序。
        """
        partials.append(destination)
        if source == first:
            result = original_copy(source, destination, **kwargs)
            copied_first.set()
            assert copied_second.wait(10)
            return result
        assert copied_first.wait(10)
        result = original_copy(source, destination, **kwargs)
        copied_second.set()
        assert published_first.wait(10)
        return result

    monkeypatch.setattr(fsproxy, "copy", copy)
    if operation == "move":
        replace = os.replace

        def cross_device_replace(source, destination):
            """
            只让原始移动跨设备，暂存提升仍使用真实原子替换。
            """
            if source in (first, second):
                raise OSError(errno.EXDEV, "cross-device")
            return replace(source, destination)

        monkeypatch.setattr(os, "replace", cross_device_replace)

    def transfer(source):
        """
        返回每个任务确认成功时实际发布的内容。
        """
        try:
            result = getattr(storage, operation)(FileItem(path=str(source), type="file"), tmp_path, target.name)
            return result, target.read_bytes()
        finally:
            if source == first:
                published_first.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first_result = workers.submit(transfer, first)
        second_result = workers.submit(transfer, second)
        assert first_result.result(20) == (True, b"A" * 64)
        assert second_result.result(20) == (True, b"B" * 64)
    assert len(set(partials)) == 2
    assert not list(tmp_path.glob("*.mp-partial"))


def test_reflink_uses_distinct_private_staging_names(tmp_path, monkeypatch):
    """
    直接调用宿主 reflink 的同目标任务也必须独立分配暂存名称。
    """
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"data")
    seen = []
    copied = Barrier(2)

    def clone(src, dest):
        """
        回放并发克隆，强制两份暂存同时存活。
        """
        seen.append(dest)
        shutil.copyfile(src, dest)
        copied.wait(10)
        return 0, ""

    monkeypatch.setattr(SystemUtils, "_reflink_copy", clone)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _index: SystemUtils.reflink(source, target), range(2)))
    assert results == [(0, ""), (0, "")]
    assert len(set(seen)) == 2


def test_audio_link_write_commits_despite_unchanged_timestamp_signature(tmp_path, monkeypatch):
    """
    相同长度标签更新即使文件系统时间戳没有变化，也不能丢弃独立副本。
    """
    source, target = tmp_path / "seed.flac", tmp_path / "library.flac"
    source.write_bytes(b"old-title")
    os.link(source, target)
    real_signature = audio._audio_signature
    signatures = {}

    def signature(path):
        """
        模拟时间粒度不足的文件系统，保持每条路径的首次版本签名。
        """
        signatures.setdefault(path, real_signature(path))
        return signatures[path]

    monkeypatch.setattr(audio, "_audio_signature", signature)
    with audio._audio_write_target(target) as writable:
        writable.write_bytes(b"new-title")
    assert target.read_bytes() == b"new-title"
    assert source.read_bytes() == b"old-title"
    assert not target.samefile(source)


def test_recursive_rename_recognizes_nested_original_paths(tmp_path, monkeypatch):
    """
    递归重命名必须处理季目录内媒体，并用真实子路径进行识别。
    """
    root = tmp_path / "Original"
    nested = root / "Season1" / "Show.S01E01.mkv"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"fictional")
    storage = object.__new__(LocalStorage)
    recognized = []

    def list_files(item, recursion=False):
        """
        在临时目录提供与存储链一致的递归列表合同。
        """
        directory = Path(item.path)
        paths = directory.rglob("*") if recursion else directory.iterdir()
        return [storage.get_item_strict(path) for path in paths]

    def recognize(path, **_kwargs):
        """
        记录实际识别位置，不读取外部媒体或来源。
        """
        recognized.append(path)
        return SimpleNamespace(meta_info=path, media_info=object())

    monkeypatch.setattr(storage_endpoint, "StorageChain", lambda: SimpleNamespace(
        list_files=list_files, rename_file=storage.rename,
    ))
    monkeypatch.setattr(storage_endpoint, "MediaChain", lambda: SimpleNamespace(recognize_by_path=recognize))
    monkeypatch.setattr(storage_endpoint, "TransferChain", lambda: SimpleNamespace(
        recommend_name=lambda **_kwargs: "Recognized.mkv"))
    monkeypatch.setattr(storage_endpoint, "ProgressHelper", Mock())
    monkeypatch.setattr(storage_endpoint, "get_api_runtime_config_snapshot", lambda: SimpleNamespace(
        media_extensions=[".mkv"], subtitle_extensions=[], audio_extensions=[]))
    result = storage_endpoint.rename(storage.get_item_strict(root), "Renamed", recursive=True)
    assert result.success
    assert recognized == [nested]
    assert (tmp_path / "Renamed" / "Season1" / "Recognized.mkv").read_bytes() == b"fictional"


def test_parallel_reflink_permission_probes_are_private(tmp_path, monkeypatch):
    """
    并发权限探针各自持有临时名称，清理另一任务探针不能使克隆退回复制。
    """
    source = tmp_path / "source"
    source.write_bytes(b"data")
    probes = []
    ready = Barrier(2)
    write_bytes = Path.write_bytes

    def write_probe(path, data):
        """
        强制两个权限探针同时存在，捕获各自名称。
        """
        result = write_bytes(path, data)
        if path.name.startswith(".mode-probe-"):
            probes.append(path)
            ready.wait(10)
        return result

    def clone(src, dest):
        """
        使用真实复制回放支持 reflink 的文件系统。
        """
        shutil.copyfile(src, dest)
        return 0, ""

    monkeypatch.setattr(Path, "write_bytes", write_probe)
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda *_args: True)
    monkeypatch.setattr(SystemUtils, "reflink", clone)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda index: LocalStorage._try_reflink_copy(source, tmp_path / str(index)), range(2)))
    assert results == [True, True]
    assert len(set(probes)) == 2
    assert not list(tmp_path.glob(".mode-probe-*"))
