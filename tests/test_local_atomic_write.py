"""
本地存储写入原子性测试。

copy / 跨盘 move / upload 原先直接写目标路径，进程被杀（OOM、重启、宿主断电、
SIGKILL）时目标目录会留下一个**半截的媒体文件，而且叫最终文件名**——媒体库会
把它扫进去，后续的「目标已存在」检查也会把它当成完成品。

改成「写临时名 → os.replace」后，os.replace 在同目录内由内核保证原子性：
中断只可能留下一个带专用后缀的隐藏文件，最终文件要么完整存在，要么根本不存在。
"""
import errno
import os
import sys
from pathlib import Path

import pytest

from app.adapters.system.fsproxy import fsproxy
from app.modules.filemanager.storages.local import LocalStorage
from app.schemas.file import FileItem


def _fileitem(path: Path) -> FileItem:
    """
    构造本地文件项。

    :param path: 文件路径
    :return: 文件项
    """
    return FileItem(
        storage="local",
        type="file",
        path=path.as_posix(),
        name=path.name,
        basename=path.stem,
        extension=path.suffix[1:],
    )


@pytest.fixture
def storage():
    """
    本地存储实例。
    """
    return LocalStorage()


def test_copy_produces_complete_file(tmp_path, storage):
    """
    正常复制的结果必须与直接复制一致。
    """
    src = tmp_path / "src" / "Movie.2024.mkv"
    src.parent.mkdir()
    src.write_bytes(b"payload" * 100)
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()

    assert storage.copy(_fileitem(src), dest_dir, "Movie.2024.mkv") is True

    assert (dest_dir / "Movie.2024.mkv").read_bytes() == b"payload" * 100
    # 复制完成后不能残留任何临时文件
    assert not list(dest_dir.glob(f"*{LocalStorage.PARTIAL_SUFFIX}"))


def test_copy_leaves_no_final_name_on_failure(tmp_path, storage, monkeypatch):
    """
    复制中途失败时，目标文件名绝不能出现——这是本次修复的核心。

    失败点选在内容写完、替换之前，正是原实现留下「完整文件名的半成品」的位置。
    """
    src = tmp_path / "Movie.2024.mkv"
    src.write_bytes(b"x" * 1000)
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()

    def boom(*_args, **_kwargs):
        """
        模拟替换阶段被打断。
        """
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)

    assert storage.copy(_fileitem(src), dest_dir, "Movie.2024.mkv") is False

    # 最终文件名不存在，媒体库不会扫到半成品
    assert not (dest_dir / "Movie.2024.mkv").exists()
    # 临时文件也要清掉，不留垃圾
    assert not list(dest_dir.glob(f"*{LocalStorage.PARTIAL_SUFFIX}"))


def test_partial_file_is_hidden_and_suffixed(tmp_path, storage):
    """
    临时文件必须同时满足：点开头（隐藏）+ 专用后缀。

    媒体库按扩展名识别媒体文件，点开头又能让多数扫描器跳过；两者叠加保证
    即使进程被 SIGKILL、临时文件残留，也不会被当成媒体收录。
    """
    partial = storage._partial_path(tmp_path / "Movie.2024.mkv")

    assert partial.name.startswith(".")
    assert partial.name.endswith(LocalStorage.PARTIAL_SUFFIX)
    # 必须与目标同目录，os.replace 才是原子的（跨文件系统会退化成拷贝）
    assert partial.parent == tmp_path
    assert partial.suffix != ".mkv"


