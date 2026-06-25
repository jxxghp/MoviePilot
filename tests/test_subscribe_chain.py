import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.schemas.types import MediaType
from app.testing import stub_modules


def _load_subscribe_chain_class():
    """隔离加载 SubscribeChain，避免测试依赖完整运行时环境。"""
    module_name = "_test_subscribe_chain"
    if module_name in sys.modules:
        module = sys.modules[module_name]
        return module, module.SubscribeChain

    stub_deps = {}

    def ensure_module(name: str, module: types.ModuleType):
        """登记一个加载期临时替换模块；实际替换与精确还原由 stub_modules 在加载时统一处理。"""
        stub_deps[name] = module
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

        @staticmethod
        def add_event_listener(*args, **kwargs):
            """兼容模块导入时注册配置变更监听。"""
            return None

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
        _fields = {
            "name",
            "type",
            "year",
            "tmdbid",
            "doubanid",
            "bangumiid",
            "season",
            "best_version",
            "save_path",
            "search_imdbid",
            "custom_words",
            "media_category",
            "filter_groups",
        }

        def __init__(self, **kwargs):
            for field in self._fields:
                setattr(self, field, None)
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _NotExistMediaInfo:
        def __init__(self, season=None, episodes=None, total_episode=None, start_episode=None,
                     require_complete_coverage=False):
            self.season = season
            self.episodes = episodes or []
            self.total_episode = total_episode
            self.start_episode = start_episode
            self.require_complete_coverage = require_complete_coverage

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

    class _SubscribeEpisodesRefreshEventData:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _SubscribeCompletionCheckEventData:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    schemas_module.Notification = _Notification
    schemas_module.Subscribe = _SubscribeSchema
    schemas_module.NotExistMediaInfo = _NotExistMediaInfo
    schemas_module.SubscribeEpisodeInfo = _SubscribeEpisodeInfo
    schemas_module.SubscrbieInfo = _SubscrbieInfo
    schemas_module.SubscribeDownloadFileInfo = _SubscribeDownloadFileInfo
    schemas_module.SubscribeLibraryFileInfo = _SubscribeLibraryFileInfo
    schemas_module.MediaRecognizeConvertEventData = _MediaRecognizeConvertEventData
    schemas_module.SubscribeEpisodesRefreshEventData = _SubscribeEpisodesRefreshEventData
    schemas_module.SubscribeCompletionCheckEventData = _SubscribeCompletionCheckEventData

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

    helper_server_module = ensure_module("app.helper.server", types.ModuleType("app.helper.server"))

    class _MoviePilotServerHelper:
        @staticmethod
        def sub_done_async(*args, **kwargs):
            """
            忽略订阅完成统计上报。
            """
            return None

        @staticmethod
        def sub_reg_async(*args, **kwargs):
            """
            忽略订阅新增统计上报。
            """
            return None

        @staticmethod
        async def async_sub_reg(*args, **kwargs):
            """
            忽略异步订阅新增统计上报。
            """
            return None

        @staticmethod
        def get_subscribe_shares():
            """
            返回空的订阅共享数据。
            """
            return []

    helper_server_module.MoviePilotServerHelper = _MoviePilotServerHelper

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
    assert spec and spec.loader
    # 加载期用 stub_modules 精确替换依赖、退出时统一还原；module_name 非桩，缓存入 sys.modules 供复用
    with stub_modules(stub_deps):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module._injected_modules = {name: sys.modules.get(name) for name in stub_deps}
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
            "bangumiid": None,
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
            meta_info=SimpleNamespace(season_list=[1], episode_list=meta_episodes or selected_episodes or []),
            media_info=SimpleNamespace(type=MediaType.TV, tmdb_id=1, douban_id=None),
        )

    def test_default_kwargs_respects_explicit_zero_best_version(self):
        """显式关闭洗版时必须保留 0，仅未传值才应用默认订阅规则。"""

        def _default_config(_mtype, key):
            return 1 if key in {"best_version", "best_version_full"} else None

        with patch.object(SubscribeChain, "_SubscribeChain__get_default_subscribe_config", side_effect=_default_config):
            explicit = SubscribeChain()._SubscribeChain__get_default_kwargs(
                MediaType.TV,
                best_version=0,
                best_version_full=0,
            )
            omitted = SubscribeChain()._SubscribeChain__get_default_kwargs(MediaType.TV)

        self.assertEqual(explicit["best_version"], 0)
        self.assertEqual(explicit["best_version_full"], 0)
        self.assertEqual(omitted["best_version"], 1)
        self.assertEqual(omitted["best_version_full"], 1)

    def test_format_subscribe_progress_preserves_special_season_zero(self):
        """订阅列表展示必须把 S0 当作合法季号，而不是回退到第 1 季。"""
        subscribe = self._build_subscribe(season=0, total_episode=5, lack_episode=2)

        progress = SubscribeChain._format_subscribe_progress(subscribe)

        self.assertEqual(progress, "第0季 [3/5]")

    def test_format_subscribe_progress_preserves_special_season_zero_without_total(self):
        """S0 没有总集数时仍显示特别季季号。"""
        subscribe = self._build_subscribe(season=0, total_episode=None, lack_episode=None)

        progress = SubscribeChain._format_subscribe_progress(subscribe)

        self.assertEqual(progress, "第0季")

    def test_match_title_fallback_calls_torrent_match_from_class(self):
        """确保标题兜底匹配不依赖 TorrentHelper 实例绑定。"""

        class _ReachedTitleMatch(Exception):
            """标记测试已经进入标题匹配函数体。"""

        class _PlainTorrentHelper:
            """模拟未声明 staticmethod 的历史 TorrentHelper 形态。"""

            def match_torrent(mediainfo, torrent_meta, torrent):
                """标记类级调用已经正确进入匹配逻辑。"""
                raise _ReachedTitleMatch

            def filter_torrent(self, *args, **kwargs):
                """保持订阅匹配后续过滤流程可继续执行。"""
                return True

        subscribe = self._build_subscribe(
            best_version=0,
            custom_words=None,
            doubanid=None,
            episode_group=None,
            sites=[],
            tmdbid=1,
        )
        mediainfo = SimpleNamespace(
            clear=lambda: None,
            douban_id=None,
            title_year="Test Show (2026)",
            tmdb_id=1,
            type=MediaType.TV,
        )
        context = SimpleNamespace(
            media_info=None,
            media_recognize_fail_count=3,
            meta_info=SimpleNamespace(
                begin_season=1,
                episode_list=[],
                org_string="Test Show",
                season_list=[1],
            ),
            torrent_info=SimpleNamespace(
                description="",
                site=1,
                site_name="TestSite",
                title="Test Show S01",
            ),
        )

        class _SubscribeOper:
            """提供单条订阅，避免依赖真实数据库。"""

            def list(self, *args, **kwargs):
                """返回当前测试构造的订阅列表。"""
                return [subscribe]

        chain = SubscribeChain()
        chain.recognize_media = lambda **kwargs: mediainfo
        chain.check_and_handle_existing_media = lambda **kwargs: (False, {})

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), patch.object(
            SUBSCRIBE_CHAIN_MODULE,
            "TorrentHelper",
            _PlainTorrentHelper,
        ), self.assertRaises(_ReachedTitleMatch):
            chain.match({"test.example": [context]})

    def test_match_accepts_special_season_zero_candidate(self):
        """S0 订阅应允许 S00 候选资源进入下载候选，不能按未指定季处理。"""

        class _TorrentHelper:
            def filter_torrent(self, *args, **kwargs):
                return True

        subscribe = self._build_subscribe(
            best_version=0,
            custom_words=None,
            doubanid=None,
            episode_group=None,
            filter_groups=[],
            keyword=None,
            media_category=None,
            save_path=None,
            search_imdbid=False,
            season=0,
            sites=[],
            tmdbid=1,
            username="",
            downloader=None,
        )
        mediainfo = SimpleNamespace(
            clear=lambda: None,
            douban_id=None,
            title_year="Test Show (2026)",
            tmdb_id=1,
            type=MediaType.TV,
        )
        torrent_media = SimpleNamespace(
            clear=lambda: None,
            douban_id=None,
            tmdb_id=1,
            type=MediaType.TV,
        )
        context = SimpleNamespace(
            media_info=torrent_media,
            media_recognize_fail_count=0,
            meta_info=SimpleNamespace(
                begin_season=0,
                episode_list=[1],
                org_string="Test Show S00E01",
                season_list=[0],
            ),
            torrent_info=SimpleNamespace(
                description="",
                pri_order=100,
                site=1,
                site_name="TestSite",
                title="Test Show S00E01",
            ),
        )
        download_calls = []

        class _SubscribeOper:
            """提供单条订阅，避免依赖真实数据库。"""

            def list(self, *args, **kwargs):
                """返回当前测试构造的订阅列表。"""
                return [subscribe]

            def get(self, *args, **kwargs):
                """下载后仍返回当前订阅。"""
                return subscribe

        def _download(self, **kwargs):
            download_calls.append(kwargs)
            return [context], {}

        chain = SubscribeChain()
        chain.recognize_media = lambda **kwargs: mediainfo
        chain.check_and_handle_existing_media = lambda **kwargs: (False, {})
        chain.get_sub_sites = lambda *_args, **_kwargs: []
        chain.get_params = lambda *_args, **_kwargs: {}
        chain.filter_torrents = lambda **_kwargs: [context.torrent_info]
        chain.finish_subscribe_or_not = lambda **_kwargs: None

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), patch.object(
            SUBSCRIBE_CHAIN_MODULE,
            "TorrentHelper",
            _TorrentHelper,
        ), patch.object(
            SubscribeChain,
            "_SubscribeChain__download_best_version_with_full_pack_first",
            _download,
        ):
            chain.match({"test.example": [context]})

        self.assertEqual(len(download_calls), 1)
        self.assertEqual(download_calls[0]["contexts"][0].meta_info.begin_season, 0)

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

        self.assertEqual(SubscribeChain.get_best_version_current_priority(subscribe), 90)
        self.assertFalse(SubscribeChain.is_best_version_complete(subscribe))

    def test_best_version_progress_helpers_mark_complete_when_all_target_episodes_done(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 100, "3": 100},
            current_priority=90,
        )

        self.assertEqual(SubscribeChain.get_best_version_current_priority(subscribe), 100)
        self.assertTrue(SubscribeChain.is_best_version_complete(subscribe))

    def test_get_subscribe_no_exists_expands_whole_missing_when_custom_start_skips_existing_range(self):
        """自定义开始集跳过季初集数时，缺失整季需要转成显式目标集。"""
        no_exists = {
            "media-key": {
                1: SimpleNamespace(
                    season=1,
                    episodes=[],
                    total_episode=48,
                    start_episode=1,
                    require_complete_coverage=False,
                )
            }
        }

        exist_flag, result = SubscribeChain._SubscribeChain__get_subscribe_no_exits(
            subscribe_name="主角 S01",
            no_exists=no_exists,
            mediakey="media-key",
            begin_season=1,
            total_episode=48,
            start_episode=44,
        )

        self.assertFalse(exist_flag)
        self.assertEqual(result["media-key"][1].episodes, [44, 45, 46, 47, 48])
        self.assertEqual(result["media-key"][1].start_episode, 44)
        self.assertEqual(result["media-key"][1].total_episode, 48)

    def test_get_subscribe_no_exists_keeps_whole_missing_when_custom_start_matches_original_start(self):
        """自定义开始集没有缩小范围时，仍保留空集列表表示整季缺失。"""
        no_exists = {
            "media-key": {
                1: SimpleNamespace(
                    season=1,
                    episodes=[],
                    total_episode=48,
                    start_episode=1,
                    require_complete_coverage=False,
                )
            }
        }

        exist_flag, result = SubscribeChain._SubscribeChain__get_subscribe_no_exits(
            subscribe_name="主角 S01",
            no_exists=no_exists,
            mediakey="media-key",
            begin_season=1,
            total_episode=48,
            start_episode=1,
        )

        self.assertFalse(exist_flag)
        self.assertEqual(result["media-key"][1].episodes, [])
        self.assertEqual(result["media-key"][1].start_episode, 1)
        self.assertEqual(result["media-key"][1].total_episode, 48)

    def test_resolve_subscribe_missing_combines_library_gap_and_download_history_without_side_effects(self):
        """目标满足查询应复用主程序媒体库缺集与订阅下载历史的合并口径，且不推进订阅状态。"""
        subscribe = self._build_subscribe(
            best_version=0,
            total_episode=20,
            lack_episode=10,
            note=list(range(11, 21)),
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={1: list(range(1, 21))},
            title_year="Test Show (2026)",
        )
        library_missing = {
            1: {
                1: SimpleNamespace(
                    season=1,
                    episodes=list(range(11, 21)),
                    total_episode=20,
                    start_episode=11,
                    require_complete_coverage=False,
                )
            }
        }
        updates = []

        class _DownloadChain:
            def get_no_exists_info(self, **kwargs):
                self.kwargs = kwargs
                return False, library_missing

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload))

        chain = SubscribeChain()
        chain.finish_subscribe_or_not = lambda **_kwargs: self.fail("resolve_subscribe_missing must not finish")

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _DownloadChain), patch.object(
            SUBSCRIBE_CHAIN_MODULE,
            "SubscribeOper",
            _SubscribeOper,
        ):
            satisfied, no_exists = chain.resolve_subscribe_missing(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=1,
            )

        self.assertTrue(satisfied)
        self.assertEqual(no_exists, {})
        self.assertEqual(updates, [])

    def test_resolve_subscribe_missing_keeps_library_gap_when_download_history_does_not_cover_it(self):
        """订阅前媒体库已有部分剧集时，目标满足查询应保留仍需下载的媒体库缺口。"""
        subscribe = self._build_subscribe(
            best_version=0,
            total_episode=20,
            lack_episode=20,
            note=[],
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={1: list(range(1, 21))},
            title_year="Test Show (2026)",
        )
        library_missing = {
            1: {
                1: SimpleNamespace(
                    season=1,
                    episodes=list(range(11, 21)),
                    total_episode=20,
                    start_episode=11,
                    require_complete_coverage=False,
                )
            }
        }

        class _DownloadChain:
            def get_no_exists_info(self, **_kwargs):
                return False, library_missing

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _DownloadChain):
            satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=1,
            )

        self.assertFalse(satisfied)
        self.assertEqual(no_exists[1][1].episodes, list(range(11, 21)))
        self.assertEqual(no_exists[1][1].start_episode, 1)
        self.assertEqual(no_exists[1][1].total_episode, 20)

    def test_resolve_subscribe_missing_uses_readonly_effective_total_from_mediainfo(self):
        """只读目标查询应使用最新媒体信息扩大有效总集数，但不能写回订阅或发送刷新事件。"""
        subscribe = self._build_subscribe(
            best_version=0,
            total_episode=10,
            lack_episode=0,
            note=list(range(1, 11)),
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={1: list(range(1, 21))},
            title_year="Test Show (2026)",
        )
        captured_totals = []

        class _DownloadChain:
            def get_no_exists_info(self, **kwargs):
                captured_totals.append(kwargs["totals"])
                return False, {
                    1: {
                        1: SimpleNamespace(
                            season=1,
                            episodes=list(range(11, 21)),
                            total_episode=20,
                            start_episode=11,
                            require_complete_coverage=False,
                        )
                    }
                }

        class _EventManager:
            def send_event(self, *_args, **_kwargs):
                raise AssertionError("resolve_subscribe_missing must not send refresh events")

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _DownloadChain), patch.object(
            SUBSCRIBE_CHAIN_MODULE,
            "eventmanager",
            _EventManager(),
        ):
            satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=1,
            )

        self.assertFalse(satisfied)
        self.assertEqual(captured_totals, [{1: 20}])
        self.assertEqual(no_exists[1][1].episodes, list(range(11, 21)))
        self.assertEqual(subscribe.total_episode, 10)
        self.assertEqual(subscribe.lack_episode, 0)
        self.assertEqual(subscribe.note, list(range(1, 11)))

    def test_resolve_subscribe_missing_preserves_special_season_zero_totals(self):
        """特别季 S0 是合法订阅季，目标满足查询必须按订阅总集数裁剪媒体库缺集。"""
        subscribe = self._build_subscribe(
            best_version=0,
            season=0,
            total_episode=5,
            lack_episode=2,
            note=[1, 2, 3],
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=0, season=0)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={0: list(range(1, 4))},
            title_year="Test Show (2026)",
        )
        captured_totals = []

        class _DownloadChain:
            def get_no_exists_info(self, **kwargs):
                captured_totals.append(kwargs["totals"])
                if kwargs["totals"] == {0: 5}:
                    return False, {
                        1: {
                            0: SimpleNamespace(
                                season=0,
                                episodes=[4, 5],
                                total_episode=5,
                                start_episode=1,
                                require_complete_coverage=False,
                            )
                        }
                    }
                return True, {}

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _DownloadChain):
            satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=1,
            )

        self.assertFalse(satisfied)
        self.assertEqual(captured_totals, [{0: 5}])
        self.assertEqual(no_exists[1][0].episodes, [4, 5])

    def test_build_subscribe_meta_preserves_special_season_zero(self):
        """订阅构造 MetaInfo 的统一入口必须保留 S0。"""
        subscribe = self._build_subscribe(season=0)

        meta = SUBSCRIBE_CHAIN_MODULE.build_subscribe_meta(subscribe)

        self.assertEqual(meta.begin_season, 0)
        self.assertEqual(meta.type, MediaType.TV)

    def test_follow_preserves_shared_special_season_zero(self):
        """follow 分享订阅携带 S0 时，标题规整不能把合法季号覆盖成未指定。"""
        added_calls = []

        class _SubscribeOper:
            """提供订阅存在性查询，避免依赖真实数据库。"""

            def exists(self, *args, **kwargs):
                return False

            def exist_history(self, *args, **kwargs):
                return False

        class _SystemConfigOper:
            """提供 follow 用户配置。"""

            def get(self, *args, **kwargs):
                return ["follow-user"]

        class _MoviePilotServerHelper:
            """提供单条 S0 分享订阅。"""

            @staticmethod
            def get_subscribe_shares():
                return [
                    {
                        "share_uid": "follow-user",
                        "name": "Test Show",
                        "type": MediaType.TV.value,
                        "year": "2026",
                        "tmdbid": None,
                        "doubanid": "12345",
                        "season": 0,
                        "best_version": 0,
                        "save_path": None,
                        "search_imdbid": False,
                        "custom_words": None,
                        "media_category": None,
                        "filter_groups": [],
                    }
                ]

        def _add(self, **kwargs):
            added_calls.append(kwargs)
            return 1, None

        def _metainfo(title):
            return SimpleNamespace(name=title, begin_season=None, episode_list=[])

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), patch.object(
            SUBSCRIBE_CHAIN_MODULE,
            "SystemConfigOper",
            _SystemConfigOper,
        ), patch.object(
            SUBSCRIBE_CHAIN_MODULE,
            "MoviePilotServerHelper",
            _MoviePilotServerHelper,
        ), patch.object(
            SUBSCRIBE_CHAIN_MODULE,
            "MetaInfo",
            _metainfo,
        ), patch.object(
            SubscribeChain,
            "add",
            _add,
        ):
            SubscribeChain.follow()

        self.assertEqual(len(added_calls), 1)
        self.assertEqual(added_calls[0]["season"], 0)

    def test_resolve_subscribe_missing_accepts_downloaded_episode_best_version_targets(self):
        """外部完成守卫可按任意已下载版本判定分集洗版目标已满足。"""
        subscribe = self._build_subscribe(
            best_version=1,
            best_version_full=0,
            total_episode=3,
            note=[1],
            episode_priority={"2": 80, "3": 99},
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={1: [1, 2, 3]},
            title_year="Test Show (2026)",
        )

        satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=1,
            best_version_accept_downloaded=True,
        )

        self.assertTrue(satisfied)
        self.assertEqual(no_exists, {})

    def test_resolve_subscribe_missing_default_best_version_requires_top_priority(self):
        """主程序洗版完成口径默认仍要求目标分集达到最高优先级。"""
        subscribe = self._build_subscribe(
            best_version=1,
            best_version_full=0,
            total_episode=3,
            note=[1],
            episode_priority={"2": 80, "3": 99},
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={1: [1, 2, 3]},
            title_year="Test Show (2026)",
        )

        satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=1,
        )

        self.assertFalse(satisfied)
        self.assertEqual(no_exists[1][1].episodes, [1, 2, 3])
        self.assertEqual(no_exists[1][1].total_episode, 3)

    def test_resolve_subscribe_missing_default_best_version_uses_readonly_effective_total(self):
        """只读目标查询扩大有效总集数时，默认洗版口径应把新增集纳入待洗范围。"""
        subscribe = self._build_subscribe(
            best_version=1,
            best_version_full=0,
            total_episode=3,
            episode_priority={"1": 100, "2": 100, "3": 100},
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={1: [1, 2, 3, 4, 5]},
            title_year="Test Show (2026)",
        )

        satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=1,
        )

        self.assertFalse(satisfied)
        self.assertEqual(no_exists[1][1].episodes, [4, 5])
        self.assertEqual(no_exists[1][1].total_episode, 5)
        self.assertEqual(subscribe.total_episode, 3)

    def test_resolve_subscribe_missing_accept_downloaded_keeps_best_version_gap(self):
        """任意版本满足口径仍应保留从未下载过的目标分集。"""
        subscribe = self._build_subscribe(
            best_version=1,
            best_version_full=0,
            total_episode=3,
            note=[1],
            episode_priority={"2": 80},
        )
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            seasons={1: [1, 2, 3]},
            title_year="Test Show (2026)",
        )

        satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=1,
            best_version_accept_downloaded=True,
        )

        self.assertFalse(satisfied)
        self.assertEqual(no_exists[1][1].episodes, [3])
        self.assertEqual(no_exists[1][1].total_episode, 3)

    def test_get_subscribe_no_exists_preserves_complete_coverage_requirement(self):
        """缺集裁剪重建 NotExistMediaInfo 时必须保留全集洗版完整覆盖约束。"""
        no_exists = {
            "media-key": {
                1: SimpleNamespace(
                    season=1,
                    episodes=list(range(1, 13)),
                    total_episode=12,
                    start_episode=1,
                    require_complete_coverage=True,
                )
            }
        }

        exist_flag, result = SubscribeChain._SubscribeChain__get_subscribe_no_exits(
            subscribe_name="主角 S01",
            no_exists=no_exists,
            mediakey="media-key",
            begin_season=1,
            total_episode=12,
            start_episode=1,
            downloaded_episodes=[1, 2, 3],
        )

        self.assertFalse(exist_flag)
        self.assertTrue(result["media-key"][1].require_complete_coverage)
        self.assertEqual(result["media-key"][1].episodes, list(range(4, 13)))

    def test_check_existing_media_refreshes_total_before_resolving_missing(self):
        """主流程应先执行完成前总集数刷新，再复用无副作用缺集查询口径。"""
        subscribe = self._build_subscribe(best_version=0, total_episode=10, lack_episode=0)
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(type=MediaType.TV, title_year="Test Show (2026)")
        calls = []

        def fake_refresh(_self, subscribe, mediainfo):
            calls.append(("refresh", subscribe.total_episode))
            subscribe.total_episode = 20

        def fake_resolve(_self, subscribe, meta, mediainfo, mediakey=None):
            calls.append(("resolve", subscribe.total_episode))
            return False, {"media-key": {1: SimpleNamespace(episodes=[11], total_episode=20, start_episode=1)}}

        chain = SubscribeChain()
        with patch.object(
            SubscribeChain,
            "_SubscribeChain__refresh_total_episode_before_completion",
            fake_refresh,
        ), patch.object(
            SubscribeChain,
            "resolve_subscribe_missing",
            fake_resolve,
        ):
            exist_flag, no_exists = chain.check_and_handle_existing_media(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey="media-key",
            )

        self.assertFalse(exist_flag)
        self.assertEqual(calls, [("refresh", 10), ("resolve", 20)])
        self.assertEqual(no_exists["media-key"][1].episodes, [11])

    def test_best_version_full_pack_first_keeps_whole_missing_for_custom_start_episode(self):
        """分集洗版优先全集时，空集列表仍表示下载链按整季资源处理。"""
        subscribe = self._build_subscribe(
            best_version=1,
            best_version_full=0,
            start_episode=44,
            total_episode=48,
            episode_priority={str(episode): 80 for episode in range(44, 49)},
        )

        result = SubscribeChain._SubscribeChain__build_full_pack_first_no_exists(
            subscribe=subscribe,
            mediakey="media-key",
        )

        self.assertEqual(result["media-key"][1].episodes, [])
        self.assertEqual(result["media-key"][1].start_episode, 44)
        self.assertEqual(result["media-key"][1].total_episode, 48)

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
                1: SimpleNamespace(
                    season=1,
                    episodes=[2],
                    total_episode=3,
                    start_episode=1,
                    require_complete_coverage=False,
                )
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
                1: SimpleNamespace(
                    season=1,
                    episodes=[2],
                    total_episode=3,
                    start_episode=1,
                    require_complete_coverage=False,
                )
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
                1: SimpleNamespace(
                    season=1,
                    episodes=[2],
                    total_episode=3,
                    start_episode=1,
                    require_complete_coverage=False,
                )
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
                1: SimpleNamespace(
                    season=1,
                    episodes=[2],
                    total_episode=3,
                    start_episode=1,
                    require_complete_coverage=False,
                )
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
        # update_subscribe_priority 不再回写 lack_episode；lack 由下载链路末端的 __update_lack_episodes 维护
        self.assertNotIn("lack_episode", payload)
        self.assertEqual(subscribe.episode_priority, {"1": 100, "2": 80, "3": 90, "4": 60})
        self.assertEqual(subscribe.current_priority, 90)
        self.assertEqual(subscribe.lack_episode, 3)
        finish_mock.assert_not_called()

    def test_update_subscribe_priority_updates_all_target_episodes_without_finishing(self):
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
        ) as finish_mock, patch.object(SUBSCRIBE_CHAIN_MODULE, "logger") as logger_mock:
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
        # 完成判定仍由 finish_subscribe_or_not 统一处理，避免优先级更新和流程尾部重复完成
        self.assertNotIn("lack_episode", payload)
        finish_mock.assert_not_called()
        self.assertFalse(
            [call for call in logger_mock.info.call_args_list if "洗版完成" in call.args[0]],
            "update_subscribe_priority should not emit completion logs before finish_subscribe_or_not finishes",
        )

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
        self.assertNotIn("lack_episode", payload)
        finish_mock.assert_not_called()

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
        self.assertNotIn("lack_episode", payload)
        finish_mock.assert_not_called()

    def test_finish_subscribe_or_not_does_not_finish_best_version_twice_after_download_completion(self):
        """洗版订阅本轮下载已触发完成时，流程尾部不应对同一订阅再次完成。"""
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 90, "3": 90},
            current_priority=90,
            lack_episode=2,
        )
        downloads = [
            self._build_download(priority=100, selected_episodes=[2]),
            self._build_download(priority=100, selected_episodes=[3]),
        ]
        chain = SubscribeChain()
        meta = SimpleNamespace(type=MediaType.TV)
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper") as subscribe_oper_cls, patch.object(
            SubscribeChain,
            "_SubscribeChain__finish_subscribe",
        ) as finish_mock:
            subscribe_oper = subscribe_oper_cls.return_value
            subscribe_oper.update.return_value = None

            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                downloads=downloads,
                lefts={},
            )

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

    def test_best_version_interested_episodes_excludes_same_priority(self):
        """同 pri_order 的候选不应再把已达到该优先级的集列为可升级集。

        回归场景：E2 已记录在 episode_priority 中为 99，候选种子标题覆盖 E2/E3 且
        其 pri_order=99；E2 不应进入 interested 集合，E3（None）则应进入。这是
        洗版重复下载链路的源头判定，必须保持"严格大于"语义。
        """
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 99},
            current_priority=100,
        )
        context = SimpleNamespace(
            meta_info=SimpleNamespace(season_list=[1], episode_list=[2, 3]),
            selected_episodes=None,
        )

        interested = SubscribeChain._SubscribeChain__get_best_version_interested_episodes(
            subscribe=subscribe,
            context=context,
            priority=99,
        )

        self.assertEqual(interested, [3])

    def test_best_version_interested_episodes_uses_title_episode_list_for_full_pack(self):
        """整包候选（标题展开的集列表）只把仍可提升优先级的集纳入 interested。

        防回归场景：标题显示"第53-104集"，实际目标范围只有 1..92，episode_priority
        已经把 1..82 升到 100，E83 已经记到 99。同 pri_order=99 的同一资源再来时，
        interested 应只剩 [84..92]，绝不能含 E83，否则后续下载层会再下一次同优先级。
        """
        subscribe = self._build_subscribe(
            total_episode=92,
            episode_priority={
                **{str(ep): 100 for ep in range(1, 83)},
                "83": 99,
            },
            current_priority=99,
        )
        context = SimpleNamespace(
            meta_info=SimpleNamespace(season_list=[1], episode_list=list(range(53, 105))),
            selected_episodes=None,
        )

        interested = SubscribeChain._SubscribeChain__get_best_version_interested_episodes(
            subscribe=subscribe,
            context=context,
            priority=99,
        )

        self.assertEqual(interested, list(range(84, 93)))


