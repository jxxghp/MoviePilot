"""
验证整理时附属文件的同步与路径处理。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.chain.transfer.facade import TransferChain
from app.schemas.file import FileItem
from app.schemas.transfer import EpisodeFormat
from app.schemas.types import MediaType
from tests.support.transfer.files import (
    FakeMeta,
    bind_empty_history_repositories,
    make_fileitem,
    make_transfer_chain,
)


@pytest.mark.parametrize("template", [None, "Test.Show.S01E{ep}.mkv"])
@pytest.mark.parametrize("offset,expected", [("+1", [1, 2]), ("-1", [-1, 0])])
def test_manual_batch_episode_offset_preserves_zero(monkeypatch, template, offset, expected) -> None:
    """
    手动批量整理经过真实文件识别、模板筛选和任务生成后，零集偏移只应用一次。
    """
    chain = make_transfer_chain()
    source_files = [
        make_fileitem("/downloads/Test.Show.S01E00.mkv"),
        make_fileitem("/downloads/Test.Show.S01E01.mkv"),
    ]
    source_directory = FileItem(storage="local", path="/downloads/", type="dir", name="downloads")
    planned_meta = []
    monkeypatch.setattr(
        chain, "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [(item, False) for item in source_files],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(chain, "_register_scrape_batch_task", lambda task: None)
    monkeypatch.setattr(chain, "_close_scrape_batch", lambda batch_id: None)

    def capture_transfer(task, callback=None):
        """
        记录最终整理任务的元数据，隔离真实文件转移。
        """
        planned_meta.append(task.meta)
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", capture_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.format.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )

    state, message = chain.do_transfer(
        fileitem=source_directory,
        mtype=MediaType.TV,
        manual=True,
        background=False,
        sync_extra_files=False,
        epformat=EpisodeFormat(format=template, offset=offset),
    )

    assert state is True
    assert message == ""
    assert [meta.begin_episode for meta in planned_meta] == expected
    assert [meta.episode_list for meta in planned_meta] == [[episode] for episode in expected]
    assert [meta.episode for meta in planned_meta] == [f"E{episode:02d}" for episode in expected]


def test_sync_extra_subtitle_inherits_matching_video_episode(monkeypatch):
    """
    同名随片字幕应继承对应视频集数，避免字幕自身识别错误时全部落到第一集。
    """
    chain = make_transfer_chain()
    planned = []
    main_ep1_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E01.2026.mkv"
    )
    main_ep2_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E02.2026.mkv"
    )
    ep1_subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E01.2026.zh-cn.srt"
    )
    ep2_subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E02.2026.zh-cn.srt"
    )
    parent_fileitem = FileItem(
        storage="local",
        path="/downloads/Test Show (2026)/",
        type="dir",
        name="Test Show (2026)",
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [
            (main_ep1_fileitem, False),
            (main_ep2_fileitem, False),
            (ep1_subtitle_fileitem, False),
            (ep2_subtitle_fileitem, False),
        ],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录实际创建的整理任务集数。
        """
        planned.append((task.fileitem.path, task.meta.begin_episode))
        return True, ""

    def fake_meta_info_path(path, custom_words=None, **kwargs):
        """
        模拟字幕文件自身会被误识别为第一集的场景。
        """
        file_name = Path(path).name
        if file_name.endswith(".mkv") and "S01E02" in file_name:
            return FakeMeta(2)
        return FakeMeta(1)

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", fake_meta_info_path)

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=parent_fileitem,
        background=False,
        sync_extra_files=True,
    )

    assert state is True
    assert errmsg == ""
    assert planned == [
        (main_ep1_fileitem.path, 1),
        (ep1_subtitle_fileitem.path, 1),
        (main_ep2_fileitem.path, 2),
        (ep2_subtitle_fileitem.path, 2),
    ]


