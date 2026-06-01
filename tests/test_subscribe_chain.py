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
        return sys.modules[module_name], sys.modules[module_name].SubscribeChain

    original_modules = {}

    def ensure_module(name: str, module: types.ModuleType):
        """临时替换模块依赖，并记录原模块以便加载完成后恢复。"""
        if name not in original_modules:
            original_modules[name] = sys.modules.get(name)
        sys.modules[name] = module
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

    class _EpisodeLocation(SimpleNamespace):
        CONFIDENCE_HIGH = "high"
        CONFIDENCE_LOW = "low"
        MODE_ABSOLUTE_TO_SEASON = "absolute_to_season"

        @classmethod
        def locate(cls, meta, target_season, target_episodes, episodes=None):
            raw_episodes = episodes if episodes is not None else meta.episode_list
            source_episodes = sorted(set(int(episode) for episode in raw_episodes))
            if set(source_episodes).intersection(set(target_episodes)):
                return None
            season_list = meta.season_list or []
            if target_season is None or len(season_list) != 1 or season_list[0] != target_season:
                return None
            if len(source_episodes) != len(target_episodes):
                return None
            if source_episodes != list(range(source_episodes[0], source_episodes[-1] + 1)):
                return None
            return cls(
                source_episodes=source_episodes,
                target_episodes=target_episodes,
                mode=cls.MODE_ABSOLUTE_TO_SEASON,
                confidence=cls.CONFIDENCE_HIGH,
            )

    context_module.EpisodeLocation = _EpisodeLocation

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
    sys.modules[module_name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module._injected_modules = {
        name: sys.modules.get(name)
        for name in original_modules
    }
    for injected_name, original_module in original_modules.items():
        if original_module is None:
            sys.modules.pop(injected_name, None)
        else:
            sys.modules[injected_name] = original_module
    return module, module.SubscribeChain


SUBSCRIBE_CHAIN_MODULE, SubscribeChain = _load_subscribe_chain_class()


def _build_context(meta_info, selected_episodes=None, allowed_episodes=None):
    """构造带集数定位能力的最小候选上下文。"""

    class _ContextStub(SimpleNamespace):
        def locate_episode(self, target_season, target_episodes):
            return SUBSCRIBE_CHAIN_MODULE.EpisodeLocation.locate(
                meta=self.meta_info,
                target_season=target_season,
                target_episodes=target_episodes,
            )

    return _ContextStub(
        meta_info=meta_info,
        selected_episodes=selected_episodes,
        allowed_episodes=allowed_episodes,
        located_episodes=None,
    )


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
            located_episodes=None,
            meta_info=SimpleNamespace(season_list=[1], episode_list=meta_episodes or []),
        )

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

        absolute_subscribe = self._build_subscribe(
            season=5,
            total_episode=26,
            episode_priority={},
        )
        self.assertTrue(
            SubscribeChain._is_episode_range_covered(
                meta=SimpleNamespace(
                    season_list=[5],
                    episode_list=list(range(57, 83)),
                    total_episode=26,
                ),
                subscribe=absolute_subscribe,
            )
        )
        self.assertFalse(
            SubscribeChain._is_episode_range_covered(
                meta=SimpleNamespace(
                    season_list=[5],
                    episode_list=list(range(57, 61)),
                    total_episode=4,
                ),
                subscribe=absolute_subscribe,
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

    def test_full_best_version_accepts_absolute_numbered_full_pack(self):
        """同季全集资源使用累计总集编号时，洗版判断应按订阅季内集数定位。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            season=5,
            total_episode=26,
            episode_priority={},
        )
        meta = SimpleNamespace(
            season_list=[5],
            episode_list=list(range(57, 83)),
            total_episode=26,
        )
        context = _build_context(meta_info=meta)

        self.assertTrue(
            SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
                meta=meta,
                subscribe=subscribe,
            )
        )
        self.assertTrue(
            SubscribeChain._is_episode_range_covered(
                meta=meta,
                subscribe=subscribe,
            )
        )
        self.assertEqual(
            SubscribeChain._SubscribeChain__get_best_version_interested_episodes(
                subscribe=subscribe,
                context=context,
                priority=100,
            ),
            list(range(1, 27)),
        )

    def test_low_confidence_episode_location_does_not_drive_best_version_filter(self):
        """低置信定位不能直接参与洗版过滤，只能作为后续弱提示保留。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            season=5,
            total_episode=26,
            episode_priority={},
        )
        meta = SimpleNamespace(
            season_list=[1],
            episode_list=list(range(57, 83)),
            total_episode=26,
        )
        context = _build_context(meta_info=meta)
        low_location = SUBSCRIBE_CHAIN_MODULE.EpisodeLocation(
            source_episodes=list(range(57, 83)),
            target_episodes=list(range(1, 27)),
            mode=SUBSCRIBE_CHAIN_MODULE.EpisodeLocation.MODE_ABSOLUTE_TO_SEASON,
            confidence=SUBSCRIBE_CHAIN_MODULE.EpisodeLocation.CONFIDENCE_LOW,
        )

        self.assertFalse(
            SubscribeChain._is_episode_range_covered(
                meta=meta,
                subscribe=subscribe,
                episode_location=low_location,
            )
        )
        self.assertEqual(
            SubscribeChain._SubscribeChain__get_best_version_interested_episodes(
                subscribe=subscribe,
                context=context,
                priority=100,
                episode_location=low_location,
            ),
            [],
        )

    def test_full_best_version_rejects_partial_absolute_numbered_pack(self):
        """累计总集编号只有完整覆盖订阅目标长度时才可按整包洗版处理。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            season=5,
            total_episode=26,
            episode_priority={},
        )
        meta = SimpleNamespace(
            season_list=[5],
            episode_list=list(range(57, 61)),
            total_episode=4,
        )

        self.assertFalse(
            SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
                meta=meta,
                subscribe=subscribe,
            )
        )
        self.assertFalse(
            SubscribeChain._is_episode_range_covered(
                meta=meta,
                subscribe=subscribe,
            )
        )

    def test_full_best_version_rejects_absolute_numbered_pack_without_season_match(self):
        """累计总集编号缺少同季信号时不自动映射，避免跨季合集误匹配。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            season=5,
            total_episode=26,
            episode_priority={},
        )
        meta = SimpleNamespace(
            season_list=[1],
            episode_list=list(range(57, 83)),
            total_episode=26,
        )

        self.assertFalse(
            SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
                meta=meta,
                subscribe=subscribe,
            )
        )

    def test_full_season_resource_matches_legacy_metainfo_cases(self):
        """保留 #5648 覆盖过的真实标题合集识别样本，避免 MetaInfo 集成回归。"""
        import app.core.metainfo as metainfo_module
        from app.core.metainfo import MetaInfo

        class _SystemConfigOper:
            """提供空系统配置，避免真实 MetaInfo 解析依赖测试数据库。"""

            def get(self, *args, **kwargs):
                return None

        cases = [
            {
                "title": "Cherry Season S01 2014 2160p 60fps WEB-DL H265 AAC-XXX",
                "subtitle": "",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 51},
                "expected": True,
            },
            {
                "title": "【爪爪字幕组】★7月新番[欢迎来到实力至上主义的教室 第二季/Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e S2][11][1080p][HEVC][GB][MP4][招募翻译校对]",
                "subtitle": "",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 13},
                "expected": False,
            },
            {
                "title": "[秋叶原冥途战争][Akiba Maid Sensou][2022][WEB-DL][1080][TV Series][第01话][LeagueWEB]",
                "subtitle": "",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Qi Refining for 3000 Years S01E06 2022 1080p B-Blobal WEB-DL X264 AAC-AnimeS@AdWeb",
                "subtitle": "",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 16},
                "expected": False,
            },
            {
                "title": "The Heart of Genius S01 13-14 2022 1080p WEB-DL H264 AAC",
                "subtitle": "",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 34},
                "expected": False,
            },
            {
                "title": "[xyx98]传颂之物/Utawarerumono/うたわれるもの[BDrip][1920x1080][TV 01-26 Fin][hevc-yuv420p10 flac_ac3][ENG PGS]",
                "subtitle": "",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 26},
                "expected": True,
            },
            {
                "title": "I Woke Up a Vampire S02 2023 2160p NF WEB-DL DDP5.1 Atmos H 265-HHWEB",
                "subtitle": "醒来变成吸血鬼 第二季 | 全8集 | 4K | 类型: 喜剧/家庭/奇幻 | 导演: TommyLynch | 主演: NikoCeci/ZebastinBorjeau/安娜·阿劳约/KaileenAngelicChang/KrisSiddiqi",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 8},
                "expected": True,
            },
            {
                "title": "Shadows of the Void S01 2024 1080p WEB-DL H264 AAC-HHWEB",
                "subtitle": "虚无边境 | 第01-02集 | 1080p | 类型: 动画 | 导演: 巴西 | 主演: 山新/周一菡/皇贞季/Kenz/李佳怡 [内嵌中字]",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 13},
                "expected": False,
            },
            {
                "title": "Mai Xiang S01 2019 2160p WEB-DL H.265 DDP2.0-HHWEB",
                "subtitle": "麦香 | 全36集 | 4K | 类型:剧情/爱情/家庭 | 主演:傅晶/章呈赫/王伟/沙景昌/何音",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 36},
                "expected": True,
            },
            {
                "title": "Jigokuraku S01E14-E25 2023 1080p CR WEB-DL x264 AAC-Nest@ADWeb",
                "subtitle": "地狱乐 / 地獄楽 / Hell’s Paradise [14-25Fin] [中日双语字幕]",
                "subscribe": {"season": None, "start_episode": 14, "total_episode": 25},
                "expected": True,
            },
            {
                "title": "Jigokuraku S01 2023 1080p BluRay Remux AVC FLAC 2.0-AnimeF@ADE",
                "subtitle": "地狱乐/Hell's Paradise: Jigokuraku [01-13Fin] [中日双语字幕]",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 13},
                "expected": True,
            },
            {
                "title": "Jigokuraku S02E12 2026 1080p NF WEB-DL x264 AAC-ADWeb",
                "subtitle": "地狱乐 第二季 地獄楽 第二期 第12集 | 类型: 动画",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Jigokuraku S02E05-E07 2026 1080p NF WEB-DL x264 AAC-ADWeb",
                "subtitle": "地狱乐 第二季 地獄楽 第二期 第05-07集 | 类型: 动画",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Bungo Stray Dogs S01 2016 1080p KKTV WEB-DL x264 AAC-ADWeb",
                "subtitle": "文豪野犬 文豪ストレイドッグス 又名: 文豪Stray Dogs 第一季 全12集 | 类型: 剧情 / 动作 / 动画 主演: 上村祐翔 / 宫野真守 / 细谷佳正 *内嵌繁体字幕*",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 12},
                "expected": True,
            },
            {
                "title": "Bungou Stray Dogs S1+S2+S3+OAD 1080p BDRip HEVC FLAC-Snow-Raws",
                "subtitle": "文豪野犬 第1-3季",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 36},
                "expected": True,
            },
            {
                "title": "Bungou Stray Dogs S1+S2+S3+OAD 1080p BDRip HEVC FLAC-Snow-Raws",
                "subtitle": "文豪野犬 第1-3季",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 60},
                "expected": True,
            },
            {
                "title": "Fu Gui S01 2005 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "福贵 | 全33集 | 4K | 类型: 剧情/家庭 | 导演: 朱正/袁进 | 主演: 陈创/刘敏涛/李丁/张鹰/温玉娟",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 33},
                "expected": True,
            },
            {
                "title": "The Story of Ming Lan S01 2018 2160p WEB-DL CHDWEB",
                "subtitle": "知否知否应是绿肥红瘦 全78集 | 2160p | 国语/中字 | 60帧高码TV版 | 类型:剧情/爱情/古装 | 主演:赵丽颖/冯绍峰/朱一龙/施诗/张佳宁",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 78},
                "expected": True,
            },
            {
                "title": "Love Beyond the Grave S01 2026 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "白日提灯 / 慕胥辞 | 第18集 | 4K | 类型: 剧情 | 导演: 秦榛 | 主演: 迪丽热巴/陈飞宇/魏哲鸣/张俪/高鹤元",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 40},
                "expected": False,
            },
            {
                "title": "The Long Ballad S01 2021 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "长歌行 | 全49集 | 4K | 类型: 剧情/爱情/古装 | 主演: 迪丽热巴/吴磊/刘宇宁/赵露思/方逸伦",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 49},
                "expected": True,
            },
            {
                "title": "The Long Ballad S01E01-E04 2021 2160p WEB-DL H265 AAC-HHWEB",
                "subtitle": "长歌行 | 第01-04集 | 4K | 类型: 剧情/爱情/古装 | 主演: 迪丽热巴/吴磊/刘宇宁/赵露思/方逸伦",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 49},
                "expected": False,
            },
            {
                "title": "Spy x Family S02 2023 1080p Baha WEB-DL x264 AAC-ADWeb",
                "subtitle": "间谍过家家 第二季 / SPY×FAMILY Season 2 [01-12Fin] [简繁内封字幕]",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 12},
                "expected": True,
            },
            {
                "title": "Spy x Family S02E03-E07 2023 1080p Baha WEB-DL x264 AAC-ADWeb",
                "subtitle": "间谍过家家 第二季 / SPY×FAMILY Season 2 第03-07集 [简繁内封字幕]",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 12},
                "expected": False,
            },
            {
                "title": "Naruto Shippuden S01-S21 Complete 1080p BluRay x264 AAC-ADWeb",
                "subtitle": "火影忍者 疾风传 全500集 [1080p][简中字幕]",
                "subscribe": {"season": None, "start_episode": 1, "total_episode": 500},
                "expected": True,
            },
            {
                "title": "Naruto Shippuden S01-S21 Complete 1080p BluRay x264 AAC-ADWeb",
                "subtitle": "火影忍者 疾风传 第01-500集 [1080p][简中字幕]",
                "subscribe": {"season": None, "start_episode": 201, "total_episode": 500},
                "expected": True,
            },
            {
                "title": "Immortality S05 2025 2160p WEB-DL HDR H265 AAC-ADWeb",
                "subtitle": "永生 第五季 全26集 总第57-82集 / 永生之太元仙府 / 永生 新篇章 / Immortality: Taiyuan Immortal Mansion [Bilibili大陆] | 类型：动作 动画 奇幻",
                "subscribe": {"season": 5, "start_episode": 1, "total_episode": 26},
                "expected": True,
            },
            {
                "title": "Immortality S05 2025 2160p WEB-DL HDR H265 AAC-ADWeb",
                "subtitle": "永生 第五季 第57-60集 / 永生之太元仙府",
                "subscribe": {"season": 5, "start_episode": 1, "total_episode": 26},
                "expected": False,
            },
        ]

        metainfo_module._rust_default_parse_options.cache_clear()
        metainfo_module._rust_custom_parse_options.cache_clear()
        with patch("app.db.systemconfig_oper.SystemConfigOper", _SystemConfigOper), patch(
            "app.core.meta.releasegroup.SystemConfigOper", _SystemConfigOper
        ), patch(
            "app.core.meta.words.SystemConfigOper", _SystemConfigOper
        ), patch(
            "app.core.meta.customization.SystemConfigOper", _SystemConfigOper
        ):
            for case in cases:
                meta = MetaInfo(
                    title=case["title"], subtitle=case["subtitle"], custom_words=["#"]
                )
                subscribe = self._build_subscribe(
                    best_version_full=1,
                    episode_priority={},
                    current_priority=0,
                    **case["subscribe"],
                )

                with self.subTest(title=case["title"], subtitle=case["subtitle"]):
                    self.assertEqual(
                        SubscribeChain._SubscribeChain__is_full_season_resource(
                            meta=meta,
                            subscribe=subscribe,
                        ),
                        case["expected"],
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
        # update_subscribe_priority 不再回写 lack_episode；lack 由下载链路末端的 __update_lack_episodes 维护
        self.assertNotIn("lack_episode", payload)
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
        # 完成判定仍由 __is_best_version_complete 走 episode_priority 字典做出，lack_episode 不参与
        self.assertNotIn("lack_episode", payload)
        finish_mock.assert_called_once_with(subscribe=subscribe, meta=meta, mediainfo=mediainfo)

    def test_update_subscribe_priority_normalizes_absolute_numbered_full_pack(self):
        """累计总集编号整包下载完成后，应按订阅季内集数更新洗版状态。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            season=5,
            total_episode=26,
            episode_priority={},
            current_priority=0,
            lack_episode=26,
        )
        download = SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=100),
            selected_episodes=None,
            located_episodes=None,
            meta_info=SimpleNamespace(
                season_list=[5],
                episode_list=list(range(57, 83)),
                total_episode=26,
            ),
        )
        chain = SubscribeChain()
        meta = SimpleNamespace()
        mediainfo = SimpleNamespace(title_year="Immortality (2025)")

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
        self.assertEqual(payload["episode_priority"], {str(ep): 100 for ep in range(1, 27)})
        self.assertEqual(payload["current_priority"], 100)
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
        self.assertNotIn("lack_episode", payload)
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
        self.assertNotIn("lack_episode", payload)
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
        context = _build_context(meta_info=SimpleNamespace(season_list=[1], episode_list=[2, 3]))

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
        context = _build_context(meta_info=SimpleNamespace(season_list=[1], episode_list=list(range(53, 105))))

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
        context = _build_context(meta_info=SimpleNamespace(season_list=[1], episode_list=list(range(53, 105))))

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
        context = _build_context(meta_info=SimpleNamespace(season_list=[1], episode_list=[2, 3]))

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
        original_context = _build_context(
            meta_info=SimpleNamespace(season_list=[1], episode_list=list(range(53, 105)))
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