class SubscribeFilterAllowedEpisodesTest(TestCase):
    """验证洗版过滤循环会把 interested 集合落到 context.allowed_episodes 上。

    这条用例直接覆盖回归点：当 __get_best_version_interested_episodes 返回非空
    集合时，候选必须带着允许集进入下载层，下游 batch_download 才能在标题元数据
    与实际种子文件错位时做出正确取舍。
    """

    def _build_subscribe(self, **overrides):
        return SubscribeChainTest()._build_subscribe(**overrides)

    def test_filter_writes_allowed_episodes_to_context(self):
        subscribe = self._build_subscribe(
            total_episode=92,
            episode_priority={
                **{str(ep): 100 for ep in range(1, 83)},
                "83": 99,
            },
            current_priority=99,
        )
        context = SimpleNamespace(
            meta_info=SimpleNamespace(season_list=[1], episode_list=list(range(53, 105))),
            selected_episodes=None,
        )

        interested = SubscribeChain._SubscribeChain__get_best_version_interested_episodes(
            subscribe=subscribe,
            context=context,
            priority=99,
        )
        # 复刻 subscribe.py 过滤循环中的赋值，确认结果作为允许集传递。
        context.allowed_episodes = set(interested) if interested else None

        self.assertIsNotNone(context.allowed_episodes)
        self.assertEqual(context.allowed_episodes, set(range(84, 93)))
        # 关键回归点：E83 已达到 99，不在允许集内；下游交集后即不会再下 E83。
        self.assertNotIn(83, context.allowed_episodes)

    def test_filter_leaves_allowed_episodes_none_when_no_upgrade(self):
        """同 pri_order 且目标集均已达到该优先级时，候选不应被放行，
        相应地也不会有 allowed_episodes 被写入。"""
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 99, "3": 99},
            current_priority=99,
        )
        context = SimpleNamespace(
            meta_info=SimpleNamespace(season_list=[1], episode_list=[2, 3]),
            selected_episodes=None,
        )

        interested = SubscribeChain._SubscribeChain__get_best_version_interested_episodes(
            subscribe=subscribe,
            context=context,
            priority=99,
        )

        self.assertEqual(interested, [])

    def test_filter_writes_allowed_episodes_in_match_path(self):
        """RSS/订阅刷新分支 match() 需要与 search() 对称地写入 allowed_episodes。

        match() 路径下候选是 `_context = copy.copy(context)`，再走 best_version
        判定。此用例复刻 match() 的过滤序列，验证浅拷贝后的 _context 在写入
        allowed_episodes 时不会污染原始 context，且写入结果与 search() 一致。
        若 match() 分支漏写 allowed_episodes，下游 batch_download 将看不到允许集
        约束，回归到 2c458317 之前的同优先级重复下载状态。
        """
        import copy

        subscribe = self._build_subscribe(
            total_episode=92,
            episode_priority={
                **{str(ep): 100 for ep in range(1, 83)},
                "83": 99,
            },
            current_priority=99,
        )
        original_context = SimpleNamespace(
            meta_info=SimpleNamespace(season_list=[1], episode_list=list(range(53, 105))),
            selected_episodes=None,
            allowed_episodes=None,
        )
        _context = copy.copy(original_context)

        interested = SubscribeChain._SubscribeChain__get_best_version_interested_episodes(
            subscribe=subscribe,
            context=_context,
            priority=99,
        )
        # 复刻 match() 中的赋值；search() 与 match() 必须保持同形以避免分支漏改。
        if interested:
            _context.allowed_episodes = set(interested)

        self.assertEqual(_context.allowed_episodes, set(range(84, 93)))
        # 浅拷贝 + 新字段写入不应反向污染源 context（match() 中 contexts 缓存可能跨多次匹配复用）。
        self.assertIsNone(original_context.allowed_episodes)


