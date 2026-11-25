"""
音乐目录文件清单与下载历史回放；不注册测试用例。
"""

import shutil
from pathlib import Path

from app.application.history import DownloadHistorySnapshot
from app.schemas.types import MediaType


def _history(root: Path, **kwargs) -> DownloadHistorySnapshot:
    """
    构造只有站点原始名称、没有已选媒体身份的下载历史。
    """
    values = dict(
        id=1, path=str(root), type=MediaType.MUSIC.value, title="Unrecognized",
        torrent_name="Artist - Actual Album (2003) [FLAC]",
        torrent_description="音乐专辑", note=None,
    )
    values.update(kwargs)
    return DownloadHistorySnapshot(**values)


def _audio_files(root: Path) -> list[Path]:
    """
    生成无标签但具有真实时长和曲名的整专输入。
    """
    root.mkdir(parents=True)
    files = [root / "01 - First Song.flac", root / "02 - Second Song.flac"]
    for path in files:
        shutil.copyfile(Path(__file__).parents[2] / "fixtures/audio/silence.flac", path)
    return files
