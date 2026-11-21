# -*- coding: utf-8 -*-
"""订阅自定义识别词快照用例：下载时保存完整识别词，整理时快照优先、实时反查兜底。

回归场景：订阅做季+集组合偏移（如 S04E05→S01E71），下载阶段生效但整理阶段因实时反查订阅
返回空而静默回退全局识别词、丢失偏移。修复后由订阅链在发起下载时将完整识别词作为入参传入
下载模块并存档（避免下载模块反查订阅的同级循环依赖），整理时优先复用该快照。
"""
from copy import deepcopy
from types import SimpleNamespace

from app.application.history import DownloadHistorySnapshot
from app.chain.transfer import TransferChain  # pylint: disable=no-name-in-module
from app.chain.transfer import records as mixins_module
from app.chain.transfer.request import _TransferCandidatePlanner
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType
from app.schemas.workflow import FileItem


def _fake_history(custom_words=None, note=None):
    """构造仅含测试所需字段的下载历史替身。"""
    return SimpleNamespace(custom_words=custom_words, note=note)


def _fake_download_history(**overrides):
    """构造包含整理元数据字段的不可变下载历史。"""
    values = {
        "id": 1,
        "path": "/downloads/Demo.Show",
        "custom_words": "保留快照",
        "note": {},
        "torrent_name": "Download.Title.S03E05.1080p.WEB-DL.x265-GROUP",
        "torrent_description": None,
        "type": MediaType.TV.value,
        "title": "Canonical Title",
        "year": "2024",
        "seasons": "S03",
        "episodes": "E05",
        "media_source": MediaSource.TMDB,
        "media_id": "123",
        "episode_group": None,
    }
    values.update(overrides)
    return DownloadHistorySnapshot(**values)


def test_transfer_prefers_snapshot_over_live_lookup(monkeypatch):
    """整理时存在下载快照，应直接使用快照且不触发实时反查订阅。"""
    called = {"lookup": False}

    class _GuardSubscribeChain:
        def get_subscribe_by_source(self, source):
            # 一旦走到实时反查即视为失败：快照存在时不应触发
            called["lookup"] = True
            return SimpleNamespace(custom_words="不应使用\n实时反查")

    monkeypatch.setattr(mixins_module, "SubscribeChain", _GuardSubscribeChain)

    history = _fake_history(
        custom_words="S04 => S01\n第 <> 集 >> EP+66",
        note={"source": "Subscribe|{...}"},
    )
    result = TransferChain._get_subscribe_custom_words(history)

    assert result == ["S04 => S01", "第 <> 集 >> EP+66"]
    assert called["lookup"] is False


def test_transfer_falls_back_to_live_lookup_without_snapshot(monkeypatch):
    """整理时无快照（历史旧记录），应按下载来源实时反查订阅取识别词。"""

    class _FakeSubscribeChain:
        def get_subscribe_by_source(self, source):
            assert source == "Subscribe|{...}"
            return SimpleNamespace(custom_words="A => B")

    monkeypatch.setattr(mixins_module, "SubscribeChain", _FakeSubscribeChain)

    history = _fake_history(custom_words=None, note={"source": "Subscribe|{...}"})
    result = TransferChain._get_subscribe_custom_words(history)

    assert result == ["A => B"]


def test_transfer_returns_none_when_unavailable(monkeypatch):
    """无下载记录、note 非字典、或来源反查不到订阅时返回 None（回退全局识别词）。"""

    class _NoneSubscribeChain:
        def get_subscribe_by_source(self, source):
            return None

    monkeypatch.setattr(mixins_module, "SubscribeChain", _NoneSubscribeChain)

    # 无下载记录
    assert TransferChain._get_subscribe_custom_words(None) is None
    # 无快照且 note 非字典：不应触发实时反查
    assert TransferChain._get_subscribe_custom_words(_fake_history(note="不是字典")) is None
    # 无快照、来源可解析但反查不到订阅
    assert (
        TransferChain._get_subscribe_custom_words(
            _fake_history(note={"source": "Subscribe|{}"})
        )
        is None
    )


def test_transfer_prefers_download_metadata_to_file_name():
    """自动整理应优先使用下载时确认的媒体、季集及资源属性。"""
    history = _fake_download_history(
        note={
            "meta_info": {
                "type": MediaType.TV.value,
                "begin_season": 3,
                "begin_episode": 5,
                "resource_pix": "1080p",
                "resource_type": "WEB-DL",
                "video_encode": "H265",
            },
            "media_file_count": 1,
        }
    )
    file_meta = MetaInfo("Wrong.Title.S01E09.2160p.BluRay.x264-OTHER")

    result = TransferChain._merge_download_meta(file_meta, history)

    assert result.name == "Canonical Title"
    assert result.year == "2024"
    assert result.begin_season == 3
    assert result.begin_episode == 5
    assert result.resource_pix == "1080p"
    assert result.resource_type == "WEB-DL"
    assert result.video_encode == "H265"
    assert result.media_source == MediaSource.TMDB
    assert result.media_id == "123"