class SubscribeNoteTrackingTest(TestCase):
    """覆盖洗版与非洗版下 subscribe.note 的下载历史追踪。

    回归目标：finish_subscribe_or_not 必须在所有订阅模式下都把本轮下载的集数追加进
    subscribe.note；__get_downloaded 在洗版分支必须把 note 与 episode_priority==100
    的完成集合并返回，避免迁移或低优先级下载场景下已下集被误判为"未下载"。
    """

    def _build_subscribe(self, **overrides):
        return SubscribeChainTest()._build_subscribe(**overrides)

    @staticmethod
    def _build_download_context(episodes):
        """构造一个最小化下载 context：只携带 finish_subscribe_or_not / __update_subscribe_note 路径会读到的字段。"""
        return SimpleNamespace(
            meta_info=SimpleNamespace(season_list=[1], episode_list=list(episodes)),
            media_info=SimpleNamespace(
                type=MediaType.TV,
                tmdb_id=1,
                douban_id=None,
            ),
            torrent_info=SimpleNamespace(pri_order=99, title="fake-torrent"),
            selected_episodes=list(episodes),
        )

    def test_finish_subscribe_writes_note_for_best_version_downloads(self):
        """洗版分支若产生 downloads，subscribe.note 必须被追加，不再被 best_version 标志拦截。

        旧逻辑只在非洗版分支调用 __update_subscribe_note，导致 best_version=1 时
        下载历史只落在 episode_priority；用户切回普通订阅或排障对账时缺失"下过哪些集"
        的事实源。这条用例验证修复后两个分支都会写 note。
        """
        subscribe = self._build_subscribe(
            best_version=1,
            total_episode=92,
            episode_priority={"1": 100},
            note=[1],
        )
        chain = SubscribeChain()
        downloads = [self._build_download_context([83])]

        captured_updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                captured_updates.append((subscribe_id, payload))

            def get(self, *args, **kwargs):
                return subscribe

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), patch.object(
            SubscribeChain,
            "update_subscribe_priority",
        ), patch.object(
            SubscribeChain,
            "_SubscribeChain__finish_subscribe",
        ):
            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.TV),
                mediainfo=SimpleNamespace(title_year="Test Show (2026)", type=MediaType.TV,
                                          tmdb_id=1, douban_id=None),
                downloads=downloads,
                lefts=None,
            )

        # note 更新必然发生在 SubscribeOper.update 上，定位"note" 键的最近一次写入。
        note_writes = [payload["note"] for _, payload in captured_updates if "note" in payload]
        self.assertTrue(note_writes, "best_version downloads should still trigger note update")
        self.assertIn(83, note_writes[-1])
        self.assertIn(1, note_writes[-1])  # 既有 note 保留

    def test_finish_subscribe_skips_note_when_no_downloads(self):
        """没有 downloads 时不应触碰 note，避免空写入或误清除。"""
        subscribe = self._build_subscribe(best_version=1, total_episode=92, note=[1, 2])
        chain = SubscribeChain()

        captured_updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                captured_updates.append((subscribe_id, payload))

            def get(self, *args, **kwargs):
                return subscribe

        with patch.object(SUBSCRIBE_CHAIN_MODULE, "SubscribeOper", _SubscribeOper), patch.object(
            SubscribeChain,
            "_SubscribeChain__is_best_version_complete",
            return_value=False,
        ), patch.object(
            SubscribeChain,
            "_SubscribeChain__finish_subscribe",
        ):
            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.TV),
                mediainfo=SimpleNamespace(title_year="Test Show (2026)", type=MediaType.TV,
                                          tmdb_id=1, douban_id=None),
                downloads=None,
                lefts=None,
            )

        # 无下载时不应该有 note 写入。
        self.assertFalse(
            [payload for _, payload in captured_updates if "note" in payload],
            "note must not be touched when downloads is empty",
        )

    def test_get_downloaded_best_version_returns_only_completed_episodes(self):
        """关键回归：洗版分支不得把 note 合并进 __get_downloaded 返回值。

        否则 check_and_handle_existing_media → __get_subscribe_no_exits 会把
        priority<100 但已下载的集从 pending no_exists 中减掉，配合 force=True 但
        __is_best_version_complete=False 的 finish_subscribe_or_not，会让订阅每轮
        都跳过搜索却又永远不完成。__get_downloaded 在洗版下的语义是"无需再处理的
        集"，只有 priority==100 才满足该语义。
        """
        subscribe = self._build_subscribe(
            best_version=1,
            total_episode=3,
            episode_priority={"1": 100, "2": 100, "3": 99},
            note=[1, 2, 3],
        )

        downloaded = SubscribeChain._SubscribeChain__get_downloaded(subscribe)

        # E3 priority=99 仍是 pending，绝对不能合并到 downloaded 里
        self.assertEqual(downloaded, [1, 2])
        self.assertNotIn(3, downloaded)

    def test_get_downloaded_non_best_version_reads_note_after_wash_migration(self):
        """迁移场景：洗版期间 finish_subscribe_or_not 把下载集写入 note；
        用户随后把 best_version 关掉，订阅切回普通模式时 __get_downloaded
        从非洗版分支读取 note，旧洗版集仍能作为"已下载"被识别，避免重新匹配。
        """
        subscribe = self._build_subscribe(
            best_version=0,
            total_episode=5,
            episode_priority={"1": 100, "2": 99},  # 旧洗版残留，普通分支不读
            note=[1, 2, 3],
        )

        downloaded = SubscribeChain._SubscribeChain__get_downloaded(subscribe)

        self.assertEqual(downloaded, [1, 2, 3])