def test_sync_extra_lrc_and_cover_follow_video_directory(monkeypatch):
    """
    影视目录中的 LRC 与标准封面应像字幕一样随主视频整理，封面只规划一次。
    """
    chain = make_transfer_chain()
    planned = []
    main_ep1_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E01.2026.mkv"
    )
    main_ep2_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E02.2026.mkv"
    )
    ep1_lyrics_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E01.2026.lrc"
    )
    cover_fileitem = make_fileitem("/downloads/Test Show (2026)/folder.jpg")
    parent_fileitem = FileItem(
        storage="local",
        path="/downloads/Test Show (2026)/",
        type="dir",
        name="Test Show (2026)",
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [
            (main_ep1_fileitem, False),
            (main_ep2_fileitem, False),
            (ep1_lyrics_fileitem, False),
            (cover_fileitem, False),
        ],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(chain, "_register_scrape_batch_task", lambda task: None)
    monkeypatch.setattr(chain, "_close_scrape_batch", lambda batch_id: None)

    def fake_handle_transfer(task, callback=None):
        """
        记录影视目录附属文件的整理顺序和继承集数。
        """
        planned.append((task.fileitem.path, task.meta.begin_episode))
        return True, ""

    def fake_meta_info_path(path, custom_words=None, **kwargs):
        """
        模拟主视频集数识别，附属文件继承对应主视频结果。
        """
        return FakeMeta(2 if "S01E02" in Path(path).name else 1)

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.format.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", fake_meta_info_path)

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=parent_fileitem,
        background=False,
        sync_extra_files=True,
    )

    assert state is True
    assert errmsg == ""
    assert planned == [
        (main_ep1_fileitem.path, 1),
        (ep1_lyrics_fileitem.path, 1),
        (cover_fileitem.path, 1),
        (main_ep2_fileitem.path, 2),
    ]


def test_transfer_chain_uses_configured_lyrics_extension():
    """
    整理链应按运行时歌词后缀配置识别自定义歌词文件。
    """
    chain = make_transfer_chain()
    chain._lyrics_exts = (".lyrics",)
    lyrics_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E01.2026.lyrics"
    )

    assert chain._is_configured_lyrics_file(lyrics_fileitem)
    assert chain._is_subtitle_file(lyrics_fileitem)
    assert chain._is_music_lyrics_file(lyrics_fileitem)
    assert chain._is_allowed_file(lyrics_fileitem)


def test_single_subtitle_transfer_reuses_same_name_video_episode(monkeypatch):
    """
    单独整理同名字幕时应复用主视频识别结果，不受 sync_extra_files 开关影响。
    """
    chain = make_transfer_chain()
    planned = []
    main_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E02.2026.mkv"
    )
    subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E02.2026.zh-cn.srt"
    )
    parent_fileitem = FileItem(
        storage="local",
        path="/downloads/Test Show (2026)/",
        type="dir",
        name="Test Show (2026)",
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [(subtitle_fileitem, False)],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录单独字幕整理时使用的集数。
        """
        planned.append((task.fileitem.path, task.meta.begin_episode))
        return True, ""

    def fake_meta_info_path(path, custom_words=None, **kwargs):
        """
        模拟字幕自身会被误识别为第一集，主视频可正确识别为第二集。
        """
        file_name = Path(path).name
        if file_name.endswith(".mkv"):
            return FakeMeta(2)
        return FakeMeta(1)

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr(
        "app.chain.transfer.request.StorageChain",
        lambda: SimpleNamespace(
            get_parent_item=lambda fileitem: parent_fileitem,
            list_files=lambda fileitem, recursion=False: [
                main_fileitem,
                subtitle_fileitem,
            ],
        ),
    )
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: SimpleNamespace(
        get_parent_item=lambda fileitem: parent_fileitem,
        list_files=lambda fileitem, recursion=False: [
            main_fileitem,
            subtitle_fileitem,
        ],
    ))
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", fake_meta_info_path)

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=subtitle_fileitem,
        background=False,
        sync_extra_files=False,
    )

    assert state is True
    assert errmsg == ""
    assert planned == [(subtitle_fileitem.path, 2)]


def test_single_video_transfer_lists_parent_once_for_same_name_extra(monkeypatch):
    """
    单文件视频整理只读取一次父目录，并只附带同名附加文件。
    """
    chain = make_transfer_chain()
    planned = []
    list_files_calls = []
    main_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E02.2026.mkv"
    )
    subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Test.Show.S01E02.2026.zh-cn.srt"
    )
    other_subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Other.Show.S01E02.2026.zh-cn.srt"
    )
    parent_fileitem = FileItem(
        storage="local",
        path="/downloads/Test Show (2026)/",
        type="dir",
        name="Test Show (2026)",
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [(main_fileitem, False)],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录单视频整理时实际附带的文件。
        """
        planned.append(task.fileitem.path)
        return True, ""

    def fake_list_files(fileitem, recursion=False):
        """
        记录父目录读取次数。
        """
        list_files_calls.append((fileitem.path, recursion))
        return [
            main_fileitem,
            subtitle_fileitem,
            other_subtitle_fileitem,
        ]

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr(
        "app.chain.transfer.request.StorageChain",
        lambda: SimpleNamespace(
            get_parent_item=lambda fileitem: parent_fileitem,
            list_files=fake_list_files,
        ),
    )
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: SimpleNamespace(
        get_parent_item=lambda fileitem: parent_fileitem,
        list_files=fake_list_files,
    ))
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", lambda path, custom_words=None, **kwargs: FakeMeta(2))

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=main_fileitem,
        background=False,
        sync_extra_files=False,
    )

    assert state is True
    assert errmsg == ""
    assert planned == [main_fileitem.path, subtitle_fileitem.path]
    assert list_files_calls == [(parent_fileitem.path, False)]


