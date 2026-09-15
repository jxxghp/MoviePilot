"""下载应用服务共享的输入校验。"""

from __future__ import annotations

import re

_TORRENT_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


def validate_torrent_hash(hash_value: str) -> None:
    """校验 BitTorrent v1 Hash，拒绝模糊任务定位。"""
    if not _TORRENT_HASH_PATTERN.fullmatch(hash_value):
        raise ValueError("hash 格式无效")
