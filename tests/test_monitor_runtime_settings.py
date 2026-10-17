"""
验证已创建的监控分发器读取最新运行配置。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.monitor.dispatcher import TransferDispatcher
from app.runtime import settings as setting_ports


def test_transfer_dispatcher_reads_runtime_extensions_at_use_time() -> None:
    """
    验证已创建的监控分发器会读取最新的整理后缀配置。
    """
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


@pytest.mark.parametrize("configured", [False, True])
def test_runtime_setting_default_preserves_falsy_values_and_missing_error(monkeypatch, configured) -> None:
    """
    启动前后均保留假值，只有缺失配置才使用默认值，未给默认值时继续暴露错误。
    """
    values = SimpleNamespace(ENABLED=False)
    monkeypatch.setattr(setting_ports, "_legacy_settings", lambda: values)
    monkeypatch.setattr(
        setting_ports, "_provider",
        (lambda key: getattr(values, key)) if configured else None,
    )

    assert setting_ports.get_runtime_setting("ENABLED", True) is False
    default = object()
    assert setting_ports.get_runtime_setting("MISSING", default) is default
    with pytest.raises(AttributeError):
        setting_ports.get_runtime_setting("MISSING")
