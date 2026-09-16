"""真实站点主副标题的离线音乐识别回归，不包含站点凭据或网络访问。"""

import json
from pathlib import Path

import pytest

from app.adapters.system import rust
from app.domain.meta import runtime
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType

SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "music_metainfo_samples.json").read_text(encoding="utf-8")
)["samples"]


@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample["id"])
def test_real_music_titles_and_subtitles(sample, engine, monkeypatch):
    """两条真实 MetaInfo 路径须提取相同的名称证据，不能用 Python 回退冒充 Rust。"""
    if engine == "rust":
        if not rust.is_available():
            pytest.skip("moviepilot_rust 扩展未安装")
        monkeypatch.setattr(rust, "is_enabled", lambda: True)

        def reject_python_fallback(*_args, **_kwargs):
            """Rust 样本解析一旦回退 Python 就显式失败。"""
            raise AssertionError("真实音乐样本不应从 Rust 回退 Python")

        monkeypatch.setattr(MetaMusic, "_prepare_name_context", reject_python_fallback)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if engine == "rust" else None)

    meta = MetaInfo(sample["title"], sample["subtitle"], mtype=MediaType.MUSIC)

    assert meta.type == MediaType.MUSIC
    assert meta.org_string == sample["title"]
    for field, expected in sample["expected"].items():
        assert getattr(meta, field) == expected, field


@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("title, expected", [
    ("Artist - Best Album FLAC", "Best Album"),
    ("Artist - Single Ladies FLAC", "Single Ladies"),
    ("Artist - Live At Montreux 1999 2022 FLAC", "Live At Montreux 1999 2022"),
])
def test_release_cleanup_preserves_natural_titles(title, expected, engine, monkeypatch):
    """发行类型和年份规则不能删除作品名中的自然单词或连续年份。"""
    if engine == "rust" and not rust.is_available():
        pytest.skip("moviepilot_rust 扩展未安装")
    monkeypatch.setattr(rust, "is_enabled", lambda: True)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if engine == "rust" else None)

    meta = MetaInfo(title, mtype=MediaType.MUSIC)

    assert meta.title == expected
    assert meta.album is None


@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("subtitle", [
    "歌手：周杰伦 - 专辑：叶惠美 | FLAC | [2003]",
    "歌手：周杰伦；专辑：叶惠美；[2003] FLAC",
    "歌手：周杰伦，专辑：叶惠美 | [2003] FLAC",
])
def test_subtitle_labels_keep_album_and_year_boundaries(subtitle, engine, monkeypatch):
    """明确字段可补全缺失信息，平台分隔与后续字段不能污染署名或专辑名。"""
    if engine == "rust" and not rust.is_available():
        pytest.skip("moviepilot_rust 扩展未安装")
    monkeypatch.setattr(rust, "is_enabled", lambda: True)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if engine == "rust" else None)

    meta = MetaInfo("晴天", subtitle, mtype=MediaType.MUSIC)

    assert (meta.artists, meta.title, meta.album, meta.year) == (["周杰伦"], "晴天", "叶惠美", 2003)


@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("suffix, artists", [
    ("(featuring Guest)", ["Artist", "Guest"]),
    ("(feat. Artist & Guest)", ["Artist", "Guest"]),
    ("(feature presentation)", ["Artist"]),
])
def test_featured_credits_require_explicit_marker(suffix, artists, engine, monkeypatch):
    """仅提取明确的客串署名，保留原始曲名，去重且不误读普通注释。"""
    if engine == "rust" and not rust.is_available():
        pytest.skip("moviepilot_rust 扩展未安装")
    monkeypatch.setattr(rust, "is_enabled", lambda: True)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if engine == "rust" else None)

    meta = MetaInfo(f"Artist - Song {suffix} FLAC", mtype=MediaType.MUSIC)

    assert meta.artists == artists
    assert meta.title == f"Song {suffix}"


@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("title, subtitle, expected", [
    ("Shan.Ge.Liao.Zai.2023.FLAC", "音乐专辑 | 山歌廖哉 - 歌手：刀郎", "山歌廖哉"),
    ("Yisa Yu 2019 SanShiErLi FLAC", "郁可唯 - 三十而慄 2019 - FLAC 分軌", "三十而慄"),
    ("Shan Ge Liao Zai FLAC", "曲名：山高水长", "Shan Ge Liao Zai"),
    ("Artist - White Light 1971 FLAC", "歌手：歌手；曲名：白光", "White Light"),
    ("山歌廖哉 FLAC", "曲名：其他专辑", "山歌廖哉"),
])
def test_pinyin_title_prefers_verified_native_subtitle(title, subtitle, expected, engine, monkeypatch):
    """中文原文须与拼音逐字对应；同字数无关文案及正常外文名不能触发替换。"""
    if engine == "rust" and not rust.is_available():
        pytest.skip("moviepilot_rust 扩展未安装")
    monkeypatch.setattr(rust, "is_enabled", lambda: True)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if engine == "rust" else None)

    meta = MetaInfo(title, subtitle, mtype=MediaType.MUSIC)

    assert meta.title == expected
    assert meta.org_string == title
