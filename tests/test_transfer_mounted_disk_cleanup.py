from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.chain.transfer import TransferChain
from app.schemas import FileItem, TransferDirectoryConf, TransferTask
from app.utils.system import SystemUtils


def _make_task(
        storage: str = "local",
        download_path: str = "/mnt/clouddrive/downloads",
) -> TransferTask:
    return TransferTask(
        fileitem=FileItem(
            storage=storage,
            path=f"{download_path}/Test.Show.S01E01.mkv",
            type="file",
            name="Test.Show.S01E01.mkv",
        ),
        target_directory=TransferDirectoryConf(
            storage=storage,
            download_path=download_path,
        ),
    )


def test_enabled_cleanup_skips_filesystem_detection():
    """
    开关开启时应保持旧行为，且不产生额外文件系统检测。
    """
    with patch(
            "app.chain.transfer.SystemUtils.is_network_filesystem"
    ) as is_network_filesystem:
        should_delete = (
            TransferChain._TransferChain__should_delete_empty_source_directories(
                _make_task(),
                True,
                {},
            )
        )

    assert should_delete is True
    is_network_filesystem.assert_not_called()


def test_disabled_cleanup_keeps_mounted_local_source_directories():
    """
    开关关闭时应保留网络或 FUSE 挂载的本地源目录。
    """
    with patch(
            "app.chain.transfer.SystemUtils.is_network_filesystem",
            return_value=True,
    ) as is_network_filesystem:
        should_delete = (
            TransferChain._TransferChain__should_delete_empty_source_directories(
                _make_task(),
                False,
                {},
            )
        )

    assert should_delete is False
    is_network_filesystem.assert_called_once_with(
        Path("/mnt/clouddrive/downloads"), include_local_fuse=True
    )


def test_disabled_cleanup_still_deletes_ordinary_local_source_directories():
    """
    开关关闭时普通本地文件系统仍应删除空目录。
    """
    with patch(
            "app.chain.transfer.SystemUtils.is_network_filesystem",
            return_value=False,
    ):
        should_delete = (
            TransferChain._TransferChain__should_delete_empty_source_directories(
                _make_task(download_path="/downloads"),
                False,
                {},
            )
        )

    assert should_delete is True


def test_disabled_cleanup_does_not_change_remote_storage_cleanup():
    """
    开关关闭时非本地存储仍应执行原有空目录清理。
    """
    with patch(
            "app.chain.transfer.SystemUtils.is_network_filesystem"
    ) as is_network_filesystem:
        should_delete = (
            TransferChain._TransferChain__should_delete_empty_source_directories(
                _make_task(storage="alist", download_path="/downloads"),
                False,
                {},
            )
        )

    assert should_delete is True
    is_network_filesystem.assert_not_called()


def test_mounted_filesystem_detection_is_cached_by_source_directory():
    """
    同一源根目录的批量任务应只检测一次文件系统。
    """
    mounted_filesystem_cache = {}
    with patch(
            "app.chain.transfer.SystemUtils.is_network_filesystem",
            return_value=True,
    ) as is_network_filesystem:
        for _ in range(2):
            should_delete = (
                TransferChain._TransferChain__should_delete_empty_source_directories(
                    _make_task(),
                    False,
                    mounted_filesystem_cache,
                )
            )
            assert should_delete is False

    is_network_filesystem.assert_called_once_with(
        Path("/mnt/clouddrive/downloads"), include_local_fuse=True
    )


def test_cleanup_detection_includes_local_fuse_mounts():
    """
    空目录清理场景应将原本排除的本地 FUSE 文件系统视为挂载盘。
    """
    df_result = SimpleNamespace(
        returncode=0,
        stdout="Filesystem Type 1K-blocks Used Available Use% Mounted on\n"
               "shfs fuse.shfs 1 1 1 1% /mnt/user\n",
    )
    with patch("app.utils.system.platform.system", return_value="Linux"), patch(
            "app.utils.system.subprocess.run", return_value=df_result
    ):
        assert SystemUtils.is_network_filesystem(Path("/mnt/user")) is False
        assert SystemUtils.is_network_filesystem(
            Path("/mnt/user"), include_local_fuse=True
        ) is True
