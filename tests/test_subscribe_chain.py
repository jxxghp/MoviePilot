import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.schemas.types import MediaType


def _load_subscribe_chain_class():
    """隔离加载 SubscribeChain，避免测试依赖完整运行时环境。"""
    module_name = "_test_subscribe_chain"
    if module_name in sys.modules:
        module = sys.modules[module_name]
        return module, module.SubscribeChain

    injected_modules = {}

    def ensure_module(name: str, module: types.ModuleType):
        if name in sys.modules:
            return sys.modules[name]
        sys.modules[name] = module
        injected_modules[name] = module
        return module

    chain_module = ensure_module("app.chain", types.ModuleType("app.chain"))

    class _ChainBase:
        def __init__(self):
            self.messagehelper = SimpleNamespace(put=lambda *args, **kwargs: None)

        def post_message(self, *args, **kwargs):
            return None

        async def async_post_message(self, *args, **kwargs):
            return None

    chain_module.ChainBase = _ChainBase

    interaction_module = ensure_module("app.helper.interaction", types.ModuleType("app.helper.interaction"))

    class _SlashInteractionManager:
        def create_or_replace(self, *args, **kwargs):
            return SimpleNamespace(request_id="request-id")

        def get_by_id(self, *args, **kwargs):
            return None

        def get_by_user(self, *args, **kwargs):
            return None

        def remove(self, *args, **kwargs):
            return None

    interaction_module.SlashInteractionManager = _SlashInteractionManager
    interaction_module.build_navigation_buttons = lambda *args, **kwargs: []
    interaction_module.format_markdown_table = lambda *args, **kwargs: ""
    interaction_module.page_items = lambda *args, **kwargs: []
    interaction_module.supports_interaction_buttons = lambda *args, **kwargs: False
    interaction_module.supports_markdown = lambda *args, **kwargs: False
    interaction_module.update_or_post_message = lambda *args, **kwargs: None

    config_module = ensure_module("app.core.config", types.ModuleType("app.core.config"))
    config_module.global_vars = SimpleNamespace(is_system_stopped=False)
    config_module.settings = SimpleNamespace(
        RECOGNIZE_SOURCE="themoviedb",
        MP_DOMAIN=lambda path: path,
    )

    context_module = ensure_module("app.core.context", types.ModuleType("app.core.context"))
    context_module.TorrentInfo = SimpleNamespace
    context_module.Context = SimpleNamespace
    context_module.MediaInfo = SimpleNamespace

    event_module = ensure_module("app.core.event", types.ModuleType("app.core.event"))

    class _EventManager:
        @staticmethod
        def send_event(*args, **kwargs):
            return None

        @staticmethod
        async def async_send_event(*args, **kwargs):
            return None

        @staticmethod
        def register(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

    event_module.eventmanager = _EventManager()
    event_module.Event = SimpleNamespace

    meta_module = ensure_module("app.core.meta", types.ModuleType("app.core.meta"))
    meta_module.MetaBase = SimpleNamespace

    metainfo_module = ensure_module("app.core.metainfo", types.ModuleType("app.core.metainfo"))
    metainfo_module.MetaInfo = lambda *args, **kwargs: SimpleNamespace(episode_list=[])

    words_module = ensure_module("app.core.meta.words", types.ModuleType("app.core.meta.words"))

    class _WordsMatcher:
        def prepare(self, title, custom_words=None):
            return title, []

    words_module.WordsMatcher = _WordsMatcher

    schemas_module = ensure_module("app.schemas", types.ModuleType("app.schemas"))

    class _Notification:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _SubscribeSchema:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _NotExistMediaInfo:
        def __init__(self, season=None, episodes=None, total_episode=None, start_episode=None):
            self.season = season
            self.episodes = episodes or []
            self.total_episode = total_episode
            self.start_episode = start_episode

    class _SubscribeEpisodeInfo:
        def __init__(self):
            self.downloading = []
            self.downloaded = []
            self.library = []

    class _SubscrbieInfo:
        def __init__(self):
            self.subscribe = None
            self.episodes = {}

    class _SubscribeDownloadFileInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _SubscribeLibraryFileInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _MediaRecognizeConvertEventData:
        def __init__(self, **kwargs):
            self.mediaid = kwargs.get("mediaid")
            self.convert_type = kwargs.get("convert_type")
            self.media_dict = kwargs.get("media_dict")

    schemas_module.Notification = _Notification
    schemas_module.Subscribe = _SubscribeSchema
    schemas_module.NotExistMediaInfo = _NotExistMediaInfo
    schemas_module.SubscribeEpisodeInfo = _SubscribeEpisodeInfo
    schemas_module.SubscrbieInfo = _SubscrbieInfo
    schemas_module.SubscribeDownloadFileInfo = _SubscribeDownloadFileInfo
    schemas_module.SubscribeLibraryFileInfo = _SubscribeLibraryFileInfo
    schemas_module.MediaRecognizeConvertEventData = _MediaRecognizeConvertEventData

    logger_module = ensure_module("app.log", types.ModuleType("app.log"))

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def warn(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

    logger_module.logger = _Logger()

    helper_subscribe_module = ensure_module("app.helper.subscribe", types.ModuleType("app.helper.subscribe"))

    class _SubscribeHelper:
        def sub_done_async(self, *args, **kwargs):
            return None

        @staticmethod
        def get_shares():
            return []

    helper_subscribe_module.SubscribeHelper = _SubscribeHelper

    helper_torrent_module = ensure_module("app.helper.torrent", types.ModuleType("app.helper.torrent"))
    helper_torrent_module.TorrentHelper = type("TorrentHelper", (), {})

    db_model_module = ensure_module("app.db.models.subscribe", types.ModuleType("app.db.models.subscribe"))

    class _SubscribeModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def to_dict(self):
            return dict(self.__dict__)

    db_model_module.Subscribe = _SubscribeModel

    subscribe_oper_module = ensure_module("app.db.subscribe_oper", types.ModuleType("app.db.subscribe_oper"))

    class _SubscribeOper:
        def update(self, *args, **kwargs):
            return None

        def get(self, *args, **kwargs):
            return None

        def list(self, *args, **kwargs):
            return []

        def delete(self, *args, **kwargs):
            return None

        def add_history(self, *args, **kwargs):
            return None

    subscribe_oper_module.SubscribeOper = _SubscribeOper

    simple_oper_modules = {
        "app.db.downloadhistory_oper": "DownloadHistoryOper",
        "app.db.site_oper": "SiteOper",
        "app.db.systemconfig_oper": "SystemConfigOper",
    }
    for module_name_key, class_name in simple_oper_modules.items():
        module = ensure_module(module_name_key, types.ModuleType(module_name_key))
        if class_name == "SystemConfigOper":
            class _SystemConfigOper:
                def get(self, *args, **kwargs):
                    return None

                def set(self, *args, **kwargs):
                    return None

            setattr(module, class_name, _SystemConfigOper)
        else:
            setattr(module, class_name, type(class_name, (), {}))

    chain_dependencies = {
        "app.chain.download": "DownloadChain",
        "app.chain.media": "MediaChain",
        "app.chain.search": "SearchChain",
        "app.chain.tmdb": "TmdbChain",
        "app.chain.torrents": "TorrentsChain",
    }
    for module_name_key, class_name in chain_dependencies.items():
        module = ensure_module(module_name_key, types.ModuleType(module_name_key))
        setattr(module, class_name, type(class_name, (), {}))

    subscribe_path = Path(__file__).resolve().parents[1] / "app" / "chain" / "subscribe.py"
    spec = importlib.util.spec_from_file_location(module_name, subscribe_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module._injected_modules = injected_modules
    return module, module.SubscribeChain


SUBSCRIBE_CHAIN_MODULE, SubscribeChain = _load_subscribe_chain_class()


class SubscribeChainTest(TestCase):
    def _build_subscribe(self, **overrides):
        data = {
            "id": 1,
            "name": "Test Show",
            "season": 1,
            "best_version": 1,
            "best_version_full": 0,
            "type": MediaType.TV.value,
            "start_episode": 1,
            "total_episode": 3,
            "current_priority": None,
            "episode_priority": None,
            "lack_episode": 3,
            "state": "R",
            "note": [],
            "manual_total_episode": 0,
            "tmdbid": 1,
            "doubanid": None,
            "year": "2026",
            "imdbid": None,
            "tvdbid": None,
            "episode_group": None,
            "poster": None,
            "backdrop": None,
            "description": None,
            "last_update": None,
            "username": None,
            "to_dict": lambda: {},
        }
        data.update(overrides)
        return SimpleNamespace(**data)

    @staticmethod
    def _build_download(priority, selected_episodes=None, meta_episodes=None):
        return SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=priority),
            selected_episodes=selected_episodes,
            meta_info=SimpleNamespace(season_list=[1], episode_list=meta_episodes or []),
        )

    def test_get_episode_priority_falls_back_to_current_priority(self):
        subscribe = self._build_subscribe(current_priority=80, episode_priority=None)

        self.assertEqual(
            SubscribeChain.get_episode_priority(subscribe),
            {"1": 80, "2": 80, "3": 80},
        )

    def test_get_pending_best_version_episodes_uses_per_episode_status(self):
        subscribe = self._build_subscribe(
            total_episode=5,
            episode_priority={"1": 100, "2": 80, "4": 100},
        )

        self.assertEqual(
            SubscribeChain._get_pending_best_version_episodes(subscribe),
            [2, 3, 5],
        )

    def test_best_version_progress_helpers_return_remaining_priority(self):
        subscribe = self._build_subscribe(
            total_episode=5,
            episode_priority={"1": 100, "2": 80, "3": 90, "4": 100, "5": 70},
            current_priority=100,
        )

        self.assertEqual(SubscribeChain.get_best_version_lack_episode(subscribe), 3)
        self.assertEqual(SubscribeChain.get_best_version_current_priority(subscribe), 90)
        self.assertFalse(SubscribeChain.is_best_version_complete(subscribe))

    def test_best_version_progress_helpers_mark_complete_when_all_target_episodes_done(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 100, "3": 100},
            current_priority=90,
        )

        self.assertEqual(SubscribeChain.get_best_version_lack_episode(subscribe), 0)
        self.assertEqual(SubscribeChain.get_best_version_current_priority(subscribe), 100)
        self.assertTrue(SubscribeChain.is_best_version_complete(subscribe))

    def test_is_episode_range_covered_matches_pending_episodes(self):
        subscribe = self._build_subscribe(
            total_episode=12,
            episode_priority={
                **{str(ep): 100 for ep in range(1, 5)},
                **{str(ep): 100 for ep in range(8, 13)},
            },
        )

        self.assertTrue(
            SubscribeChain._is_episode_range_covered(
                meta=SimpleNamespace(episode_list=[5, 6, 7]),
                subscribe=subscribe,
            )
        )
        self.assertFalse(
            SubscribeChain._is_episode_range_covered(
                meta=SimpleNamespace(episode_list=[1, 2, 3, 4]),
                subscribe=subscribe,
            )
        )
        self.assertTrue(
            SubscribeChain._is_episode_range_covered(
                meta=SimpleNamespace(episode_list=[]),
                subscribe=subscribe,
            )
        )

    def test_full_best_version_rejects_episode_resource(self):
        subscribe = self._build_subscribe(best_version_full=1, total_episode=3)

        self.assertFalse(
            SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
                meta=SimpleNamespace(season_list=[1], episode_list=[1]),
                subscribe=subscribe,
            )
        )

    def test_full_best_version_accepts_full_pack_resource(self):
        subscribe = self._build_subscribe(best_version_full=1, total_episode=3)

        self.assertTrue(
            SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
                meta=SimpleNamespace(season_list=[1], episode_list=[]),
                subscribe=subscribe,
            )
        )
        self.assertTrue(
            SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
                meta=SimpleNamespace(season_list=[1], episode_list=[1, 2, 3]),
                subscribe=subscribe,
            )
        )

    def test_episode_best_version_downloads_full_pack_before_episode_fallback(self):
        subscribe = self._build_subscribe(best_version_full=0, total_episode=3)
        full_pack_context = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=90),
            media_info=SimpleNamespace(type=MediaType.TV),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[]),
        )
        episode_context = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=90),
            media_info=SimpleNamespace(type=MediaType.TV),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[2]),
        )
        no_exists = {
            "media-key": {
                1: SimpleNamespace(season=1, episodes=[2], total_episode=3, start_episode=1)
            }
        }
        calls = []

        class _FakeDownloadChain:
            """记录批量下载调用，用于验证分集洗版会先尝试全集资源。"""

            def batch_download(self, **kwargs):
                calls.append(kwargs)
                return [full_pack_context], {}

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _FakeDownloadChain):
            downloads, lefts = SubscribeChain()._SubscribeChain__download_best_version_with_full_pack_first(
                contexts=[episode_context, full_pack_context],
                no_exists=no_exists,
                subscribe=subscribe,
                mediakey="media-key",
                username="user",
                save_path="/downloads",
                downloader="qb",
                source="subscribe",
            )

        self.assertEqual(downloads, [full_pack_context])
        self.assertEqual(lefts, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["contexts"], [full_pack_context])
        self.assertEqual(calls[0]["no_exists"]["media-key"][1].episodes, [])

    def test_episode_best_version_falls_back_when_full_pack_not_downloaded(self):
        subscribe = self._build_subscribe(best_version_full=0, total_episode=3)
        full_pack_context = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=90),
            media_info=SimpleNamespace(type=MediaType.TV),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[]),
        )
        episode_context = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=90),
            media_info=SimpleNamespace(type=MediaType.TV),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[2]),
        )
        no_exists = {
            "media-key": {
                1: SimpleNamespace(season=1, episodes=[2], total_episode=3, start_episode=1)
            }
        }
        calls = []

        class _FakeDownloadChain:
            """模拟全集下载失败，验证后续会回退到按集下载。"""

            def batch_download(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return [], kwargs["no_exists"]
                return [episode_context], {}

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _FakeDownloadChain):
            downloads, lefts = SubscribeChain()._SubscribeChain__download_best_version_with_full_pack_first(
                contexts=[episode_context, full_pack_context],
                no_exists=no_exists,
                subscribe=subscribe,
                mediakey="media-key",
            )

        self.assertEqual(downloads, [episode_context])
        self.assertEqual(lefts, {})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["contexts"], [full_pack_context])
        self.assertIs(calls[1]["no_exists"], no_exists)

    def test_episode_best_version_skips_full_pack_first_when_pack_priority_equals_existing_episode(self):
        """验证全集优先级等于目标分集时回退到分集下载。"""
        subscribe = self._build_subscribe(
            best_version_full=0,
            total_episode=3,
            episode_priority={"1": 80, "2": 80, "3": 80},
            current_priority=80,
        )
        full_pack_context = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=80),
            media_info=SimpleNamespace(type=MediaType.TV),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[]),
        )
        episode_context = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=90),
            media_info=SimpleNamespace(type=MediaType.TV),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[2]),
        )
        no_exists = {
            "media-key": {
                1: SimpleNamespace(season=1, episodes=[2], total_episode=3, start_episode=1)
            }
        }
        calls = []

        class _FakeDownloadChain:
            """记录回退下载调用，确保全集候选仍可参与拆包匹配。"""

            def batch_download(self, **kwargs):
                calls.append(kwargs)
                return [episode_context], {}

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _FakeDownloadChain):
            downloads, lefts = SubscribeChain()._SubscribeChain__download_best_version_with_full_pack_first(
                contexts=[episode_context, full_pack_context],
                no_exists=no_exists,
                subscribe=subscribe,
                mediakey="media-key",
            )

        self.assertEqual(downloads, [episode_context])
        self.assertEqual(lefts, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["contexts"], [episode_context, full_pack_context])
        self.assertIs(calls[0]["no_exists"], no_exists)

    def test_episode_best_version_skips_full_pack_first_when_pack_priority_below_one_episode(self):
        """验证全集低于任一目标分集优先级时不会整包优先。"""
        subscribe = self._build_subscribe(
            best_version_full=0,
            total_episode=3,
            episode_priority={"1": 90, "2": 80, "3": 80},
            current_priority=80,
        )
        full_pack_context = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=85),
            media_info=SimpleNamespace(type=MediaType.TV),
            meta_info=SimpleNamespace(season_list=[1], episode_list=[]),
        )
        no_exists = {
            "media-key": {
                1: SimpleNamespace(season=1, episodes=[2], total_episode=3, start_episode=1)
            }
        }
        calls = []

        class _FakeDownloadChain:
            """记录回退下载调用，验证低优先级全集不进入整包优先分支。"""

            def batch_download(self, **kwargs):
                calls.append(kwargs)
                return [], kwargs["no_exists"]

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _FakeDownloadChain):
            downloads, lefts = SubscribeChain()._SubscribeChain__download_best_version_with_full_pack_first(
                contexts=[full_pack_context],
                no_exists=no_exists,
                subscribe=subscribe,
                mediakey="media-key",
            )

        self.assertEqual(downloads, [])
        self.assertIs(lefts, no_exists)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["contexts"], [full_pack_context])
        self.assertIs(calls[0]["no_exists"], no_exists)

    def test_full_pack_priority_check_uses_current_priority_fallback(self):
        """验证旧订阅没有分集状态时使用 current_priority 兜底判断。"""
        subscribe = self._build_subscribe(total_episode=3, current_priority=80, episode_priority=None)

        self.assertFalse(
            SubscribeChain._SubscribeChain__is_full_season_priority_higher_than_all_targets(
                subscribe=subscribe,
                priority=80,
            )
        )
        self.assertTrue(
            SubscribeChain._SubscribeChain__is_full_season_priority_higher_than_all_targets(
                subscribe=subscribe,
                priority=81,
            )
        )

    def test_update_subscribe_priority_uses_selected_episodes(self):
        subscribe = self._build_subscribe(
            total_episode=4,
            episode_priority={"1": 100, "2": 80, "3": 70, "4": 60},
            current_priority=80,
            lack_episode=3,
        )
        download = self._build_download(
            priority=90,
            selected_episodes=[3],
            meta_episodes=[2, 3, 4],
        )
        chain = SubscribeChain()
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper") as subscribe_oper_cls, patch.object(
            SubscribeChain,
            "_SubscribeChain__finish_subscribe",
        ) as finish_mock:
            subscribe_oper = subscribe_oper_cls.return_value
            subscribe_oper.update.return_value = None

            chain.update_subscribe_priority(
                subscribe=subscribe,
                meta=SimpleNamespace(),
                mediainfo=mediainfo,
                downloads=[download],
            )

        subscribe_oper.update.assert_called_once()
        payload = subscribe_oper.update.call_args.args[1]
        self.assertEqual(payload["episode_priority"], {"1": 100, "2": 80, "3": 90, "4": 60})
        self.assertEqual(payload["current_priority"], 90)
        self.assertEqual(payload["lack_episode"], 3)
        self.assertEqual(subscribe.episode_priority, {"1": 100, "2": 80, "3": 90, "4": 60})
        self.assertEqual(subscribe.current_priority, 90)
        self.assertEqual(subscribe.lack_episode, 3)
        finish_mock.assert_not_called()

    def test_update_subscribe_priority_marks_complete_when_all_target_episodes_done(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 90, "3": 80},
            current_priority=90,
            lack_episode=2,
        )
        downloads = [
            self._build_download(priority=100, selected_episodes=[2]),
            self._build_download(priority=100, selected_episodes=[3]),
        ]
        chain = SubscribeChain()
        meta = SimpleNamespace()
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper") as subscribe_oper_cls, patch.object(
            SubscribeChain,
            "_SubscribeChain__finish_subscribe",
        ) as finish_mock:
            subscribe_oper = subscribe_oper_cls.return_value
            subscribe_oper.update.return_value = None

            chain.update_subscribe_priority(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                downloads=downloads,
            )

        payload = subscribe_oper.update.call_args.args[1]
        self.assertEqual(payload["episode_priority"], {"1": 100, "2": 100, "3": 100})
        self.assertEqual(payload["current_priority"], 100)
        self.assertEqual(payload["lack_episode"], 0)
        finish_mock.assert_called_once_with(subscribe=subscribe, meta=meta, mediainfo=mediainfo)

    def test_full_best_version_updates_all_episodes_when_pack_has_no_episode_metadata(self):
        subscribe = self._build_subscribe(
            best_version_full=1,
            total_episode=3,
            episode_priority={"1": 80, "2": 80, "3": 80},
            current_priority=80,
            lack_episode=3,
        )
        download = self._build_download(priority=100, selected_episodes=[], meta_episodes=[])
        chain = SubscribeChain()
        meta = SimpleNamespace()
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper") as subscribe_oper_cls, patch.object(
            SubscribeChain,
            "_SubscribeChain__finish_subscribe",
        ) as finish_mock:
            subscribe_oper = subscribe_oper_cls.return_value
            subscribe_oper.update.return_value = None

            chain.update_subscribe_priority(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                downloads=[download],
            )

        payload = subscribe_oper.update.call_args.args[1]
        self.assertEqual(payload["episode_priority"], {"1": 100, "2": 100, "3": 100})
        self.assertEqual(payload["current_priority"], 100)
        self.assertEqual(payload["lack_episode"], 0)
        finish_mock.assert_called_once_with(subscribe=subscribe, meta=meta, mediainfo=mediainfo)

    def test_episode_best_version_updates_all_episodes_when_full_pack_has_no_episode_metadata(self):
        subscribe = self._build_subscribe(
            best_version_full=0,
            total_episode=3,
            episode_priority={"1": 80, "2": 80, "3": 80},
            current_priority=80,
            lack_episode=3,
        )
        download = self._build_download(priority=100, selected_episodes=[], meta_episodes=[])
        chain = SubscribeChain()
        meta = SimpleNamespace()
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper") as subscribe_oper_cls, patch.object(
            SubscribeChain,
            "_SubscribeChain__finish_subscribe",
        ) as finish_mock:
            subscribe_oper = subscribe_oper_cls.return_value
            subscribe_oper.update.return_value = None

            chain.update_subscribe_priority(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                downloads=[download],
            )

        payload = subscribe_oper.update.call_args.args[1]
        self.assertEqual(payload["episode_priority"], {"1": 100, "2": 100, "3": 100})
        self.assertEqual(payload["current_priority"], 100)
        self.assertEqual(payload["lack_episode"], 0)
        finish_mock.assert_called_once_with(subscribe=subscribe, meta=meta, mediainfo=mediainfo)

    def test_check_resets_current_priority_when_new_episodes_expand_target_range(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 100, "3": 100},
            current_priority=100,
            lack_episode=0,
        )
        chain = SubscribeChain()
        chain.recognize_media = lambda **kwargs: SimpleNamespace(
            seasons={1: [1, 2, 3, 4, 5]},
            title="Test Show",
            year="2026",
            vote_average=9.5,
            overview="overview",
            imdb_id="tt1234567",
            tvdb_id=99,
            get_poster_image=lambda: "poster",
            get_backdrop_image=lambda: "backdrop",
        )

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper") as subscribe_oper_cls:
            subscribe_oper = subscribe_oper_cls.return_value
            subscribe_oper.list.return_value = [subscribe]
            subscribe_oper.update.return_value = None

            chain.check()

        payload = subscribe_oper.update.call_args.args[1]
        self.assertEqual(payload["total_episode"], 5)
        self.assertEqual(payload["lack_episode"], 2)
        self.assertEqual(payload["current_priority"], 0)
        self.assertEqual(payload["episode_priority"], {"1": 100, "2": 100, "3": 100, "4": 0, "5": 0})
        self.assertEqual(subscribe.total_episode, 5)
        self.assertEqual(subscribe.lack_episode, 2)
        self.assertEqual(subscribe.current_priority, 0)