def test_transfer_detaches_apply_words_from_frozen_download_snapshot():
    """恢复非空识别词时应生成普通列表，确保整理规划可以深拷贝元数据。"""
    apply_words = ["Original => Canonical"]
    history = _fake_download_history(
        note={
            "meta_info": {
                "type": MediaType.TV.value,
                "apply_words": apply_words,
            },
            "media_file_count": 1,
        }
    )

    result = TransferChain._merge_download_meta(None, history)
    copied = deepcopy(result)

    assert result.apply_words == apply_words
    assert type(result.apply_words) is list
    assert copied.apply_words == apply_words
    result.apply_words.append("Another => Value")
    assert history.note["meta_info"]["apply_words"] == apply_words


def test_transfer_uses_file_episode_for_multi_file_download():
    """多文件季包复用订阅季和标题，但每个文件仍保留自身集数。"""
    history = _fake_download_history(
        episodes="E01-E12",
        note={
            "meta_info": {
                "type": MediaType.TV.value,
                "begin_season": 3,
                "begin_episode": 1,
                "end_episode": 12,
                "total_episode": 12,
                "resource_pix": "1080p",
            },
            "media_file_count": 12,
        },
    )
    file_meta = MetaInfo("Wrong.Title.S01E04.2160p.mkv")

    result = TransferChain._merge_download_meta(file_meta, history)

    assert result.name == "Canonical Title"
    assert result.begin_season == 3
    assert result.begin_episode == 4
    assert result.end_episode is None
    assert result.resource_pix == "1080p"


def test_transfer_restores_episode_range_for_single_file_download():
    """
    单个文件包含多集时，自动整理应使用下载时确认的完整集数范围。
    """
    history = _fake_download_history(
        episodes="E04-E05",
        note={"media_file_count": 1},
    )
    file_meta = MetaInfo("Wrong.Title.S01E04.2160p.mkv")

    result = TransferChain._merge_download_meta(file_meta, history)

    assert result.begin_season == 3
    assert result.begin_episode == 4
    assert result.end_episode == 5
    assert result.episode_list == [4, 5]


def test_transfer_restores_legacy_history_columns_without_snapshot():
    """无结构化快照的旧下载也应优先复用标题、季集和媒体身份。"""
    history = _fake_download_history(note={"source": "Subscribe|{}"})
    file_meta = MetaInfo("Unrelated.File.S01E09.2160p.mkv")

    result = TransferChain._merge_download_meta(file_meta, history)

    assert result.name == "Canonical Title"
    assert result.begin_season == 3
    assert result.begin_episode == 5
    assert result.media_source == MediaSource.TMDB
    assert result.media_id == "123"


def test_transfer_keeps_file_metadata_for_movie_collection_conflict():
    """电影合集内年份不同的文件不能复用代表首部电影的下载元数据。"""
    history = _fake_download_history(
        type=MediaType.MOVIE.value,
        title="Collection First Movie",
        year="2020",
        seasons=None,
        episodes=None,
    )
    file_meta = MetaInfo("Collection.Second.Movie.2022.1080p.mkv")

    result = TransferChain._merge_download_meta(file_meta, history)

    assert result is file_meta
    assert result.name == "Collection Second Movie"
    assert result.year == "2022"


def test_manual_transfer_keeps_explicit_file_metadata():
    """手动整理必须保留用户路径识别结果，不得被关联下载历史覆盖。"""
    chain = SimpleNamespace(
        _merge_download_meta=lambda *_args: (_ for _ in ()).throw(
            AssertionError("手动整理不应合并下载历史")
        )
    )
    planner = _TransferCandidatePlanner(
        chain,
        meta=None,
        season=None,
        formater=None,
        batch_mtype=MediaType.TV,
        mediainfo=None,
        continue_callback=None,
        has_episode_format_template=False,
        transfer_exclude_words=None,
        download_hash=None,
        sync_extra_files=False,
        fileitem=SimpleNamespace(),
        manual=True,
    )
    file_meta = MetaInfo("Manual.Title.S01E09.2160p.mkv")
    planner._build_path_meta = lambda *_args, **_kwargs: file_meta

    result = planner._build_file_meta(
        FileItem(storage="local", path="/downloads/Manual.Title.S01E09.2160p.mkv"),
        history_record=_fake_download_history(),
    )

    assert result is file_meta
    assert result.begin_season == 1
    assert result.begin_episode == 9


def test_transfer_file_context_restores_download_metadata():
    """候选上下文重构后，自动影视整理仍应传递下载快照并复用确认过的季集。"""
    chain = SimpleNamespace(
        _restore_music_download_context=lambda **_kwargs: (None, None),
        _get_subscribe_custom_words=lambda _history: None,
        _merge_download_meta=TransferChain._merge_download_meta,
    )
    fileitem = FileItem(storage="local", path="/downloads/Wrong.Title.S01E09.mkv")
    planner = _TransferCandidatePlanner(
        chain,
        meta=None,
        season=None,
        formater=None,
        batch_mtype=MediaType.TV,
        mediainfo=None,
        continue_callback=None,
        has_episode_format_template=False,
        transfer_exclude_words=None,
        download_hash=None,
        sync_extra_files=False,
        fileitem=fileitem,
        manual=False,
    )
    planner._build_path_meta = lambda *_args, **_kwargs: MetaInfo("Wrong.Title.S01E09.mkv")

    result, music_info = planner._build_file_context(
        fileitem, _fake_download_history(), None, False,
    )

    assert result.name == "Canonical Title"
    assert result.begin_season == 3
    assert result.begin_episode == 5
    assert result.media_id == "123"
    assert music_info is None
