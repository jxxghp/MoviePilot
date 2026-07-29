from pathlib import Path

from app.modules.filemanager.transhandler import TransHandler

confirm_target_absent = TransHandler._TransHandler__confirm_target_absent


def test_confirm_target_absent_for_missing_file(tmp_path):
    """
    目标文件确实不存在时应确认为不存在，允许正常整理。
    """
    assert confirm_target_absent("local", tmp_path / "missing.mkv") is True


def test_confirm_target_absent_for_existing_file(tmp_path):
    """
    目标文件存在时应确认为存在。
    """
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"movie")

    assert confirm_target_absent("local", target) is False


def test_confirm_target_absent_for_broken_symlink(tmp_path):
    """
    失效软链接视为目标不存在，不应阻断整理。
    """
    target = tmp_path / "movie.mkv"
    target.symlink_to(tmp_path / "gone.mkv")

    assert confirm_target_absent("local", target) is True


def test_confirm_target_absent_returns_none_on_stat_error(tmp_path, monkeypatch):
    """
    FUSE 挂载抖动导致 stat 失败时应返回无法确认，从而拒绝覆盖。
    """
    target = tmp_path / "movie.mkv"

    def raise_stat_error(self, *args, **kwargs):
        """
        模拟 CloudDrive FUSE 挂载返回 ENOTRECOVERABLE。
        """
        raise OSError(131, "State not recoverable")

    monkeypatch.setattr(Path, "stat", raise_stat_error)

    assert confirm_target_absent("local", target) is None


def test_confirm_target_absent_skips_remote_storage(tmp_path):
    """
    网盘存储没有低成本二次探测手段，沿用 get_item 的判定。
    """
    assert confirm_target_absent("u115", tmp_path / "movie.mkv") is True
