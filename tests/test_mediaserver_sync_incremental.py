import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import schemas
from app.chain import mediaserver as MEDIA_SERVER_CHAIN_MODULE
from app.chain.mediaserver import MediaServerChain
from app.db import Base
from app.db.mediaserver_oper import MediaServerOper
from app.db.models.mediaserver import MediaServerItem


class MediaServerIncrementalSyncTest(unittest.TestCase):
    """验证媒体库同步改为按条目增量更新。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "mediaserver.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.SessionFactory = sessionmaker(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_add_allows_same_item_id_across_servers(self):
        """不同媒体服务器允许复用相同 item_id。"""
        with self.SessionFactory() as db:
            oper = MediaServerOper(db)

            self.assertTrue(
                oper.add(
                    server="plex",
                    library="movies",
                    item_id="same-item-id",
                    item_type="电影",
                    title="Movie A",
                )
            )
            self.assertTrue(
                oper.add(
                    server="jellyfin",
                    library="movies",
                    item_id="same-item-id",
                    item_type="电影",
                    title="Movie B",
                )
            )

            items = (
                db.query(MediaServerItem)
                .order_by(MediaServerItem.server.asc())
                .all()
            )

        self.assertEqual(len(items), 2)
        self.assertEqual([item.server for item in items], ["jellyfin", "plex"])

    def test_sync_updates_rows_and_removes_stale_entries(self):
        """同步应更新已存在条目，并清理未再出现或已移除服务的数据。"""
        old_sync_time = "2026-05-01 00:00:00"

        with self.SessionFactory() as db:
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
        chain.librarys = lambda _server: [SimpleNamespace(id="movies", name="电影库")]
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

        with patch("app.db.ScopedSession", self.SessionFactory), patch.object(
            MEDIA_SERVER_CHAIN_MODULE.ServiceConfigHelper,
            "get_mediaserver_configs",
            return_value=[SimpleNamespace(name="plex", enabled=True, sync_libraries=["all"])],
        ):
            chain.sync()

        with self.SessionFactory() as db:
            items = (
                db.query(MediaServerItem)
                .order_by(MediaServerItem.server.asc(), MediaServerItem.item_id.asc())
                .all()
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, existing_id)
        self.assertEqual(items[0].server, "plex")
        self.assertEqual(items[0].item_id, "/library/metadata/1")
        self.assertEqual(items[0].item_type, "电影")
        self.assertEqual(items[0].title, "New Title")
        self.assertEqual(items[0].path, "/media/new.mkv")
        self.assertNotEqual(items[0].lst_mod_date, old_sync_time)