def test_episode_format_filters_extra_files_before_sync_planning(monkeypatch):
    """
    存在集数定位模板时，不匹配模板的附加文件不应被主视频带入整理计划。
    """
    chain = make_transfer_chain()
    planned = []
    main_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.mkv"
    )
    subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.sc.ass"
    )
    parent_fileitem = FileItem(
        storage="local",
        path="/downloads/Test Show (2026)/",
        type="dir",
        name="Test Show (2026)",
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [
            (main_fileitem, False),
            (subtitle_fileitem, False),
        ],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录进入整理执行阶段的文件。
        """
        planned.append(task.fileitem.path)
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", lambda path, custom_words=None, **kwargs: FakeMeta(1))

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=parent_fileitem,
        background=False,
        sync_extra_files=True,
        epformat=EpisodeFormat(format="Show - {ep}.mkv"),
    )

    assert state is True
    assert errmsg == ""
    assert planned == [main_fileitem.path]


def test_episode_format_keeps_matching_extra_files_following_main(monkeypatch):
    """
    附加文件自身匹配集数定位模板时，仍可跟随同名主视频整理。
    """
    chain = make_transfer_chain()
    planned = []
    main_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.mkv"
    )
    subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.ass"
    )
    parent_fileitem = FileItem(
        storage="local",
        path="/downloads/Test Show (2026)/",
        type="dir",
        name="Test Show (2026)",
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [
            (main_fileitem, False),
            (subtitle_fileitem, False),
        ],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录进入整理执行阶段的文件和集数。
        """
        planned.append((task.fileitem.path, task.meta.begin_episode))
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", lambda path, custom_words=None, **kwargs: FakeMeta(1))

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=parent_fileitem,
        background=False,
        sync_extra_files=True,
        epformat=EpisodeFormat(format="Show - {ep}.{a}"),
    )

    assert state is True
    assert errmsg == ""
    assert planned == [
        (main_fileitem.path, 1),
        (subtitle_fileitem.path, 1),
    ]


