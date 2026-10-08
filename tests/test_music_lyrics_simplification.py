"""歌词字形设置、格式保护及本地和远端写入回归。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.settings.contract import ALL_SETTING_SPECS, build_value_schema
from app.chain.scraping import ScrapingChain
from app.domain.context import MusicLyrics
from app.runtime.config import ConfigModel, Settings, settings
from app.schemas.file import FileItem


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("metadata_enabled", [False, True])
@pytest.mark.parametrize("synced", [False, True])
@pytest.mark.parametrize("storage", ["local", "u115"])
def test_lyrics_write_uses_independent_simplification_setting(
        tmp_path, monkeypatch, enabled, metadata_enabled, synced, storage):
    """两种存储和歌词格式只受歌词开关控制，不修改候选、标签或时间轴。"""
    monkeypatch.setattr(settings, "MUSIC_LYRICS_TO_SIMPLIFIED", enabled)
    monkeypatch.setattr(settings, "MUSIC_METADATA_TO_SIMPLIFIED", metadata_enabled)
    original = (
        "[ar:周杰倫]\r\n[ti:愛的樂章]\r\n[offset:-120]\r\n"
        "[00:01.00][01:02.345]愛與夢想 [讓夢飛揚]\r\n"
        "[00:03:04]<03:04.50>讓<03:05.600>夢飛揚 English かな\r\n"
    ) if synced else "[愛與夢想]\r\n讓夢飛揚 English かな\r\n"
    simplified = original.replace("愛與夢想", "爱与梦想").replace("讓", "让").replace("夢飛揚", "梦飞扬")
    lyrics = MusicLyrics(
        provider="lrclib",
        synced_lyrics=original if synced else None,
        plain_lyrics=None if synced else original,
    )
    local_path = tmp_path / "track.flac"
    target_path = local_path if storage == "local" else Path("/Music/track.flac")
    chain = object.__new__(ScrapingChain)
    chain.storagechain = Mock()
    chain.storagechain.get_parent_item.return_value = FileItem(storage=storage, path="/Music/", type="dir")
    uploaded: list[tuple[str, bytes]] = []

    def upload(_parent, path, new_name):
        """在临时歌词被清理前读取真实上传内容。"""
        uploaded.append((new_name, path.read_bytes()))
        return True

    chain.storagechain.upload_file.side_effect = upload
    assert chain._write_music_lyrics_sidecar(
        FileItem(storage=storage, path=str(target_path), name="track.flac", type="file"),
        local_path, lyrics, overwrite=False,
    )
    expected = f"{(simplified if enabled else original).rstrip()}\n".encode("utf-8")
    if storage == "local":
        assert local_path.with_suffix(lyrics.extension).read_bytes() == expected
    else:
        assert uploaded == [(f"track{lyrics.extension}", expected)]
        assert not local_path.with_suffix(lyrics.extension).exists()
    assert lyrics.content == original


@pytest.mark.parametrize("storage", ["local", "u115"])
def test_lyricsfile_original_is_preserved_while_compatible_lrc_is_simplified(
        tmp_path, monkeypatch, storage):
    """AMLL 或插件提供的 Lyricsfile 原文保持不变，仅转换播放器兼容歌词。"""
    monkeypatch.setattr(settings, "MUSIC_LYRICS_TO_SIMPLIFIED", True)
    original = (
        "version: '1.0'\nmetadata: {title: 愛的樂章, artist: 周杰倫}\n"
        'lines:\n  - start_ms: 1000\n    text: "愛與夢想"\n'
    )
    lyrics = MusicLyrics(provider="amll", lyricsfile=original)
    chain = object.__new__(ScrapingChain)
    chain.storagechain = Mock()
    chain.storagechain.get_parent_item.return_value = FileItem(storage=storage, path="/Music/", type="dir")
    uploaded: dict[str, bytes] = {}

    def upload(_parent, path, new_name):
        """记录远端 LRC 和 Lyricsfile 的独立内容。"""
        uploaded[new_name] = path.read_bytes()
        return True

    chain.storagechain.upload_file.side_effect = upload
    local_path = tmp_path / "track.flac"
    target_path = local_path if storage == "local" else Path("/Music/track.flac")
    assert chain._write_music_lyrics_sidecar(
        FileItem(storage=storage, path=str(target_path), name="track.flac", type="file"),
        local_path, lyrics, overwrite=False,
    )
    saved = (
        {name: (tmp_path / name).read_bytes() for name in ("track.lrc", "track.lyricsfile.yaml")}
        if storage == "local" else uploaded
    )
    assert saved["track.lrc"] == "[00:01.00]爱与梦想\n".encode("utf-8")
    assert saved["track.lyricsfile.yaml"] == original.encode("utf-8")
    assert lyrics.lyricsfile == original
    assert lyrics.content == "[00:01.00]愛與夢想"


def test_lyrics_simplification_default_and_configuration_contract(monkeypatch):
    """默认关闭，设置目录公开布尔字段，Chain 快照随更新读取独立值。"""
    assert ConfigModel.model_fields["MUSIC_LYRICS_TO_SIMPLIFIED"].default is False
    spec = ALL_SETTING_SPECS["MUSIC_LYRICS_TO_SIMPLIFIED"]
    assert spec.group == "music"
    assert build_value_schema(spec)["type"] == "boolean"
    for enabled in (True, False):
        monkeypatch.setattr(settings, "MUSIC_LYRICS_TO_SIMPLIFIED", enabled)
        assert get_chain_runtime_config_snapshot().music_lyrics_to_simplified is enabled


@pytest.mark.parametrize("enabled", [False, True])
def test_lyrics_simplification_setting_persists_and_reloads(tmp_path, monkeypatch, enabled):
    """设置页写入的布尔值可通过环境文件保存，重新加载仍保持独立开关。"""
    env_path = tmp_path / "app.env"
    env_path.touch()
    monkeypatch.delenv("MUSIC_LYRICS_TO_SIMPLIFIED", raising=False)
    monkeypatch.setattr("app.runtime.config.get_env_path", lambda: env_path)
    config = Settings(_env_file=env_path, MUSIC_LYRICS_TO_SIMPLIFIED=not enabled)
    success, message = config.update_setting("MUSIC_LYRICS_TO_SIMPLIFIED", enabled)
    assert success is True
    assert message == ""
    restored = Settings(_env_file=env_path)
    assert restored.MUSIC_LYRICS_TO_SIMPLIFIED is enabled


def test_enabling_simplification_still_respects_existing_lyrics_policy(tmp_path, monkeypatch):
    """开启转换不能绕过仅缺失策略，擅自重写用户已有歌词。"""
    monkeypatch.setattr(settings, "MUSIC_LYRICS_TO_SIMPLIFIED", True)
    audio = tmp_path / "track.flac"
    sidecar = audio.with_suffix(".lrc")
    sidecar.write_text("[00:01.00]愛與夢想", encoding="utf-8")
    chain = object.__new__(ScrapingChain)
    chain.storagechain = Mock()
    chain.storagechain.get_file_item.return_value = FileItem(storage="local", path=str(sidecar), type="file")
    lyrics_chain = Mock()
    assert chain._scrape_music_lyrics(
        FileItem(storage="local", path=str(audio), type="file"), audio, None,
        SimpleNamespace(is_skip=False, is_upgrade=False), False, lyrics_chain, None,
    ) == "existing"
    lyrics_chain.get_music_lyrics.assert_not_called()
    assert sidecar.read_text(encoding="utf-8") == "[00:01.00]愛與夢想"


@pytest.mark.parametrize("link_type", ["hardlink", "symlink"])
def test_simplification_replaces_linked_library_lyrics_without_changing_seed(
        tmp_path, monkeypatch, link_type):
    """对已有旁挂进行字形转换时，仅原子替换媒体库目录项，种源字节不变。"""
    monkeypatch.setattr(settings, "MUSIC_LYRICS_TO_SIMPLIFIED", True)
    seed = tmp_path / "seed.lrc"
    original = "[00:01.00]愛與夢想\n"
    seed.write_text(original, encoding="utf-8")
    audio = tmp_path / "track.flac"
    sidecar = audio.with_suffix(".lrc")
    if link_type == "hardlink":
        sidecar.hardlink_to(seed)
    else:
        sidecar.symlink_to(seed)
    chain = object.__new__(ScrapingChain)
    chain._remove_alternate_music_lyrics = Mock()
    assert chain._write_music_lyrics_sidecar(
        FileItem(storage="local", path=str(audio), type="file"), audio,
        MusicLyrics(provider="lrclib", synced_lyrics=original), overwrite=True,
    )
    assert seed.read_text(encoding="utf-8") == original
    assert sidecar.read_text(encoding="utf-8") == "[00:01.00]爱与梦想\n"
    assert not sidecar.is_symlink()
