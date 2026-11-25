"""
真实种子与磁力的 v1/v2 下载身份回归。
"""

import base64
import hashlib

import pytest
from torrentool.bencode import Bencode

from app.domain.torrent import download_hashes


@pytest.mark.parametrize("version", ["v1", "v2", "hybrid"])
def test_download_hashes_preserves_binary_info_identity(version):
    """
    v2 二进制 pieces root 不能因字符串解码替换而改变真实哈希。
    """
    info = {"name": "Show", "piece length": 16384}
    if version in {"v1", "hybrid"}:
        info.update({"length": 1, "pieces": b"\xff" * 20})
    if version in {"v2", "hybrid"}:
        info.update({"meta version": 2, "file tree": {"show.mkv": {"": {
            "length": 1, "pieces root": b"\xff" * 32,
        }}}})
    raw_info = Bencode.encode(info)
    content = Bencode.encode({"info": info, "announce": "https://tracker.invalid/announce"})
    expected = set()
    if version in {"v1", "hybrid"}:
        expected.add(hashlib.sha1(raw_info).hexdigest())
    if version in {"v2", "hybrid"}:
        digest = hashlib.sha256(raw_info).hexdigest()
        expected.update((digest, digest[:40]))

    assert download_hashes(content) == expected


def test_download_hashes_reads_hex_base32_and_v2_magnets():
    """
    磁力中的 v1 十六进制/base32 与 v2 multihash 均保持精确身份。
    """
    v1 = "ab" * 20
    v2 = "cd" * 32
    base32 = base64.b32encode(bytes.fromhex(v1)).decode()
    assert download_hashes(f"magnet:?xt=urn:btih:{v1.upper()}") == {v1}
    assert download_hashes(f"magnet:?xt=urn:btih:{base32}") == {v1}
    assert download_hashes(f"magnet:?xt=urn:btih:{v1}&xt=urn:btmh:1220{v2}") == {v1, v2, v2[:40]}


@pytest.mark.parametrize("content", [
    b"", b"torrent", b"d4:infod4:name4:Show6:pieces20:abce",
    b"d4:infod4:name4:Showe4:infod6:pieces0:ee", b"d4:infodeeextra",
    b"d4:infod6:pieces0:6:lengthi+1eee", b"d4:infod06:pieces0:ee",
    b"d4:infod6:pieces0:4:listdi1ei2eeee", b"d4:infod6:pieces0:6:lengthi-0eee",
    "magnet:?xt=urn:btih:invalid", "magnet:?xt=urn:btmh:1220abcd", "https://tracker.invalid/file",
])
def test_download_hashes_rejects_unconfirmed_identity(content):
    """
    格式无效或缺少身份时不得生成名称/大小兜底身份。
    """
    assert not download_hashes(content)
