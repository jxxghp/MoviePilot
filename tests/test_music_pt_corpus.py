"""真实 PT 标题和文件列表的离线回归；来源身份的验证另用录制元数据。"""

import asyncio
import json
import re
from copy import deepcopy
from pathlib import Path
from unicodedata import normalize

import pytest

from app.adapters.system import rust
from app.application.audio import AudioMetadataHelper
from app.application.music.observation import capture_music_recognition, music_request_timeout
from app.domain.context import MusicInfo
from app.domain.meta import runtime
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import match_music_resource
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.types import MediaSource, MediaType

CORPUS = json.loads((Path(__file__).parent / "fixtures/music_pt_corpus.json").read_text(encoding="utf-8"))
FIELDS = ("title", "artists", "album", "year", "track_number", "disc_number", "version")
RECORDINGS = json.loads((Path(__file__).parent / "fixtures/music_pt_musicbrainz.json").read_text(encoding="utf-8"))


def text_key(value):
    """标注允许全半角、空白和括号排版差异，保留字母、汉字与数字内容。"""
    return re.sub(r"[\W_]+", "", normalize("NFKC", value or "").casefold())


@pytest.fixture(params=["python", "rust"])
def engine(request, monkeypatch):
    """实际切换解析引擎，Rust 不得以 Python 回退冒充通过。"""
    if request.param == "rust":
        assert rust.is_available(), "锁定环境必须安装 moviepilot-rust"
        monkeypatch.setattr(rust, "is_enabled", lambda: True)

        def reject_fallback(*_args, **_kwargs):
            """发现 Rust 回退时立即失败，防止双路径覆盖失真。"""
            raise AssertionError("Rust must not fall back to Python")

        monkeypatch.setattr(MetaMusic, "_prepare_name_context", reject_fallback)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if request.param == "rust" else None)
    return request.param


@pytest.mark.usefixtures("engine")
def test_annotated_pt_torrent_corpus():
    """逐条验证 300 条有独立命名标注的样本，其余标题只验证可解析和原文保留。"""
    failures = []
    for sample in CORPUS["torrents"]:
        meta = MetaMusic.parse_resource(sample["title"], sample["subtitle"])
        assert meta.org_string == sample["title"]
        for field, expected in sample.get("expected", {}).items():
            actual = getattr(meta, field)
            equal = text_key(actual) == text_key(expected) if field == "title" else actual == expected
            if not equal:
                failures.append((sample["id"], field, expected, actual))
    assert not failures, failures


@pytest.mark.usefixtures("engine")
def test_real_pt_file_track_numbers():
    """文件名入口与实际文件读取入口均须保留 2596 条带曲序文件的数字证据。"""
    failures = []
    for sample in CORPUS["files"]:
        if "expected" not in sample:
            continue
        expected = sample["expected"]["track_number"]
        path = Path("/music") / sample["path"]
        for entry, meta in (
                ("name", MetaMusic.parse_query(path.name)),
                ("file", AudioMetadataHelper.read_filename(path)),
        ):
            if meta.track_number != expected:
                failures.append((sample["id"], entry, expected, meta.track_number))
    assert not failures, failures


def test_pt_corpus_engine_parity(monkeypatch):
    """所有 3874 条真实输入在两条引擎路径下返回一致的关键字段。"""
    assert rust.is_available()
    monkeypatch.setattr(rust, "is_enabled", lambda: True)
    python_rows = []
    rust_rows = []
    for accelerator, rows in ((None, python_rows), (rust, rust_rows)):
        monkeypatch.setattr(runtime, "_metainfo_accelerator", accelerator)
        for sample in CORPUS["torrents"]:
            meta = MetaMusic.parse_resource(sample["title"], sample["subtitle"])
            rows.append(tuple(getattr(meta, field) for field in FIELDS))
        for sample in CORPUS["files"]:
            meta = MetaMusic.parse_query(Path(sample["path"]).name)
            rows.append(tuple(getattr(meta, field) for field in FIELDS))
    assert python_rows == rust_rows


@pytest.mark.usefixtures("engine")
@pytest.mark.parametrize("filename,title,artists,number", [
    ("01. Britney Spears - …Baby One More Time.flac", "…Baby One More Time", ["Britney Spears"], 1),
    ("16. t.A.T.u. - All the Things She Said.flac", "All the Things She Said", ["t.A.T.u."], 16),
    ("09. 911 - Bodyshakin’.flac", "Bodyshakin’", ["911"], 9),
    ("01 - Artist - First.flac", "First", ["Artist"], 1),
    ("01 - Sonata Arctica - FullMoon.flac", "FullMoon", ["Sonata Arctica"], 1),
    ("01 - 1812 Overture.flac", "1812 Overture", [], 1),
    ("09 - 100,000 Years.flac", "100,000 Years", [], 9),
    ("01 - 5 A.M..flac", "5 A.M.", [], 1),
    ("10 - 12 Visions fugitives, Op. 22- Andante.flac", "12 Visions fugitives, Op. 22- Andante", [], 10),
    ("05 - Symphony No. 2 - V. Im Tempo des Scherzo.flac", "Symphony No. 2 - V. Im Tempo des Scherzo", [], 5),
    ("07 - Polonaise - Fantaisie, Op. 61 in A-flat.flac", "Polonaise - Fantaisie, Op. 61 in A-flat", [], 7),
])
def test_numbered_filename_keeps_work_and_credit(filename, title, artists, number):
    """真实数字艺人、数字曲名及古典乐章不能被曲序/碟号规则吃掉。"""
    meta = AudioMetadataHelper.read_filename(Path("/music") / filename)
    assert text_key(meta.title) == text_key(title)
    assert meta.artists == artists
    assert meta.track_number == number
    assert meta.disc_number is None


