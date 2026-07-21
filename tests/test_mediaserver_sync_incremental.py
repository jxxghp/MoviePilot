from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import schemas
from app.chain import mediaserver as MEDIA_SERVER_CHAIN_MODULE
from app.chain.mediaserver import MediaServerChain
from app.db import Base
from app.db.mediaserver_oper import MediaServerOper
from app.db.models.mediaserver import MediaServerItem


@pytest.fixture
def database(tmp_path):
    """创建隔离的媒体服务器测试数据库。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'mediaserver.db'}")
    session_factory = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield session_factory
    engine.dispose()


def test_add_allows_same_item_id_across_servers(database):
    """不同媒体服务器允许复用相同 item_id。"""
    with database() as db:
        oper = MediaServerOper(db)

        assert oper.add(
            server="plex",
            library="movies",
            item_id="same-item-id",
            item_type="电影",
            title="Movie A",
        )
        assert oper.add(
            server="jellyfin",
            library="movies",
            item_id="same-item-id",
            item_type="电影",
            title="Movie B",
        )

        items = (
            db.query(MediaServerItem)
            .order_by(MediaServerItem.server.asc())
            .all()
        )

    assert len(items) == 2
    assert [item.server for item in items] == ["jellyfin", "plex"]


def test_media_count_reuses_existing_server_statistics():
    """整服同步应复用现有媒体统计并排除剧集集数。"""
    chain = object.__new__(MediaServerChain)
    chain.run_module = lambda *_args, **_kwargs: [
        schemas.Statistic(movie_count=12, tv_count=8, episode_count=200)
    ]

    assert chain.media_count("plex") == 20


def test_sync_updates_rows_and_removes_stale_entries(database):
    """同步应更新已存在条目，并清理未再出现或已移除服务的数据。"""
    old_sync_time = "2026-05-01 00:00:00"

    with database() as db:
        db.add_all(
            [
                MediaServerItem(
                    server="plex",
                    library="movies",
                    item_id="/library/metadata/1",
                    item_type="电影",
                    title="Old Title",
                    year="2024",
                    path="/media/old.mkv",
                    lst_mod_date=old_sync_time,
                ),
                MediaServerItem(
                    server="plex",
                    library="movies",
                    item_id="/library/metadata/2",
                    item_type="电影",
                    title="Stale Title",
                    year="2020",
                    path="/media/stale.mkv",
                    lst_mod_date=old_sync_time,
                ),
                MediaServerItem(
                    server="jellyfin",
                    library="movies",
                    item_id="/library/metadata/1",
                    item_type="电影",
                    title="Removed Server Title",
                    year="2024",
                    path="/media/removed.mkv",
                    lst_mod_date=old_sync_time,
                ),
            ]
        )
        db.commit()
        existing_id = (
            db.query(MediaServerItem.id)
            .filter(
                MediaServerItem.server == "plex",
                MediaServerItem.item_id == "/library/metadata/1",
            )
            .scalar()
        )

    chain = object.__new__(MediaServerChain)
    chain.librarys = lambda _server: [
        SimpleNamespace(id="movies", name="电影库"),
        SimpleNamespace(id="shows", name="剧集库"),
    ]
    chain.media_count = lambda _server: pytest.fail("部分媒体库同步不应使用整服统计")
    chain.items_count = lambda **_kwargs: 1
    chain.items = lambda **_kwargs: iter(
        [
            schemas.MediaServerItem(
                server="plex",
                library="movies",
                item_id="/library/metadata/1",
                item_type="Movie",
                title="New Title",
                year="2024",
                tmdbid=100,
                path="/media/new.mkv",
            )
        ]
    )
    chain.episodes = lambda *_args, **_kwargs: []

    with patch("app.db.ScopedSession", database), patch.object(
        MEDIA_SERVER_CHAIN_MODULE.ServiceConfigHelper,
        "get_mediaserver_configs",
        return_value=[SimpleNamespace(name="plex", enabled=True, sync_libraries=["movies"])],
    ):
        chain.sync()

    with database() as db:
        items = (
            db.query(MediaServerItem)
            .order_by(MediaServerItem.server.asc(), MediaServerItem.item_id.asc())
            .all()
        )

    assert len(items) == 1
    assert items[0].id == existing_id
    assert items[0].server == "plex"
    assert items[0].item_id == "/library/metadata/1"
    assert items[0].item_type == "电影"
    assert items[0].title == "New Title"
    assert items[0].path == "/media/new.mkv"
    assert items[0].lst_mod_date != old_sync_time


def test_sync_queries_counts_before_items_and_reports_media_progress(database):
    """同步前应查询全部目标媒体库总数，并按媒体条目更新进度。"""
    chain = object.__new__(MediaServerChain)
    events = []
    progress_snapshots = []
    server_libraries = {
        "plex-a": [SimpleNamespace(id="movies", name="电影库")],
        "plex-b": [SimpleNamespace(id="shows", name="剧集库")],
    }
    library_items = {
        ("plex-a", "movies"): [
            schemas.MediaServerItem(
                server="plex-a",
                library="movies",
                item_id=f"movie-{index}",
                item_type="Movie",
                title=f"电影 {index}",
            )
            for index in range(2)
        ],
        ("plex-b", "shows"): [
            schemas.MediaServerItem(
                server="plex-b",
                library="shows",
                item_id="show-1",
                item_type="Movie",
                title="剧集 1",
            )
        ],
    }

    chain.librarys = lambda server: server_libraries[server]

    def media_count(server):
        """记录整服统计顺序并返回待同步媒体总数。"""
        events.append(f"count:{server}")
        return sum(
            len(items)
            for (item_server, _library_id), items in library_items.items()
            if item_server == server
        )

    def items(**kwargs):
        """记录同步顺序并返回媒体库条目。"""
        server = kwargs["server"]
        library_id = kwargs["library_id"]
        events.append(f"items:{server}:{library_id}")
        return iter(library_items[(server, library_id)])

    chain.media_count = media_count
    chain.items_count = lambda **_kwargs: pytest.fail("整服同步不应逐库重复计数")
    chain.items = items
    chain.episodes = lambda *_args, **_kwargs: []

    with patch("app.db.ScopedSession", database), patch.object(
        MEDIA_SERVER_CHAIN_MODULE.ServiceConfigHelper,
        "get_mediaserver_configs",
        return_value=[
            SimpleNamespace(name="plex-a", enabled=True, sync_libraries=["all"]),
            SimpleNamespace(name="plex-b", enabled=True, sync_libraries=["all"]),
        ],
    ):
        chain.sync(
            progress_callback=lambda **kwargs: progress_snapshots.append(kwargs)
        )

    assert events == [
        "count:plex-a",
        "count:plex-b",
        "items:plex-a:movies",
        "items:plex-b:shows",
    ]
    media_progress = [
        snapshot
        for snapshot in progress_snapshots
        if snapshot["text"].startswith(("正在同步 plex-a 媒体库", "正在同步 plex-b 媒体库"))
    ]
    assert [round(snapshot["value"], 2) for snapshot in media_progress] == [
        33.33,
        66.67,
        100.0,
    ]
    assert media_progress[0]["data"]["media_total"] == 3
    assert media_progress[1]["data"]["library_media_finished"] == 2
    assert media_progress[2]["data"]["current_library"] == "剧集库"
    assert media_progress[2]["data"]["media_total"] == 3
    assert media_progress[2]["data"]["media_finished"] == 3
    progress_values = [snapshot["value"] for snapshot in progress_snapshots]
    assert progress_values == sorted(progress_values)


def test_sync_targets_one_server_without_excluding_other_enabled_servers(monkeypatch):
    """定向同步只访问目标服务器，缓存清理仍保留其他已启用服务器。"""
    chain = object.__new__(MediaServerChain)
    library_calls = []
    excluded_server_calls = []

    class FakeMediaServerOper:
        """记录媒体服务器缓存清理参数的测试替身。"""

        def delete_excluded_servers(self, servers):
            """记录应保留的全部已启用服务器名称。"""
            excluded_server_calls.append(servers)

    chain.librarys = lambda server: library_calls.append(server) or []
    monkeypatch.setattr(MEDIA_SERVER_CHAIN_MODULE, "MediaServerOper", FakeMediaServerOper)
    monkeypatch.setattr(
        MEDIA_SERVER_CHAIN_MODULE.ServiceConfigHelper,
        "get_mediaserver_configs",
        lambda: [
            SimpleNamespace(name="plex-a", enabled=True, sync_libraries=["all"]),
            SimpleNamespace(name="plex-b", enabled=True, sync_libraries=["all"]),
        ],
    )

    chain.sync(server="plex-a")

    assert library_calls == ["plex-a"]
    assert excluded_server_calls == [["plex-a", "plex-b"]]