def _name_with_byte_length(total: int) -> str:
    """
    构造指定字节长度的文件名（多字节字符与 ASCII 混合）。

    :param total: 文件名按 UTF-8 计的总字节数，含 “.nfo”
    :return: 指定字节长度的文件名
    """
    stem_bytes = total - len(".nfo")
    name = "测" * (stem_bytes // 3) + "a" * (stem_bytes % 3) + ".nfo"
    assert len(name.encode("utf-8")) == total
    return name


def test_partial_path_keeps_short_dest_name_unchanged(tmp_path, storage):
    """
    目标名在预算内时中间名必须与旧格式完全一致。

    218 字节以下的目标名不应被改动；恰好占满预算（210 字节目标名，
    210 + 45 = 255）的边界名也不应被截断。
    """
    short = storage._partial_path(tmp_path / "Movie.2024.mkv")
    assert ".Movie.2024.mkv." in short.name
    assert short.name.endswith(LocalStorage.PARTIAL_SUFFIX)

    boundary = _name_with_byte_length(210)
    partial = storage._partial_path(tmp_path / boundary)
    assert f".{boundary}." in partial.name
    assert len(partial.name.encode("utf-8")) == 255


def test_partial_path_truncates_long_dest_name(tmp_path, storage):
    """
    目标名 218 字节时中间名不得超过 255 字节（issue #7572）。

    旧格式会生成 218 + 45 = 263 字节的中间名，写临时文件直接报
    ENAMETOOLONG；截断后中间名必须合法，且保留的目标名前缀不得在
    多字节字符中间被切断。
    """
    name = _name_with_byte_length(218)
    partial = storage._partial_path(tmp_path / name)

    assert partial.parent == tmp_path
    assert partial.name.startswith(".")
    assert partial.name.endswith(LocalStorage.PARTIAL_SUFFIX)
    assert len(partial.name.encode("utf-8")) <= 255
    # 去掉前导点与 “.uuid.mp-partial” 后，剩余部分必须是原目标名的前缀
    core = partial.name[1: -len(LocalStorage.PARTIAL_SUFFIX)]
    truncated, _token = core.rsplit(".", 1)
    assert name.startswith(truncated)
    assert len(truncated.encode("utf-8")) <= 210


def test_copy_long_dest_name_succeeds(tmp_path, storage):
    """
    目标文件名 218 字节时复制必须成功（issue #7572 的端到端回归）。

    目标名本身合法（≤ 255 字节），元数据写入就不应因中间名超长而失败。
    """
    src = tmp_path / "src" / "Episode.nfo"
    src.parent.mkdir()
    src.write_bytes(b"<episodedetails />")
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()

    name = _name_with_byte_length(218)
    assert storage.copy(_fileitem(src), dest_dir, name) is True

    assert (dest_dir / name).read_bytes() == b"<episodedetails />"
    assert not list(dest_dir.glob(f"*{LocalStorage.PARTIAL_SUFFIX}"))


def test_copy_non_utf8_long_dest_name_succeeds(tmp_path, storage, monkeypatch):
    """
    非 UTF-8 长文件名复制必须成功（issue #7572 的跟进回归）。

    磁盘上可能存在无法按 UTF-8 解码的文件名（surrogateescape）；度量与
    截断必须用 os.fsencode 往返原字节，str.encode("utf-8") 会直接抛
    UnicodeEncodeError，让本可复制的文件写不进去。
    APFS 等强制合法 UTF-8 的文件系统无法直接落盘原始字节，此时通过底层端口替身校验。
    """
    if sys.getfilesystemencodeerrors() != "surrogateescape":
        pytest.skip("当前平台文件系统编码未使用 surrogateescape")

    src = tmp_path / "src" / "Episode.nfo"
    src.parent.mkdir()
    src.write_bytes(b"<episodedetails />")
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()

    raw = b"\xff" * 10 + b"a" * 204 + b".nfo"
    assert len(raw) == 218
    name = os.fsdecode(raw)

    partial = storage._partial_path(dest_dir / name)
    raw_partial = os.fsencode(partial.name)
    assert len(raw_partial) <= 255
    assert raw_partial.startswith(b".\xff" + b"\xff" * 9)

    probe = dest_dir / os.fsdecode(b".mp-probe-\xff")
    supports_raw_bytes = True
    try:
        probe.write_bytes(b"")
    except OSError:
        supports_raw_bytes = False
    finally:
        probe.unlink(missing_ok=True)

    if supports_raw_bytes:
        assert storage.copy(_fileitem(src), dest_dir, name) is True
        assert (dest_dir / name).read_bytes() == b"<episodedetails />"
        assert not list(dest_dir.glob(f"*{LocalStorage.PARTIAL_SUFFIX}"))
    else:
        recorded: dict[str, Path] = {}

        def fake_copy(source, destination, **_kwargs):
            """
            记录传入底层复制的中间路径，避免 APFS 因非 UTF-8 字节报 EILSEQ。
            """
            assert source == src
            assert len(os.fsencode(destination.name)) <= 255
            recorded["partial"] = destination
            return {"copied": 18, "total": 18}

        def fake_replace(source, destination):
            """
            校验原子替换阶段将预算内的中间路径提升到完整目标路径。
            """
            assert source == recorded["partial"]
            recorded["dest"] = Path(destination)

        monkeypatch.setattr(fsproxy, "copy", fake_copy)
        monkeypatch.setattr(os, "replace", fake_replace)
        assert storage.copy(_fileitem(src), dest_dir, name, try_reflink=False) is True
        assert recorded["dest"] == dest_dir / name


def test_partial_path_respects_tiny_name_max(tmp_path, storage, monkeypatch):
    """
    文件系统上限异常小时，退化的中间名仍不得超过该上限。
    """
    monkeypatch.setattr(os, "pathconf", lambda *_args: 40)

    partial = storage._partial_path(tmp_path / "Movie.2024.mkv")

    assert partial.parent == tmp_path
    assert partial.name.startswith(".")
    assert partial.name.endswith(LocalStorage.PARTIAL_SUFFIX)
    assert len(os.fsencode(partial.name)) <= 40


def test_partial_path_falls_back_when_pathconf_unavailable_or_unlimited(
    tmp_path, storage, monkeypatch
):
    """
    缺少 os.pathconf、查询抛错或返回无上限负数时，统一回退到 255 字节预算。
    """
    long_name = _name_with_byte_length(218)

    # 模拟 Windows 环境不存在 os.pathconf
    monkeypatch.delattr(os, "pathconf", raising=False)
    win_partial = storage._partial_path(tmp_path / long_name)
    assert len(os.fsencode(win_partial.name)) <= 255
    assert win_partial.name.endswith(LocalStorage.PARTIAL_SUFFIX)

    # 模拟挂载查询抛出 OSError
    def raise_oserror(*_args):
        """
        模拟不支持 PC_NAME_MAX 查询的文件系统。
        """
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(os, "pathconf", raise_oserror, raising=False)
    err_partial = storage._partial_path(tmp_path / long_name)
    assert len(os.fsencode(err_partial.name)) <= 255

    # 模拟 POSIX 无固定上限时返回 -1，不得退化为固定空标识名
    monkeypatch.setattr(os, "pathconf", lambda *_args: -1, raising=False)
    unlimited_a = storage._partial_path(tmp_path / "Movie.2024.mkv")
    unlimited_b = storage._partial_path(tmp_path / "Movie.2024.mkv")
    assert ".Movie.2024.mkv." in unlimited_a.name
    assert unlimited_a != unlimited_b


def test_partial_path_omits_empty_name_when_multibyte_char_exceeds_small_budget(
    tmp_path, storage, monkeypatch
):
    """
    剩余预算小于首字符多字节长度时，应退化为单前导点紧凑名，避免拼出双点前缀。
    """
    monkeypatch.setattr(os, "pathconf", lambda *_args: 46, raising=False)

    partial = storage._partial_path(tmp_path / "测试.nfo")

    assert partial.name.startswith(".")
    assert not partial.name.startswith("..")
    assert partial.name.endswith(LocalStorage.PARTIAL_SUFFIX)
    assert len(os.fsencode(partial.name)) <= 46


def test_copy_retries_compact_partial_when_mount_enforces_stricter_name_max(
    tmp_path, storage, monkeypatch
):
    """
    网络挂载虚报 PC_NAME_MAX=255 但底层限制 143 字节时，复制应自动回退紧凑中间名完成写入；
    若最终目标名自身已超限，则在替换阶段失败且不重复复制。
    """
    src = tmp_path / "src" / "Episode.nfo"
    src.parent.mkdir()
    src.write_bytes(b"<episodedetails />")
    dest_dir = tmp_path / "encrypted_mount"
    dest_dir.mkdir()

    monkeypatch.setattr(os, "pathconf", lambda *_args: 255, raising=False)
    real_copy = fsproxy.copy
    real_replace = os.replace
    copy_attempts: list[Path] = []

    def strict_copy(source, destination, **kwargs):
        """
        模拟下层 eCryptfs 目录拒绝超过 143 字节的文件名。
        """
        copy_attempts.append(destination)
        if len(os.fsencode(destination.name)) > 143:
            raise OSError(errno.ENAMETOOLONG, "File name too long")
        return real_copy(source, destination, **kwargs)

    def strict_replace(source, destination):
        """
        模拟最终替换同样遵守下层 143 字节上限。
        """
        if len(os.fsencode(Path(destination).name)) > 143:
            raise OSError(errno.ENAMETOOLONG, "File name too long")
        return real_replace(source, destination)

    monkeypatch.setattr(fsproxy, "copy", strict_copy)
    monkeypatch.setattr(os, "replace", strict_replace)

    valid_dest = _name_with_byte_length(130)
    assert storage.copy(_fileitem(src), dest_dir, valid_dest, try_reflink=False) is True
    assert len(copy_attempts) == 2
    assert len(os.fsencode(copy_attempts[0].name)) == 175
    assert len(os.fsencode(copy_attempts[1].name)) == 44
    assert (dest_dir / valid_dest).read_bytes() == b"<episodedetails />"
    assert not list(dest_dir.glob(f"*{LocalStorage.PARTIAL_SUFFIX}"))

    # 最终目标名本身超过 143 字节（但初始中间名也超过 143 字节）：回退写完紧凑名后在 replace 失败并清理
    copy_attempts.clear()
    too_long_dest = _name_with_byte_length(150)
    assert storage.copy(_fileitem(src), dest_dir, too_long_dest, try_reflink=False) is False
    assert not (dest_dir / too_long_dest).exists()
    assert not list(dest_dir.glob(f"*{LocalStorage.PARTIAL_SUFFIX}"))


def test_softlink_repair_dangling_long_target_name_succeeds(tmp_path, storage):
    """
    悬空软链接的目标文件名接近 255 字节上限时，私有暂存链接仍须控制在上限内并完成原子修复。
    """
    src = tmp_path / "src" / "Episode.mkv"
    src.parent.mkdir()
    src.write_bytes(b"video")
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()

    long_target = dest_dir / _name_with_byte_length(230)
    long_target.symlink_to(tmp_path / "missing.mkv")
    assert long_target.is_symlink() and not long_target.exists()

    assert storage.softlink(_fileitem(src), long_target) is True
    assert long_target.is_symlink()
    assert long_target.resolve() == src.resolve()
    assert not list(dest_dir.glob(f".*{LocalStorage.STAGED_LINK_SUFFIX}"))


def test_cleanup_removes_stale_truncated_partial(tmp_path, storage):
    """
    截断后的临时名仍要能被陈旧清理认出并清理。

    清理按 `*{PARTIAL_SUFFIX}` 匹配，与中间名是否含完整目标名无关。
    """
    stale = storage._partial_path(tmp_path / _name_with_byte_length(218))
    fresh = storage._partial_path(tmp_path / _name_with_byte_length(218))
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")

    old_time = os.stat(stale).st_mtime - LocalStorage.PARTIAL_STALE_SECONDS - 60
    os.utime(stale, (old_time, old_time))

    storage._cleanup_stale_partials(tmp_path)

    assert not stale.exists(), "陈旧的截断临时文件没有被清理"
    assert fresh.exists(), "正在写入的截断临时文件被误删了"


def test_move_produces_complete_file_and_removes_source(tmp_path, storage, monkeypatch):
    """
    跨盘移动完成后目标完整、源被删除。
    """
    src = tmp_path / "src" / "Movie.2024.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data" * 50)
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()

    # 强制走跨盘路径（copy + unlink）：只让「源 → 最终目标」的直接 rename 以
    # EXDEV 失败，临时文件到目标的替换仍需正常工作
    real_replace = os.replace

    def exdev_for_source(source, target, *args, **kwargs):
        """
        模拟源与目标不在同一文件系统。
        """
        if Path(source) == src:
            raise OSError(18, "Invalid cross-device link")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", exdev_for_source)

    assert storage.move(_fileitem(src), dest_dir, "Movie.2024.mkv") is True

    assert (dest_dir / "Movie.2024.mkv").read_bytes() == b"data" * 50
    assert not src.exists()
    assert not list(dest_dir.glob(f"*{LocalStorage.PARTIAL_SUFFIX}"))


