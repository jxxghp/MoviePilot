"""PT 音乐目录名称在两种解析引擎下保留作品、年份和版本证据。"""

from pathlib import Path

import pytest

from app.adapters.system import rust
from app.domain.meta import runtime
from app.domain.meta.metamusic import MetaMusic


@pytest.fixture(params=["python", "rust"])
def parser_engine(request, monkeypatch):
    """为实际文件名与目录上下文切换解析引擎，Rust 不可用时明确跳过。"""
    if request.param == "rust":
        if not rust.is_available():
            pytest.skip("moviepilot_rust 扩展未安装")
        monkeypatch.setattr(rust, "is_enabled", lambda: True)

        def reject_python_fallback(*_args, **_kwargs):
            """确保双引擎回归实际进入 Rust，而非隐式回退 Python。"""
            raise AssertionError("目录证据样本不应从 Rust 回退 Python")

        monkeypatch.setattr(MetaMusic, "_prepare_name_context", reject_python_fallback)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if request.param == "rust" else None)
    return request.param


@pytest.mark.parametrize("name", [
    "周杰伦 - 2001 - 范特西 [FLAC]", "周杰伦-2001-范特西 [FLAC]",
    "Daft Punk - 2001 - Discovery [FLAC]",
])
def test_middle_year_is_shared_by_resource_and_directory(name, parser_engine):
    """有三个明确部分时，各入口应使用相同艺人、专辑和年份。"""
    resource = MetaMusic.parse_resource(name)
    directory = MetaMusic.parse_album_dir(name)
    path = MetaMusic().apply_path_context(Path("/music") / name / "01.flac")

    assert resource.year == directory["year"] == path.year == 2001
    assert resource.title == directory["album"] == path.album
    assert resource.artists == path.artists == [directory["artist"]]


@pytest.mark.parametrize("suffix", ["(Live)", "(Deluxe Edition)", "[Live]", "(Unknown Subtitle)", "（现场版）"])
def test_directory_keeps_identity_qualifiers(suffix, parser_engine):
    """括号中的版本及未知作品副标题不能作为规格噪声丢弃。"""
    name = f"Artist - Record {suffix} (2001) [FLAC 24bit-96kHz]"
    meta = MetaMusic().apply_path_context(Path("/music") / name / "03.flac")

    assert suffix.replace("（", "(").replace("）", ")") in meta.album
    assert meta.year == 2001
    assert meta.artists == ["Artist"]
    assert meta.track_number == 3
    assert meta.bit_depth == 24
    assert meta.sample_rate == 96000
    if suffix in {"(Live)", "[Live]"}:
        assert meta.version == "Live"


def test_tagged_track_version_is_not_inferred_from_album_directory():
    """现场专辑中的有标签曲目也可能是录音室附赠版，不能强写录音版本。"""
    meta = MetaMusic(title="Studio Bonus", artists=["Artist"], album="Record")

    meta.apply_path_context("/music/Artist - Record (Live) (2001)/03.flac")

    assert meta.title == "Studio Bonus"
    assert meta.album == "Record"
    assert meta.version is None


@pytest.mark.parametrize("name,album,year", [
    ("Taylor Swift - 1989 [FLAC]", "1989", None),
    ("2020 - Record (Live) [FLAC]", "Record (Live)", 2020),
    ("Artist - Best Album (2001) [FLAC]", "Best Album", 2001),
])
def test_directory_does_not_guess_numeric_names(name, album, year):
    """数字专辑名、年份前缀和自然标题保持原本语义。"""
    result = MetaMusic.parse_album_dir(name)

    assert result["album"] == album
    assert result["year"] == year
    if name.startswith("2020"):
        assert result["artist"] is None


def test_album_context_retains_directory_version():
    """整专匹配同样接收目录录音版本，不能只在单文件入口保留。"""
    meta = MetaMusic.from_album_context("Artist - Record (Live) (2001) [FLAC]", [])

    assert meta.title == "Record (Live)"
    assert meta.version == "Live"
