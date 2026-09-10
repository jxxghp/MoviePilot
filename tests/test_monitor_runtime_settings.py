from pathlib import Path
from unittest.mock import patch

from app.monitor.dispatcher import TransferDispatcher


def test_transfer_dispatcher_reads_runtime_extensions_at_use_time() -> None:
    """验证已创建的监控分发器会读取最新的整理后缀配置。"""
    runtime_settings = {
        "DOWNLOAD_TMPEXT": [".part"],
        "RMT_MEDIAEXT": [".mkv"],
        "RMT_SUBEXT": [".srt"],
        "RMT_AUDIOEXT": [".flac"],
    }

    with patch(
        "app.monitor.dispatcher.get_runtime_setting",
        side_effect=runtime_settings.__getitem__,
    ):
        dispatcher = TransferDispatcher(cache={})

        assert dispatcher.is_transfer_candidate_path(Path("movie.mkv"))

        runtime_settings["RMT_MEDIAEXT"] = [".mp4"]

        assert not dispatcher.is_transfer_candidate_path(Path("movie.mkv"))
        assert dispatcher.is_transfer_candidate_path(Path("movie.mp4"))