def test_move_keeps_source_when_copy_fails(tmp_path, storage, monkeypatch):
    """
    跨盘移动失败时源文件必须保留——先删源再失败就是永久丢件。
    """
    src = tmp_path / "src" / "Movie.2024.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()

    def boom(*_args, **_kwargs):
        """
        模拟直接移动与临时文件替换都失败（磁盘写满）。
        """
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)

    assert storage.move(_fileitem(src), dest_dir, "Movie.2024.mkv") is False

    assert src.read_bytes() == b"data", "移动失败却删掉了源文件，等于永久丢件"
    assert not (dest_dir / "Movie.2024.mkv").exists()


def test_same_path_move_is_noop(tmp_path, storage):
    """
    源与目标相同时应直接成功，不能把文件写没了。
    """
    media = tmp_path / "Movie.2024.mkv"
    media.write_bytes(b"keep")

    assert storage.move(_fileitem(media), tmp_path, "Movie.2024.mkv") is True
    assert media.read_bytes() == b"keep"


def test_cleanup_removes_stale_partials_only(tmp_path, storage):
    """
    清理只针对陈旧的临时文件：正在写入的临时文件与正常媒体文件都不能被误删。

    不做全库扫描，只在实际写入的目录做局部清理——全库遍历在网络挂载上代价
    不可接受，而残留只会出现在曾经写入过的目录里。
    """
    stale = tmp_path / f".Old.mkv.1234{LocalStorage.PARTIAL_SUFFIX}"
    fresh = tmp_path / f".New.mkv.5678{LocalStorage.PARTIAL_SUFFIX}"
    media = tmp_path / "Keep.mkv"
    for item in (stale, fresh, media):
        item.write_bytes(b"x")

    # 把陈旧临时文件的 mtime 推到阈值之前
    old_time = os.stat(stale).st_mtime - LocalStorage.PARTIAL_STALE_SECONDS - 60
    os.utime(stale, (old_time, old_time))

    storage._cleanup_stale_partials(tmp_path)

    assert not stale.exists(), "陈旧临时文件没有被清理"
    assert fresh.exists(), "正在写入的临时文件被误删了"
    assert media.exists(), "正常媒体文件被误删了"


def test_cleanup_never_raises(tmp_path, storage, monkeypatch):
    """
    清理是尽力而为的旁路操作，任何失败都不能影响主流程。
    """
    def boom(*_args, **_kwargs):
        """
        模拟目录不可读。
        """
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Path, "glob", boom)

    # 不抛异常即为通过
    storage._cleanup_stale_partials(tmp_path)