@pytest.mark.usefixtures("engine")
@pytest.mark.parametrize("index", [544, 545, 778, 998, 1039, 1040, 1080, 1110, 1112, 1199, 1234, 1235])
def test_cleaned_resources_still_require_exact_identity(index):
    """清理后的真实资源可严格匹配；错误艺人、作品、年份和录音版本仍不能自动命中。"""
    sample = CORPUS["torrents"][index]
    expected = sample["expected"]
    artists = expected.get("artists", ["Travis"])
    target = MusicInfo(music_type="album", title=expected["title"], artists=artists, year=str(expected["year"]))
    assert match_music_resource(target, sample["title"], sample["subtitle"]).status == "exact"
    for changes in (
            {"title": "Unrelated work"}, {"artists": ["Unrelated artist"]},
            {"year": str(expected["year"] + 10)}, {"version": "Live"},
    ):
        wrong = deepcopy(target)
        for field, value in changes.items():
            setattr(wrong, field, value)
        assert match_music_resource(wrong, sample["title"], sample["subtitle"]).status != "exact"


@pytest.mark.usefixtures("engine")
@pytest.mark.parametrize("title,expected", [
    ("Artist - Best Album FLAC", "Best Album"),
    ("Artist.Name-Best.Album.FLAC", "Best Album"),
    ("Artist - Special Album (2001) CD FLAC", "Special Album"),
    ("Artist - Single Ladies FLAC", "Single Ladies"),
    ("Artist - Vinyl Days FLAC", "Vinyl Days"),
    ("Artist - CD Is Dead FLAC", "CD Is Dead"),
    ("Artist - Record (Live) 2020 FLAC", "Record (Live)"),
])
def test_carrier_cleanup_preserves_natural_titles(title, expected):
    """发行规格必须有上下文，不能删除作品中的普通词或录音版本。"""
    assert MetaMusic.parse_resource(title).title == expected


def test_explicit_album_label_and_tags_keep_priority():
    """明确标签可使用格式词作作品名，文件名不能覆盖真实音频标签。"""
    assert MetaMusic.parse_resource("Song", "专辑：WAV").album == "WAV"
    meta = MetaMusic(title="Tagged title", artists=["Tagged artist"], track_number=8)
    meta.apply_path_context("/music/01. Britney Spears - …Baby One More Time.flac")
    assert (meta.title, meta.artists, meta.track_number) == ("Tagged title", ["Tagged artist"], 8)


@pytest.mark.usefixtures("engine")
@pytest.mark.parametrize("case", RECORDINGS["cases"], ids=lambda case: case["torrent_id"])
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_real_musicbrainz_candidate_replay(case, async_mode, monkeypatch):
    """真实候选集经同步/异步入口返回核对后的专辑 ID，歧义仍阻止自动认领。"""
    responses = {
        json.dumps([row["path"], row["params"]], sort_keys=True): row["response"]
        for row in RECORDINGS["requests"]
    }

    def request(path, params=None):
        """保留生产请求预算和歧义短路；未录制的请求直接失败，不能触网。"""
        if music_request_timeout() is None:
            return None
        return deepcopy(responses[json.dumps([path, params], sort_keys=True)])

    async def async_request(path, params=None):
        """异步回放同一候选集，避免网络和测试缓存改变识别顺序。"""
        return request(path, params)

    module = MusicBrainzModule()
    monkeypatch.setattr(module, "_request_json", request)
    monkeypatch.setattr(module, "_async_request_json", async_request)
    sample = next(row for row in CORPUS["torrents"] if row["id"] == case["torrent_id"])
    kwargs = {
        "meta": MetaMusic.parse_resource(sample["title"], sample["subtitle"]),
        "mtype": MediaType.MUSIC, "media_source": MediaSource.MusicBrainz,
        "music_type": "album", "cache": False,
    }
    with capture_music_recognition() as observation:
        result = asyncio.run(module.async_recognize_media(**kwargs)) if async_mode else module.recognize_media(**kwargs)
    expected = case["expected"]
    if expected:
        assert result is not None
        assert (result.media_id, result.title, result.artists, result.music_type) == (
            expected["media_id"], expected["title"], expected["artists"], "album",
        )
    else:
        assert result is None or not result.media_id
        assert observation.status == case["unmatched_status"] == "ambiguous"
