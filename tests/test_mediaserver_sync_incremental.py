import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if "psutil" not in sys.modules:
    sys.modules["psutil"] = types.ModuleType("psutil")

if "aiosqlite" not in sys.modules:
    aiosqlite_module = types.ModuleType("aiosqlite")
    for attr in (
        "DatabaseError",
        "Error",
        "IntegrityError",
        "InterfaceError",
        "InternalError",
        "NotSupportedError",
        "OperationalError",
        "ProgrammingError",
        "sqlite_version",
        "sqlite_version_info",
    ):
        setattr(aiosqlite_module, attr, getattr(sqlite3, attr))
    aiosqlite_module.connect = sqlite3.connect
    aiosqlite_module.paramstyle = "qmark"
    aiosqlite_module.threadsafety = sqlite3.threadsafety
    sys.modules["aiosqlite"] = aiosqlite_module

if "app.log" not in sys.modules:
    log_module = types.ModuleType("app.log")

    class _Logger:
        def info(self, *_args, **_kwargs):
            return None

        def debug(self, *_args, **_kwargs):
            return None

        def warning(self, *_args, **_kwargs):
            return None

        def error(self, *_args, **_kwargs):
            return None

    log_module.logger = _Logger()
    log_module.log_settings = SimpleNamespace()
    log_module.LogConfigModel = type("LogConfigModel", (), {})
    sys.modules["app.log"] = log_module

from app import schemas
from app.db import Base
from app.db.mediaserver_oper import MediaServerOper
from app.db.models.mediaserver import MediaServerItem


def _load_mediaserver_chain_class():
    """隔离加载 MediaServerChain，避免测试依赖完整运行时环境。"""
    module_name = "_test_mediaserver_chain"
    if module_name in sys.modules:
        module = sys.modules[module_name]
        return module, module.MediaServerChain

    if "app.chain" not in sys.modules:
        chain_module = types.ModuleType("app.chain")
        chain_module.ChainBase = type("ChainBase", (), {})
        sys.modules["app.chain"] = chain_module

    if "app.core.config" not in sys.modules:
        config_module = types.ModuleType("app.core.config")
        config_module.global_vars = SimpleNamespace(is_system_stopped=False)
        sys.modules["app.core.config"] = config_module

    if "app.helper.service" not in sys.modules:
        service_module = types.ModuleType("app.helper.service")

        class _ServiceConfigHelper:
            @staticmethod
            def get_mediaserver_configs():
                return []

        service_module.ServiceConfigHelper = _ServiceConfigHelper
        sys.modules["app.helper.service"] = service_module

    mediaserver_path = Path(__file__).resolve().parents[1] / "app" / "chain" / "mediaserver.py"
    spec = importlib.util.spec_from_file_location(module_name, mediaserver_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, module.MediaServerChain


MEDIA_SERVER_CHAIN_MODULE, MediaServerChain = _load_mediaserver_chain_class()


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


if __name__ == "__main__":
    unittest.main()
