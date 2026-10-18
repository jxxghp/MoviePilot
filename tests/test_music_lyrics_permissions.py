"""
本地歌词原子写入的权限、链接保护及失败清理回归。
"""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.chain import scraping

pytestmark = pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 平台支持完整文件权限语义")


@pytest.mark.parametrize("extension", [".txt", ".lrc", ".lyricsfile.yaml"])
@pytest.mark.parametrize("umask,expected_mode", [(0o000, 0o666), (0o022, 0o644), (0o027, 0o640), (0o077, 0o600)])
def test_new_music_sidecar_respects_umask(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str, umask: int, expected_mode: int,
) -> None:
    """
    新建三种歌词旁挂均遵循部署权限规则，允许媒体服务器按配置读取。
    """
    monkeypatch.setattr(scraping, "current_umask", umask)
    target = tmp_path / f"track{extension}"

    scraping.ScrapingChain._write_music_text_sidecar(target, "南方姑娘\n")

    assert target.stat().st_mode & 0o777 == expected_mode
    assert target.read_text(encoding="utf-8") == "南方姑娘\n"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("original_mode", [0o600, 0o664])
@pytest.mark.parametrize("umask,expected_mode", [(0o022, 0o644), (0o077, 0o600)])
def test_replaced_music_sidecar_respects_umask(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original_mode: int, umask: int, expected_mode: int,
) -> None:
    """
    覆盖写入同样应用部署规则，修正旧私有歌词并尊重显式限制读取的部署。
    """
    monkeypatch.setattr(scraping, "current_umask", umask)
    target = tmp_path / "track.txt"
    target.write_text("旧歌词", encoding="utf-8")
    target.chmod(original_mode)

    scraping.ScrapingChain._write_music_text_sidecar(target, "新歌词")

    assert target.stat().st_mode & 0o777 == expected_mode
    assert target.read_text(encoding="utf-8") == "新歌词\n"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("link_type", ["hardlink", "symlink", "dangling_symlink"])
def test_music_sidecar_permissions_do_not_change_linked_source(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_type: str,
) -> None:
    """
    仅设置独立副本权限并替换目录项，种源字节和权限均不受影响。
    """
    monkeypatch.setattr(scraping, "current_umask", 0o022)
    source = tmp_path / "seed.lrc"
    target = tmp_path / "track.lrc"
    if link_type != "dangling_symlink":
        source.write_text("种源歌词", encoding="utf-8")
        source.chmod(0o640)
    if link_type == "hardlink":
        target.hardlink_to(source)
    else:
        target.symlink_to(source)

    scraping.ScrapingChain._write_music_text_sidecar(target, "媒体库歌词")

    assert not target.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.read_text(encoding="utf-8") == "媒体库歌词\n"
    if link_type == "dangling_symlink":
        assert not source.exists()
    else:
        assert source.read_text(encoding="utf-8") == "种源歌词"
        assert source.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("operation", ["chmod", "replace"])
def test_music_sidecar_publish_failure_keeps_original_and_removes_temporary(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """
    权限设置或原子替换失败时保留原歌词，并清理未发布的临时文件。
    """
    target = tmp_path / "track.txt"
    target.write_text("原歌词", encoding="utf-8")
    target.chmod(0o640)
    monkeypatch.setattr(Path, operation, Mock(side_effect=PermissionError("拒绝发布")))

    with pytest.raises(PermissionError, match="拒绝发布"):
        scraping.ScrapingChain._write_music_text_sidecar(target, "新歌词")

    assert target.read_text(encoding="utf-8") == "原歌词"
    assert target.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [target]
