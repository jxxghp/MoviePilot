"""
整理队列持久化与重启回放测试。

整理队列是纯内存的 queue.Queue：挂载挂死后的人工重启、版本升级、OOM、宿主
重启都会让队列连同「这些文件还没整理」这个事实一起蒸发，而已稳定落地的文件
不会再产生任何监控事件，也不会有新的补偿扫描起点——结果就是永久漏件。

这些测试固定三项不变量：入队即落盘登记、终态即注销、重启能回放。
"""
from pathlib import Path
from unittest.mock import MagicMock

from app.chain.transfer import TransferChain
from app.schemas import FileItem, TransferTask


def _build_chain(pendingoper) -> TransferChain:
    """
    构造绕过单例初始化的 TransferChain 骨架。
    :param pendingoper: 待整理登记管理替身
    :return: TransferChain 骨架
    """
    chain = object.__new__(TransferChain)
    chain._pendingoper = pendingoper
    return chain


def _task(path: str, storage: str = "local") -> TransferTask:
    """
    构造测试用整理任务。
    :param path: 源文件路径
    :param storage: 存储
    :return: 整理任务
    """
    file_path = Path(path)
    return TransferTask(fileitem=FileItem(
        storage=storage,
        path=path,
        type="file",
        name=file_path.name,
        basename=file_path.stem,
        extension=file_path.suffix[1:],
    ))


def test_register_pending_records_storage_and_path():
    """
    入队时必须落盘登记「存储 + 源路径」这一最小事实。
    """
    pendingoper = MagicMock()
    chain = _build_chain(pendingoper)

    chain._TransferChain__register_pending(_task("/mnt/cd2/downloads/Movie.2024.mkv"))

    pendingoper.register.assert_called_once_with(
        storage="local", src_path="/mnt/cd2/downloads/Movie.2024.mkv"
    )


def test_register_pending_failure_does_not_break_enqueue():
    """
    落盘登记只是重启后的补救手段，登记失败绝不能阻断正常整理。
    """
    pendingoper = MagicMock()
    pendingoper.register.side_effect = RuntimeError("db locked")
    chain = _build_chain(pendingoper)

    # 不抛异常即为通过
    chain._TransferChain__register_pending(_task("/mnt/cd2/downloads/Movie.2024.mkv"))


def test_discard_pending_on_terminal_state():
    """
    整理到达终态后必须注销登记，否则每次重启都会重复回放。
    """
    pendingoper = MagicMock()
    chain = _build_chain(pendingoper)

    chain._TransferChain__discard_pending(_task("/mnt/cd2/downloads/Movie.2024.mkv"))

    pendingoper.discard.assert_called_once_with(
        storage="local", src_path="/mnt/cd2/downloads/Movie.2024.mkv"
    )


def test_replay_resends_pending_files_to_transfer(tmp_path, monkeypatch):
    """
    重启回放：登记过的文件要重新送入整理链，恢复被内存队列蒸发的任务。
    """
    media = tmp_path / "Movie.2024.mkv"
    media.write_bytes(b"x" * 10)

    pendingoper = MagicMock()
    pendingoper.list_all.return_value = [("local", str(media))]
    chain = _build_chain(pendingoper)

    transferred = []
    monkeypatch.setattr(chain, "do_transfer", lambda **kw: transferred.append(kw["fileitem"]))

    chain._TransferChain__replay_pending()

    assert len(transferred) == 1
    item = transferred[0]
    assert item.path == media.as_posix()
    assert item.storage == "local"
    assert item.type == "file"
    # 回放时重新读取当前大小，不依赖登记时的陈旧信息
    assert item.size == 10


def test_replay_discards_vanished_files(tmp_path):
    """
    源文件已消失的登记要注销，否则每次启动都会重复回放一个不存在的文件。
    """
    pendingoper = MagicMock()
    missing = tmp_path / "gone.mkv"
    pendingoper.list_all.return_value = [("local", str(missing))]
    chain = _build_chain(pendingoper)
    chain.do_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    chain.do_transfer.assert_not_called()
    pendingoper.discard.assert_called_once_with(storage="local", src_path=str(missing))


def test_replay_keeps_registration_when_mount_unreadable(tmp_path, monkeypatch):
    """
    挂载未就绪时读取失败属于暂时性故障，登记必须保留，等下次启动或人工整理。

    这与「文件已消失」必须区别对待：把挂载抖动误判成文件消失就等于主动丢件。
    """
    media = tmp_path / "Movie.2024.mkv"
    media.write_bytes(b"x")

    pendingoper = MagicMock()
    pendingoper.list_all.return_value = [("local", str(media))]
    chain = _build_chain(pendingoper)
    chain.do_transfer = MagicMock()

    def unreadable(self, *_args, **_kwargs):
        """
        模拟挂载未就绪时的 stat 失败。
        """
        raise OSError(107, "Transport endpoint is not connected")

    monkeypatch.setattr(Path, "stat", unreadable)

    chain._TransferChain__replay_pending()

    chain.do_transfer.assert_not_called()
    pendingoper.discard.assert_not_called()


def test_replay_restores_bluray_directory_type(tmp_path, monkeypatch):
    """
    蓝光原盘登记时保留尾部斜杠，回放必须还原成目录类型，否则会被当成单文件整理。
    """
    bluray = tmp_path / "Movie.2024.BluRay"
    bluray.mkdir()
    src_path = f"{bluray.as_posix()}/"

    pendingoper = MagicMock()
    pendingoper.list_all.return_value = [("local", src_path)]
    chain = _build_chain(pendingoper)

    transferred = []
    monkeypatch.setattr(chain, "do_transfer", lambda **kw: transferred.append(kw["fileitem"]))

    chain._TransferChain__replay_pending()

    assert len(transferred) == 1
    assert transferred[0].type == "dir"
    assert transferred[0].path == src_path


def test_replay_is_noop_without_registrations():
    """
    没有登记时回放不应触碰整理链。
    """
    pendingoper = MagicMock()
    pendingoper.list_all.return_value = []
    chain = _build_chain(pendingoper)
    chain.do_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    chain.do_transfer.assert_not_called()


def test_replay_survives_db_failure():
    """
    读取登记失败不能让启动流程报错。
    """
    pendingoper = MagicMock()
    pendingoper.list_all.side_effect = RuntimeError("db gone")
    chain = _build_chain(pendingoper)
    chain.do_transfer = MagicMock()

    chain._TransferChain__replay_pending()

    chain.do_transfer.assert_not_called()


def test_replay_continues_after_single_file_failure(tmp_path, monkeypatch):
    """
    单个文件回放失败不能中断整批回放，否则一个坏文件会拖住所有漏件的恢复。
    """
    first = tmp_path / "A.mkv"
    second = tmp_path / "B.mkv"
    for item in (first, second):
        item.write_bytes(b"x")

    pendingoper = MagicMock()
    pendingoper.list_all.return_value = [("local", str(first)), ("local", str(second))]
    chain = _build_chain(pendingoper)

    handled = []

    def flaky(**kw):
        """
        第一个文件整理抛异常，第二个正常。
        """
        if kw["fileitem"].name == "A.mkv":
            raise RuntimeError("boom")
        handled.append(kw["fileitem"].name)

    monkeypatch.setattr(chain, "do_transfer", flaky)

    chain._TransferChain__replay_pending()

    assert handled == ["B.mkv"]
