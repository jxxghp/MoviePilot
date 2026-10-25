"""
共用下载器摘要的原始单位、目录空间与普通仪表盘兼容回归。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.dependencies import dashboard as sampling
from app.api.endpoints import dashboard as endpoint
from app.schemas.dashboard import DownloaderInfo


@pytest.fixture
def downloader_sources(tmp_path, monkeypatch):
    """
    将所有目录和下载器 I/O 替换为可核验边界，不访问用户磁盘或网络。
    """
    paths = [tmp_path / "first", tmp_path / "second"]
    for path in paths:
        path.mkdir()
    directories = Mock(return_value=[SimpleNamespace(download_path=str(path)) for path in paths])
    usage = Mock(return_value=(8192, 4096))
    transfers = Mock(return_value=[DownloaderInfo(download_speed=0, upload_speed=0)])
    monkeypatch.setattr(sampling.DirectoryHelper, "get_local_download_dirs", directories)
    monkeypatch.setattr(sampling.SystemUtils, "space_usage", usage)
    monkeypatch.setattr(sampling.DashboardChain, "downloader_info", transfers)
    return SimpleNamespace(paths=paths, directories=directories, usage=usage, transfers=transfers)


def test_downloader_aggregates_original_bytes_and_forwards_disk_policy(downloader_sources):
    """
    多下载器的全局速率包含做种上传，原累计量及目录去重参数不丢失。
    """
    case = downloader_sources
    case.transfers.return_value = [
        DownloaderInfo(download_speed=2048, upload_speed=512, download_size=100, upload_size=200),
        DownloaderInfo(download_speed=0, upload_speed=1536, download_size=300, upload_size=400),
    ]
    result = sampling.sample_downloader("chosen", btrfs_fsid_dedup=True, require_source=True)
    assert result.model_dump() == dict(
        download_speed=2048, upload_speed=2048, download_size=400, upload_size=600, free_space=4096
    )
    case.transfers.assert_called_once_with("chosen")
    case.usage.assert_called_once_with(case.paths, btrfs_fsid_dedup=True)


@pytest.mark.parametrize("transfers", [None, []])
def test_downloader_absent_source_is_strict_only(downloader_sources, transfers):
    """
    小组件不能把采样失败当零，普通仪表盘仍保留既有全部零的返回值。
    """
    case = downloader_sources
    case.transfers.return_value = transfers
    with pytest.raises(ValueError, match="暂不可用"):
        sampling.sample_downloader(require_source=True)
    assert endpoint._build_downloader() == DownloaderInfo()


@pytest.mark.parametrize("field", ["download_speed", "upload_speed"])
@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf")])
def test_downloader_strict_speed_requires_every_source(downloader_sources, field, value):
    """
    即使另一个下载器有效，也不能将缺失或非法的速率漏计为有效总量。
    """
    case = downloader_sources
    case.transfers.return_value = [DownloaderInfo(), DownloaderInfo(**{field: value})]
    with pytest.raises(ValueError, match="速度来源"):
        sampling.sample_downloader(require_source=True)


def test_downloader_aggregate_overflow_is_not_fresh_data(downloader_sources):
    """
    单项有限但求和溢出的速率仍应拒绝，不向原生端输出无限值。
    """
    downloader_sources.transfers.return_value = [DownloaderInfo(download_speed=1e308) for _ in range(2)]
    with pytest.raises(ValueError, match="速度来源"):
        sampling.sample_downloader(require_source=True)


@pytest.mark.parametrize(
    "usage, expected",
    [
        ((8192, 0), 0),
        ((0, 0), None),
        ((8192, -1), None),
        ((8192, 8193), None),
        ((float("inf"), 1), None),
        ((8192, float("nan")), None),
    ],
)
def test_downloader_disk_unknown_is_not_full(downloader_sources, usage, expected):
    """
    有效零剩余代表磁盘满，缺少总容量、越界或非有限容量代表未知。
    """
    downloader_sources.usage.return_value = usage
    result = sampling.sample_downloader(require_source=True)
    assert result.download_speed == 0 and result.upload_speed == 0
    assert result.free_space == expected


@pytest.mark.parametrize("missing", ["none", "partial", "error"])
def test_downloader_missing_disk_preserves_valid_rates(downloader_sources, missing):
    """
    未配置、部分目录失效或磁盘读取失败只标容量未知，速度仍保持可用。
    """
    case = downloader_sources
    if missing == "none":
        case.directories.return_value = []
    elif missing == "partial":
        case.paths[0].rmdir()
    else:
        case.usage.side_effect = OSError("private-path")
    result = sampling.sample_downloader(require_source=True)
    assert result.free_space is None and result.download_speed == 0
    if missing != "error":
        case.usage.assert_not_called()


def test_downloader_regular_page_retains_nullable_fields_and_disk_error(downloader_sources):
    """
    提取公共采样不改变原网页的空字段聚合及磁盘异常传播行为。
    """
    case = downloader_sources
    case.transfers.return_value = [DownloaderInfo(download_speed=None, upload_size=None)]
    assert endpoint._build_downloader().model_dump() == dict(
        download_speed=0, upload_speed=0, download_size=0, upload_size=0, free_space=4096
    )
    case.usage.side_effect = OSError("unreadable")
    with pytest.raises(OSError, match="unreadable"):
        endpoint._build_downloader()