def test_single_matching_subtitle_uses_unmatched_video_only_as_context(monkeypatch):
    """
    单独整理匹配模板的字幕时，同名主视频只提供识别上下文，不会被额外加入整理计划。
    """
    chain = make_transfer_chain()
    planned = []
    main_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 02.mkv"
    )
    subtitle_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 02.ass"
    )
    parent_fileitem = FileItem(
        storage="local",
        path="/downloads/Test Show (2026)/",
        type="dir",
        name="Test Show (2026)",
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [(subtitle_fileitem, False)],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录单独字幕整理时实际使用的集数。
        """
        planned.append((task.fileitem.path, task.meta.begin_episode))
        return True, ""

    def fake_meta_info_path(path, custom_words=None, **kwargs):
        """
        模拟字幕自身识别不准，但同名主视频可提供正确集数。
        """
        file_name = Path(path).name
        if file_name.endswith(".mkv"):
            return FakeMeta(2)
        return FakeMeta(1)

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr(
        "app.chain.transfer.request.StorageChain",
        lambda: SimpleNamespace(
            get_parent_item=lambda fileitem: parent_fileitem,
            list_files=lambda fileitem, recursion=False: [
                main_fileitem,
                subtitle_fileitem,
            ],
        ),
    )
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: SimpleNamespace(
        get_parent_item=lambda fileitem: parent_fileitem,
        list_files=lambda fileitem, recursion=False: [
            main_fileitem,
            subtitle_fileitem,
        ],
    ))
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", fake_meta_info_path)

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=subtitle_fileitem,
        background=False,
        sync_extra_files=True,
        epformat=EpisodeFormat(format="Show - {ep}.ass"),
    )

    assert state is True
    assert errmsg == ""
    assert planned == [(subtitle_fileitem.path, 2)]


def test_cleanup_dest_fileitem_is_checkpointed_only_after_allowed_items_exist(monkeypatch):
    """
    旧目标清理意图只应在模板筛选后确实存在待整理任务时进入规划输入。
    """
    chain = make_transfer_chain()
    planned = []
    main_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.mkv"
    )
    old_dest_fileitem = make_fileitem(
        "/library/Test Show/Show - 01.mkv"
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [(main_fileitem, False)],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录实际任务携带的冻结 cleanup intent。
        """
        planned.append(
            (
                task.fileitem.path,
                task.planning_input.options.get("cleanup_dest_fileitem"),
            )
        )
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr("app.chain.transfer.request.MetaInfoPath", lambda path, custom_words=None, **kwargs: FakeMeta(1))

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=main_fileitem,
        background=False,
        epformat=EpisodeFormat(format="Show - {ep}.mkv"),
        cleanup_dest_fileitem=old_dest_fileitem,
    )

    assert state is True
    assert errmsg == ""
    assert planned == [
        (
            main_fileitem.path,
            old_dest_fileitem.model_dump(mode="json"),
        )
    ]


def test_cleanup_dest_fileitem_is_kept_when_episode_format_matches_nothing(monkeypatch):
    """
    集数定位模板匹配不到文件时，不应清理历史记录中的旧目标文件。
    """
    chain = make_transfer_chain()
    delete_calls = []
    source_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.sc.ass"
    )
    old_dest_fileitem = make_fileitem(
        "/library/Test Show/Show - 01.sc.ass"
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [(source_fileitem, False)],
    )
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr(
        "app.chain.transfer.request.StorageChain",
        lambda: SimpleNamespace(
            delete_media_file=lambda fileitem: delete_calls.append(fileitem.path) or True,
        ),
    )
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: SimpleNamespace(
        delete_media_file=lambda fileitem: delete_calls.append(fileitem.path) or True,
    ))

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=source_fileitem,
        background=False,
        epformat=EpisodeFormat(format="Show - {ep}.mkv"),
        cleanup_dest_fileitem=old_dest_fileitem,
    )

    assert state is True
    assert errmsg == ""
    assert delete_calls == []


def test_episode_format_matched_but_filtered_by_size_returns_failure(monkeypatch):
    """
    文件名匹配集数定位模板但被大小过滤时，不应误报为模板无匹配的安全跳过。
    """
    chain = make_transfer_chain()
    source_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.mkv"
    )

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda fileitem, predicate: [(source_fileitem, False)],
    )
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=source_fileitem,
        background=False,
        epformat=EpisodeFormat(format="Show - {ep}.mkv"),
        min_filesize=2,
    )

    assert state is False
    assert errmsg == f"{source_fileitem.name} 没有找到可整理的媒体文件"


def test_candidate_collection_checks_continue_callback(monkeypatch):
    """
    候选文件收集阶段应响应取消，避免大目录或远程存储继续完整遍历。
    """
    chain = make_transfer_chain()
    source_fileitem = make_fileitem(
        "/downloads/Test Show (2026)/Show - 01.mkv"
    )
    callback_calls = []

    def fake_get_trans_fileitems(fileitem, predicate):
        """
        模拟递归收集候选文件时调用 predicate。
        """
        callback_calls.append("collect")
        predicate(source_fileitem, False)
        return [(source_fileitem, False)]

    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        fake_get_trans_fileitems,
    )
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))

    state, errmsg = TransferChain.do_transfer(
        chain,
        fileitem=source_fileitem,
        background=False,
        continue_callback=lambda: False,
    )

    assert state is False
    assert errmsg == f"{source_fileitem.name} 已取消"
    assert callback_calls == ["collect"]
