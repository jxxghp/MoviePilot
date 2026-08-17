"""仪表板存储空间统计口径测试。

覆盖 issue #6268：仪表板此前仅按 ``library_storage`` 选存储，
只配置本地下载目录（未填媒体库存储）时会退化为 0，而设置页正常。
"""
from typing import Any, Dict, List
from unittest.mock import patch

from app import schemas
from app.api.endpoints import dashboard as dashboard_endpoint
from app.schemas.types import StorageAction


def _usage_result(total: float, available: float) -> Dict[str, Any]:
    """构造 storage_manage 契约的 usage 返回结构。"""
    return {"success": True, "data": {"total": total, "available": available}}


def _patch_dirs(dirs: List[schemas.TransferDirectoryConf]):
    """替换仪表板读取的目录配置。"""
    return patch.object(dashboard_endpoint.DirectoryHelper, "get_dirs", return_value=dirs)


def test_storage_counts_local_download_dir_without_library_storage():
    """只配置本地下载目录时仍应统计到该存储（issue #6268 回归）。"""
    dirs = [
        schemas.TransferDirectoryConf(
            name="下载目录",
            storage="local",
            download_path="/downloads",
        )
    ]

    with _patch_dirs(dirs), patch.object(
        dashboard_endpoint.StorageChain,
        "manage_storage",
        return_value=_usage_result(1000.0, 400.0),
    ) as mocked_usage:
        ret = dashboard_endpoint._build_storage()

    mocked_usage.assert_called_once_with(storage="local", action=StorageAction.USAGE.value)
    assert ret.total_storage == 1000.0
    assert ret.used_storage == 600.0


def test_storage_queries_each_storage_once():
    """同一存储被多个目录引用时只查询一次，避免重复累加。"""
    dirs = [
        schemas.TransferDirectoryConf(
            name="下载与媒体库同盘",
            storage="local",
            download_path="/downloads",
            library_path="/media",
            library_storage="local",
        ),
        schemas.TransferDirectoryConf(
            name="另一个本地目录",
            storage="local",
            download_path="/downloads2",
            library_path="/media2",
            library_storage="local",
        ),
    ]

    with _patch_dirs(dirs), patch.object(
        dashboard_endpoint.StorageChain,
        "manage_storage",
        return_value=_usage_result(1000.0, 400.0),
    ) as mocked_usage:
        ret = dashboard_endpoint._build_storage()

    assert mocked_usage.call_count == 1
    assert ret.total_storage == 1000.0
    assert ret.used_storage == 600.0


def test_storage_sums_distinct_storages():
    """不同存储分别统计并累加。"""
    dirs = [
        schemas.TransferDirectoryConf(
            name="本地下载",
            storage="local",
            download_path="/downloads",
        ),
        schemas.TransferDirectoryConf(
            name="网盘媒体库",
            storage="local",
            download_path="/downloads",
            library_path="/cloud/media",
            library_storage="u115",
        ),
    ]
    usages = {
        "local": _usage_result(1000.0, 400.0),
        "u115": _usage_result(500.0, 100.0),
    }

    with _patch_dirs(dirs), patch.object(
        dashboard_endpoint.StorageChain,
        "manage_storage",
        side_effect=lambda storage, action: usages[storage],
    ) as mocked_usage:
        ret = dashboard_endpoint._build_storage()

    assert {call.kwargs["storage"] for call in mocked_usage.call_args_list} == {"local", "u115"}
    assert ret.total_storage == 1500.0
    assert ret.used_storage == 1000.0


def test_storage_skips_dirs_without_storage_fields():
    """目录未填存储标识时不参与统计。"""
    dirs = [
        schemas.TransferDirectoryConf(name="空配置", download_path="/downloads"),
        schemas.TransferDirectoryConf(name="空媒体库存储", library_path="/media"),
    ]

    with _patch_dirs(dirs), patch.object(
        dashboard_endpoint.StorageChain, "manage_storage"
    ) as mocked_usage:
        ret = dashboard_endpoint._build_storage()

    mocked_usage.assert_not_called()
    assert ret.total_storage == 0
    assert ret.used_storage == 0


def test_storage_returns_zero_without_dirs():
    """未配置任何目录时返回 0。"""
    with _patch_dirs([]), patch.object(
        dashboard_endpoint.StorageChain, "manage_storage"
    ) as mocked_usage:
        ret = dashboard_endpoint._build_storage()

    mocked_usage.assert_not_called()
    assert ret.total_storage == 0
    assert ret.used_storage == 0


def test_storage_ignores_failed_usage_result():
    """存储查询失败时不计入统计。"""
    dirs = [
        schemas.TransferDirectoryConf(
            name="下载目录",
            storage="local",
            download_path="/downloads",
        )
    ]

    with _patch_dirs(dirs), patch.object(
        dashboard_endpoint.StorageChain,
        "manage_storage",
        return_value={"success": False, "message": "该存储类型未启用或不支持此管理动作"},
    ):
        ret = dashboard_endpoint._build_storage()

    assert ret.total_storage == 0
    assert ret.used_storage == 0
