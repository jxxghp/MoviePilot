import asyncio
import sys
import types
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import schemas
from app.application.subscription.contract import (
    SubscriptionPatch,
    SubscriptionWriteResult,
)
from app.application.subscription.mutation import SubscriptionMutation
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType


def _load_subscribe_chain_class():
    """隔离加载 SubscribeChain，避免测试依赖完整运行时环境。"""
    module_name = "_test_subscribe_chain"
    if module_name in sys.modules:
        module = sys.modules[module_name]
        return module, module.SubscribeChain

    # 交互处理器模块须在打桩上下文之外预先真实加载：其模块级会话管理器单例
    # 由 SlashInteractionManager 构造，若在桩内导入会绑定桩类并残留 sys.modules，
    # 污染依赖真实会话管理器的后续测试
    import app.application.messaging.subscribe  # noqa: F401

    stub_deps = {}

    def ensure_module(name: str, module: types.ModuleType):
        """登记一个加载期临时替换模块；实际替换与精确还原由 stub_modules 在加载时统一处理。"""
        stub_deps[name] = module
        return module

    chain_module = ensure_module("app.chain", types.ModuleType("app.chain"))
    chain_module.__path__ = []

    class _ChainBase:
        subscription_repository = SimpleNamespace()

        def __init__(self):
            """装配隔离链依赖和显式同步订阅修改作用域。"""
            self.messagehelper = SimpleNamespace(put=lambda *args, **kwargs: None)
            self.subscription_repository = type(self).subscription_repository
            self.sync_subscription_mutation_scope = self._subscription_mutation_scope

        @contextmanager
        def _subscription_mutation_scope(self):
            """提供委托当前测试 repository 的同步修改命令。"""
            yield SimpleNamespace(update=self._update_subscription)

        def _update_subscription(
            self,
            subscribe_id,
            payload,
            _actor,
            existing=None,
            scene="update",
        ):
            """记录测试写入并返回与生产命令一致的前后快照。"""
            updated = self.subscription_repository.update(
                subscribe_id,
                SubscriptionPatch(payload),
            )
            old = existing.to_dict() if existing else {}
            new = {**old, **payload} if updated else {}
            return SubscriptionMutation(
                snapshot=replace(existing, **payload),
                old=old,
                new=new,
            )

        def post_message(self, *args, **kwargs):
            return None

        async def async_post_message(self, *args, **kwargs):
            return None

        def recognize_media(self, *args, **kwargs):
            return None

    chain_base_module = ensure_module(
        "app.chain.base",
        types.ModuleType("app.chain.base"),
    )
    chain_base_module.ChainBase = _ChainBase

    # 链内功能域 mixin：交互四件套委托与音乐订阅域，隔离加载以空 mixin 注入
    interaction_mixin_module = ensure_module("app.chain._interaction", types.ModuleType("app.chain._interaction"))
    interaction_mixin_module.InteractionChainMixin = type("InteractionChainMixin", (), {})
    music_mixin_module = ensure_module("app.chain._music", types.ModuleType("app.chain._music"))
    music_mixin_module.MusicSubscribeMixin = type("MusicSubscribeMixin", (), {})

    class _MediaChain:
        """提供订阅链隔离测试所需的统一媒体识别接口。"""

        def recognize_media(self, *args, **kwargs):
            """同步识别默认返回空结果，由具体用例显式替换。"""
            return None

        async def async_recognize_media(self, *args, **kwargs):
            """异步识别默认返回空结果，由具体用例显式替换。"""
            return None

        def recognize_by_meta(self, *args, **kwargs):
            """同步按元数据识别默认返回空结果。"""
            return None

        async def async_recognize_by_meta(self, *args, **kwargs):
            """异步按元数据识别默认返回空结果。"""
            return None

        def supplement_media_info(self, mediainfo, *args, **kwargs):
            """隔离测试不访问外部附加信息源，原样返回识别结果。"""
            return mediainfo

    interaction_module = ensure_module(
        "app.application.messaging.interaction", types.ModuleType("app.application.messaging.interaction")
    )

    class _SlashInteractionManager:
        def create_or_replace(self, *args, **kwargs):
            return SimpleNamespace(request_id="request-id")

        def get_by_id(self, *args, **kwargs):
            return None

        def get_by_user(self, *args, **kwargs):
            return None

        def remove(self, *args, **kwargs):
            return None

    # 真实导入 app.application.messaging.subscribe 需要 MessageGateway 类型符号
    interaction_module.MessageGateway = type("MessageGateway", (), {})
    interaction_module.SlashInteractionManager = _SlashInteractionManager
    interaction_module.build_navigation_buttons = lambda *args, **kwargs: []
    interaction_module.format_markdown_table = lambda *args, **kwargs: ""
    interaction_module.page_items = lambda *args, **kwargs: []
    interaction_module.supports_interaction_buttons = lambda *args, **kwargs: False
    interaction_module.supports_markdown = lambda *args, **kwargs: False
    interaction_module.update_or_post_message = lambda *args, **kwargs: None

    config_module = ensure_module("app.runtime.config", types.ModuleType("app.runtime.config"))
    config_module.global_vars = SimpleNamespace(is_system_stopped=False)
    config_module.settings = SimpleNamespace(
        RECOGNIZE_SOURCE="themoviedb",
        MP_DOMAIN=lambda path: path,
    )

    context_module = ensure_module("app.domain.context", types.ModuleType("app.domain.context"))
    context_module.TorrentInfo = SimpleNamespace
    context_module.Context = SimpleNamespace
    context_module.MediaInfo = SimpleNamespace
    context_module.MusicInfo = SimpleNamespace
    context_module.MUSIC_ENTITY_ALBUM = "album"
    context_module.MUSIC_ENTITY_RECORDING = "recording"

    event_module = ensure_module("app.runtime.events", types.ModuleType("app.runtime.events"))

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

    meta_module = ensure_module("app.domain.meta", types.ModuleType("app.domain.meta"))
    meta_module.MetaBase = SimpleNamespace
    meta_module.MetaMusic = SimpleNamespace

    metainfo_module = ensure_module("app.domain.metainfo", types.ModuleType("app.domain.metainfo"))

    class _MetaInfo(SimpleNamespace):
        """提供订阅刷新测试需要的 MetaInfo 核心字段。"""

        def __init__(self, title="", *args, **kwargs):
            super().__init__(name=title, episode_list=[])

        @property
        def season_seq(self):
            if getattr(self, "begin_season", None) is not None:
                return str(self.begin_season)
            if getattr(self, "type", None) == MediaType.TV:
                return "1"
            return ""

        @property
        def season(self):
            if getattr(self, "begin_season", None) is not None:
                return f"S{str(self.begin_season).rjust(2, '0')}"
            if getattr(self, "type", None) == MediaType.TV:
                return "S01"
            return ""

    metainfo_module.MetaInfo = _MetaInfo

    words_module = ensure_module("app.domain.meta.words", types.ModuleType("app.domain.meta.words"))

    class _WordsMatcher:
        def prepare(self, title, custom_words=None):
            return title, []

    words_module.WordsMatcher = _WordsMatcher

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
            "media_source",
            "media_id",
            "season",
            "episode_group",
            "best_version",
            "save_path",
            "search_imdbid",
            "custom_words",
            "media_category",
            "filter_groups",
            "music_type",
            "total_tracks",
        }

        def __init__(self, **kwargs):
            for field in self._fields:
                setattr(self, field, None)
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _NotExistMediaInfo:
        def __init__(
            self, season=None, episodes=None, total_episode=None, start_episode=None, require_complete_coverage=False
        ):
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
            self.updated = kwargs.get("updated", False)
            self.total_episode = kwargs.get("total_episode")
            self.source = kwargs.get("source", "未知来源")
            self.reason = kwargs.get("reason", "")
            self.__dict__.update(kwargs)

    class _SubscribeCompletionCheckEventData:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _SubscribeDeletedEventData:
        """提供订阅删除应用服务在隔离加载时需要的事件快照。"""

        def __init__(self, **kwargs):
            """保存事件字段，行为与测试所需的 schema 投影一致。"""
            self.__dict__.update(kwargs)

    mediaserver_schema_module = ensure_module(
        "app.schemas.mediaserver",
        types.ModuleType("app.schemas.mediaserver"),
    )
    mediaserver_schema_module.NotExistMediaInfo = _NotExistMediaInfo
    message_schema_module = ensure_module(
        "app.schemas.message",
        types.ModuleType("app.schemas.message"),
    )
    message_schema_module.Message = _Notification
    subscribe_schema_module = ensure_module(
        "app.schemas.subscribe",
        types.ModuleType("app.schemas.subscribe"),
    )
    subscribe_schema_module.Subscribe = _SubscribeSchema
    subscribe_schema_module.SubscribeEpisodeInfo = _SubscribeEpisodeInfo
    subscribe_schema_module.SubscrbieInfo = _SubscrbieInfo
    subscribe_schema_module.SubscribeDownloadFileInfo = _SubscribeDownloadFileInfo
    subscribe_schema_module.SubscribeLibraryFileInfo = _SubscribeLibraryFileInfo
    event_schema_module = ensure_module(
        "app.schemas.event",
        types.ModuleType("app.schemas.event"),
    )
    event_schema_module.MediaRecognizeConvertEventData = _MediaRecognizeConvertEventData
    event_schema_module.SubscribeEpisodesRefreshEventData = _SubscribeEpisodesRefreshEventData
    event_schema_module.SubscribeCompletionCheckEventData = _SubscribeCompletionCheckEventData
    event_schema_module.SubscribeDeletedEventData = _SubscribeDeletedEventData

    logger_module = ensure_module("app.runtime.log", types.ModuleType("app.runtime.log"))

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

    helper_server_module = ensure_module(
        "app.adapters.external.server", types.ModuleType("app.adapters.external.server")
    )

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

    helper_mediaserver_module = ensure_module(
        "app.application.mediaserver", types.ModuleType("app.application.mediaserver")
    )
    helper_mediaserver_module.MediaServerHelper = type("MediaServerHelper", (), {})

    helper_torrent_module = ensure_module(
        "app.application.torrent.download",
        types.ModuleType("app.application.torrent.download"),
    )
    helper_torrent_module.TorrentHelper = type("TorrentHelper", (), {})

    db_model_module = ensure_module("app.db.models.subscribe", types.ModuleType("app.db.models.subscribe"))

    class _SubscribeModel:
        def __init__(self, **kwargs):
            self.best_version_full = 0
            self.bangumiid = None
            self.anilistid = None
            self.media_source = None
            self.media_id = None
            self.mediaid = None
            self.music_type = None
            self.total_tracks = None
            self.episode_group = None
            for key, value in kwargs.items():
                setattr(self, key, value)

        def to_dict(self):
            return dict(self.__dict__)

    db_model_module.Subscribe = _SubscribeModel

    subscribe_oper_module = ensure_module("app.db.oper.subscribe", types.ModuleType("app.db.oper.subscribe"))

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
        "app.db.oper.downloadhistory": "DownloadHistoryOper",
        "app.db.oper.site": "SiteOper",
        "app.db.oper.systemconfig": "SystemConfigOper",
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
        "app.chain.mediaserver": "MediaServerChain",
        "app.chain.search": "SearchChain",
        "app.chain.tmdb": "TmdbChain",
        "app.chain.torrents": "TorrentsChain",
    }
    for module_name_key, class_name in chain_dependencies.items():
        module = ensure_module(module_name_key, types.ModuleType(module_name_key))
        setattr(module, class_name, type(class_name, (), {}))

    media_chain_module = ensure_module("app.chain.media", types.ModuleType("app.chain.media"))
    media_chain_module.MediaChain = _MediaChain

    import app.chain._music as music_owner
    import app.chain.subscribe.completion as completion_owner
    import app.chain.subscribe.create as create_owner
    import app.chain.subscribe.match as match_owner
    import app.chain.subscribe.notify as notification_owner
    import app.chain.subscribe.policy as policy_owner
    import app.chain.subscribe.query as query_owner
    import app.chain.subscribe.refresh as refresh_owner
    import app.chain.subscribe.search as search_owner
    import app.startup.composition.chain as chain_composition
    from app.application.messaging.subscribe import SubscribeInteractionHandler
    from app.application.subscription.contract import SubscriptionSnapshot, build_subscribe_meta
    from app.chain.subscribe.context import _SubscribeCreateContext
    from app.chain.subscribe.facade import SubscribeChain as ProductionSubscribeChain

    class SubscribeChain(ProductionSubscribeChain):
        """保留存量单元测试按调用时替换 repository 的隔离接缝。"""

        subscription_repository = SimpleNamespace()

        def __init__(self):
            """复用真实运行时依赖，只把同步修改 scope 改为动态读取测试 repository。"""
            super().__init__()
            self.subscription_repository = type(self).subscription_repository
            self.sync_subscription_mutation_scope = self._subscription_mutation_scope

        @contextmanager
        def _subscription_mutation_scope(self):
            """为当前测试 repository 提供同步修改命令。"""
            yield SimpleNamespace(update=self._update_subscription)

        def _update_subscription(
            self,
            subscribe_id,
            payload,
            _actor,
            existing=None,
            scene="update",
        ):
            """记录测试写入并返回与生产命令一致的前后快照。"""
            updated = self.subscription_repository.update(
                subscribe_id,
                SubscriptionPatch(payload),
            )
            old = existing.to_dict() if existing else {}
            new = updated.to_dict() if updated else {}
            return SubscriptionMutation(
                snapshot=updated,
                old=old,
                new=new,
            )

    class _SubscribeOwnerPatchSurface:
        """把旧单文件测试接缝转发到拆包后的唯一实现 owner。"""

        def __init__(self):
            """登记实现依赖的真实所属模块，不向生产包根增加重复导出。"""
            object.__setattr__(self, "SubscribeChain", SubscribeChain)
            object.__setattr__(self, "SubscribeInteractionHandler", SubscribeInteractionHandler)
            object.__setattr__(self, "SubscriptionSnapshot", SubscriptionSnapshot)
            object.__setattr__(self, "_SubscribeCreateContext", _SubscribeCreateContext)
            object.__setattr__(self, "build_subscribe_meta", build_subscribe_meta)
            object.__setattr__(
                self,
                "_targets",
                {
                    "MediaChain": (
                        create_owner,
                        completion_owner,
                        match_owner,
                        query_owner,
                        refresh_owner,
                        search_owner,
                    ),
                    "TorrentHelper": (match_owner,),
                    "DownloadChain": (music_owner, policy_owner, refresh_owner),
                    "eventmanager": (refresh_owner,),
                    "get_configured_system_config": (
                        match_owner,
                        query_owner,
                        search_owner,
                    ),
                    "MoviePilotServerHelper": (chain_composition,),
                    "MetaInfo": (create_owner, match_owner, query_owner),
                    "logger": (
                        completion_owner,
                        create_owner,
                        match_owner,
                        notification_owner,
                        policy_owner,
                        query_owner,
                        refresh_owner,
                        search_owner,
                    ),
                    "add_subscribe": (create_owner,),
                },
            )

        def __getattr__(self, name):
            """读取首个 owner 的依赖，供 unittest.mock 保存并恢复原值。"""
            targets = object.__getattribute__(self, "_targets")
            if name not in targets:
                raise AttributeError(name)
            return getattr(targets[name][0], name)

        def __setattr__(self, name, value):
            """把测试替身同步设置到声明该依赖的所有 owner。"""
            targets = object.__getattribute__(self, "_targets")
            if name not in targets:
                object.__setattr__(self, name, value)
                return
            for target in targets[name]:
                setattr(target, name, value)

        def __delattr__(self, name):
            """兼容 patch.object 对临时新增属性的清理协议。"""
            targets = object.__getattribute__(self, "_targets")
            if name not in targets:
                object.__delattr__(self, name)
                return
            for target in targets[name]:
                delattr(target, name)

    module = _SubscribeOwnerPatchSurface()
    return module, SubscribeChain


SUBSCRIBE_CHAIN_MODULE, SubscribeChain = _load_subscribe_chain_class()
# 进度格式化已迁移到交互处理器，经由隔离加载的模块获取
SubscribeInteractionHandler = SUBSCRIBE_CHAIN_MODULE.SubscribeInteractionHandler


def _patch_media_recognize(module, result):
    """将隔离测试中的统一媒体识别入口替换为指定结果或回调。"""
    recognizer = result if callable(result) else lambda **_kwargs: result
    media_chain = SimpleNamespace(
        recognize_media=recognizer,
        supplement_media_info=lambda mediainfo: mediainfo,
    )
    return patch.object(module, "MediaChain", return_value=media_chain)


class TestSubscribeChain:
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
            "year": "2026",
            "media_source": "themoviedb",
            "media_id": "1",
            "episode_group": None,
            "poster": None,
            "backdrop": None,
            "description": None,
            "last_update": None,
            "username": None,
            "custom_words": None,
        }
        data.update(overrides)
        return SUBSCRIBE_CHAIN_MODULE.SubscriptionSnapshot(**data)

    @staticmethod
    def _build_download(priority, selected_episodes=None, meta_episodes=None):
        return SimpleNamespace(
            torrent_info=SimpleNamespace(pri_order=priority),
            selected_episodes=selected_episodes,
            meta_info=SimpleNamespace(season_list=[1], episode_list=meta_episodes or selected_episodes or []),
            media_info=SimpleNamespace(type=MediaType.TV, media_source="themoviedb", media_id="1"),
            confirmed_full_coverage=False,
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

        assert explicit["best_version"] == 0
        assert explicit["best_version_full"] == 0
        assert omitted["best_version"] == 1
        assert omitted["best_version_full"] == 1

    def test_format_subscribe_progress_preserves_special_season_zero(self):
        """订阅列表展示必须把 S0 当作合法季号，而不是回退到第 1 季。"""
        subscribe = self._build_subscribe(season=0, total_episode=5, lack_episode=2)

        progress = SubscribeInteractionHandler._format_subscribe_progress(subscribe)

        assert progress == "第0季 [3/5]"

    def test_format_subscribe_progress_preserves_special_season_zero_without_total(self):
        """S0 没有总集数时仍显示特别季季号。"""
        subscribe = self._build_subscribe(season=0, total_episode=None, lack_episode=None)

        progress = SubscribeInteractionHandler._format_subscribe_progress(subscribe)

        assert progress == "第0季"

    def test_match_title_fallback_calls_torrent_match_from_class(self):
        """确保标题兜底匹配不依赖 TorrentHelper 实例绑定。"""
        reached = []

        class _PlainTorrentHelper:
            """模拟需要按类调用的 TorrentHelper 形态。"""

            def match_torrent(mediainfo, torrent_meta, torrent):
                """标记类级调用已经正确进入匹配逻辑。"""
                reached.append((mediainfo, torrent_meta, torrent))
                return False

            def filter_torrent(self, *args, **kwargs):
                """保持订阅匹配后续过滤流程可继续执行。"""
                return True

        subscribe = self._build_subscribe(
            best_version=0,
            custom_words=None,
            episode_group=None,
            sites=[],
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

            def get(self, subscribe_id):
                """返回取得订阅准入后的最新快照。"""
                return subscribe if subscribe_id == subscribe.id else None

        chain = SubscribeChain()
        chain.subscription_repository = _SubscribeOper()
        chain.check_and_handle_existing_media = lambda **kwargs: (False, {})
        chain.finish_subscribe_or_not = lambda **kwargs: None

        with (
            patch.object(
                SUBSCRIBE_CHAIN_MODULE,
                "TorrentHelper",
                _PlainTorrentHelper,
            ),
            _patch_media_recognize(SUBSCRIBE_CHAIN_MODULE, mediainfo),
        ):
            chain.match({"test.example": [context]})

        assert len(reached) == 1
        actual_media, actual_meta, actual_torrent = reached[0]
        assert actual_media.title_year == mediainfo.title_year
        assert actual_meta is context.meta_info
        assert actual_torrent is context.torrent_info

    def test_match_accepts_special_season_zero_candidate(self):
        """S0 订阅应允许 S00 候选资源进入下载候选，不能按未指定季处理。"""

        class _TorrentHelper:
            def filter_torrent(self, *args, **kwargs):
                return True

        subscribe = self._build_subscribe(
            best_version=0,
            custom_words=None,
            episode_group=None,
            filter_groups=[],
            keyword=None,
            media_category=None,
            save_path=None,
            search_imdbid=False,
            season=0,
            sites=[],
            username="",
            downloader=None,
        )
        mediainfo = SimpleNamespace(
            clear=lambda: None,
            media_source="themoviedb",
            media_id="1",
            title_year="Test Show (2026)",
            type=MediaType.TV,
        )
        torrent_media = SimpleNamespace(
            clear=lambda: None,
            media_source="themoviedb",
            media_id="1",
            type=MediaType.TV,
        )
        context = SimpleNamespace(
            media_info=torrent_media,
            media_recognize_fail_count=0,
            match_source="unknown",
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
        chain.subscription_repository = _SubscribeOper()
        chain.check_and_handle_existing_media = lambda **kwargs: (False, {})
        chain.get_sub_sites = lambda *_args, **_kwargs: []
        chain.get_params = lambda *_args, **_kwargs: {}
        chain.filter_torrents = lambda **_kwargs: [context.torrent_info]
        chain.finish_subscribe_or_not = lambda **_kwargs: None

        with (
            patch.object(
                SUBSCRIBE_CHAIN_MODULE,
                "TorrentHelper",
                _TorrentHelper,
            ),
            patch.object(
                SubscribeChain,
                "_SubscribeChain__download_best_version_with_full_pack_first",
                _download,
            ),
            _patch_media_recognize(SUBSCRIBE_CHAIN_MODULE, mediainfo),
        ):
            chain.match({"test.example": [context]})

        assert len(download_calls) == 1
        assert download_calls[0]["contexts"][0].meta_info.begin_season == 0
        execution_context = download_calls[0]["execution_context"]
        assert execution_context.lease.subscription_id == subscribe.id
        assert execution_context.lease.operation == "match"
        replacement = chain._subscription_execution_admission.try_acquire(
            subscription_id=subscribe.id,
            operation="search",
            ttl_seconds=60,
        )
        assert replacement is not None
        assert chain._subscription_execution_admission.release(replacement) is True

    def test_get_episode_priority_falls_back_to_current_priority(self):
        subscribe = self._build_subscribe(current_priority=80, episode_priority=None)

        assert SubscribeChain.get_episode_priority(subscribe) == {"1": 80, "2": 80, "3": 80}

    def test_full_best_version_does_not_materialize_episode_priority_from_baseline(self):
        subscribe = self._build_subscribe(
            best_version_full=1,
            current_priority=82,
            episode_priority=None,
        )

        assert SubscribeChain.get_episode_priority(subscribe) == {}
        assert SubscribeChain.get_best_version_current_priority(subscribe) == 82

    def test_get_pending_best_version_episodes_uses_per_episode_status(self):
        subscribe = self._build_subscribe(
            total_episode=5,
            episode_priority={"1": 100, "2": 80, "4": 100},
        )

        assert SubscribeChain._get_pending_best_version_episodes(subscribe) == [2, 3, 5]

    def test_best_version_progress_helpers_return_remaining_priority(self):
        subscribe = self._build_subscribe(
            total_episode=5,
            episode_priority={"1": 100, "2": 80, "4": 100, "5": 70},
            current_priority=100,
        )

        assert SubscribeChain.get_best_version_current_priority(subscribe) == 0
        assert not (SubscribeChain.is_best_version_complete(subscribe))

    def test_best_version_current_priority_uses_legacy_fallback_when_episode_priority_empty(self):
        subscribe = self._build_subscribe(total_episode=3, current_priority=80, episode_priority=None)

        assert SubscribeChain.get_best_version_current_priority(subscribe) == 80

    def test_best_version_progress_helpers_mark_complete_when_all_target_episodes_done(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 100, "3": 100},
            current_priority=90,
        )

        assert SubscribeChain.get_best_version_current_priority(subscribe) == 100
        assert SubscribeChain.is_best_version_complete(subscribe)

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

        assert not (exist_flag)
        assert result["media-key"][1].episodes == [44, 45, 46, 47, 48]
        assert result["media-key"][1].start_episode == 44
        assert result["media-key"][1].total_episode == 48

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

        assert not (exist_flag)
        assert result["media-key"][1].episodes == []
        assert result["media-key"][1].start_episode == 1
        assert result["media-key"][1].total_episode == 48

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
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        chain = SubscribeChain()
        chain.finish_subscribe_or_not = lambda **_kwargs: self.fail("resolve_subscribe_missing must not finish")

        with (
            patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _DownloadChain),
            patch.object(
                SubscribeChain,
                "subscription_repository",
                _SubscribeOper(),
                create=True,
            ),
        ):
            satisfied, no_exists = chain.resolve_subscribe_missing(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=1,
            )

        assert satisfied
        assert no_exists == {}
        assert updates == []

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

        assert not (satisfied)
        assert no_exists[1][1].episodes == list(range(11, 21))
        assert no_exists[1][1].start_episode == 1
        assert no_exists[1][1].total_episode == 20

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

        with (
            patch.object(SUBSCRIBE_CHAIN_MODULE, "DownloadChain", _DownloadChain),
            patch.object(
                SUBSCRIBE_CHAIN_MODULE,
                "eventmanager",
                _EventManager(),
            ),
        ):
            satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey=1,
            )

        assert not (satisfied)
        assert captured_totals == [{1: 20}]
        assert no_exists[1][1].episodes == list(range(11, 21))
        assert subscribe.total_episode == 10
        assert subscribe.lack_episode == 0
        assert subscribe.note == list(range(1, 11))

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

        assert not (satisfied)
        assert captured_totals == [{0: 5}]
        assert no_exists[1][0].episodes == [4, 5]

    def test_build_subscribe_meta_preserves_special_season_zero(self):
        """订阅构造 MetaInfo 的统一入口必须保留 S0。"""
        subscribe = self._build_subscribe(season=0)

        meta = SUBSCRIBE_CHAIN_MODULE.build_subscribe_meta(subscribe)

        assert meta.begin_season == 0
        assert meta.type == MediaType.TV

    def test_follow_preserves_shared_special_season_and_episode_group(self):
        """Follow 分享必须保留合法 S0 与自定义剧集组的完整订阅范围。"""
        added_calls = []
        exists_calls = []
        history_calls = []

        class _SubscribeOper:
            """提供订阅存在性查询，避免依赖真实数据库。"""

            def exists(self, identity):
                exists_calls.append(identity)
                return False

            def history_exists(self, identity):
                history_calls.append(identity)
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
                        "media_source": "douban",
                        "media_id": "12345",
                        "season": 0,
                        "episode_group": "eg-special",
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

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                SUBSCRIBE_CHAIN_MODULE,
                "get_configured_system_config",
                _SystemConfigOper,
            ),
            patch.object(
                SUBSCRIBE_CHAIN_MODULE,
                "MoviePilotServerHelper",
                _MoviePilotServerHelper,
            ),
            patch.object(
                SUBSCRIBE_CHAIN_MODULE,
                "MetaInfo",
                _metainfo,
            ),
            patch.object(
                SubscribeChain,
                "add",
                _add,
            ),
        ):
            SubscribeChain().follow()

        assert len(added_calls) == 1
        assert added_calls[0]["season"] == 0
        assert added_calls[0]["episode_group"] == "eg-special"
        assert exists_calls[0].episode_group == "eg-special"
        assert history_calls[0].episode_group == "eg-special"

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

        assert satisfied
        assert no_exists == {}

    def test_total_episode_decrease_rejects_invalid_missing_scope(self):
        subscribe = self._build_subscribe(best_version=0, total_episode=100, note=[])
        missing_info = SimpleNamespace(
            episodes=list(range(91, 101)),
            require_complete_coverage=False,
        )
        chain = SubscribeChain()

        with patch.object(chain, "resolve_subscribe_missing", return_value=(False, {1: {1: missing_info}})):
            total_episode = chain._SubscribeChain__resolve_total_episode_decrease(
                subscribe=subscribe,
                candidate_total=1,
                meta=SimpleNamespace(type=MediaType.TV, begin_season=1, season=1),
                mediainfo=SimpleNamespace(type=MediaType.TV, seasons={1: [1]}),
                mediakey=1,
            )

        assert total_episode == 1

    def test_resolve_subscribe_missing_accepts_downloaded_legacy_current_priority_targets(self):
        """外部完成守卫读取按集事实时，应保留 current_priority 整体快照兼容。"""
        subscribe = self._build_subscribe(
            best_version=1,
            best_version_full=0,
            total_episode=3,
            current_priority=80,
            episode_priority=None,
            note=[],
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

        assert satisfied
        assert no_exists == {}

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

        assert not (satisfied)
        assert no_exists[1][1].episodes == [1, 2, 3]
        assert no_exists[1][1].total_episode == 3

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

        assert not (satisfied)
        assert no_exists[1][1].episodes == [4, 5]
        assert no_exists[1][1].total_episode == 5
        assert subscribe.total_episode == 3

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

        assert not (satisfied)
        assert no_exists[1][1].episodes == [3]
        assert no_exists[1][1].total_episode == 3

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

        assert not (exist_flag)
        assert result["media-key"][1].require_complete_coverage
        assert result["media-key"][1].episodes == list(range(4, 13))

    def test_check_existing_media_refreshes_total_before_resolving_missing(self):
        """主流程应先执行完成前总集数刷新，再复用无副作用缺集查询口径。"""
        subscribe = self._build_subscribe(best_version=0, total_episode=10, lack_episode=0)
        meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
        mediainfo = SimpleNamespace(type=MediaType.TV, title_year="Test Show (2026)")
        calls = []

        def fake_refresh(_self, subscribe, mediainfo, meta=None, mediakey=None):
            calls.append(("refresh", subscribe.total_episode))
            return replace(subscribe, total_episode=20)

        def fake_resolve(_self, subscribe, meta, mediainfo, mediakey=None):
            calls.append(("resolve", subscribe.total_episode))
            return False, {"media-key": {1: SimpleNamespace(episodes=[11], total_episode=20, start_episode=1)}}

        chain = SubscribeChain()
        with (
            patch.object(
                SubscribeChain,
                "_SubscribeChain__refresh_total_episode_before_completion",
                fake_refresh,
            ),
            patch.object(
                SubscribeChain,
                "resolve_subscribe_missing",
                fake_resolve,
            ),
        ):
            exist_flag, no_exists = chain.check_and_handle_existing_media(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                mediakey="media-key",
            )

        assert not (exist_flag)
        assert calls == [("refresh", 10), ("resolve", 20)]
        assert no_exists["media-key"][1].episodes == [11]

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

        assert result["media-key"][1].episodes == []
        assert result["media-key"][1].start_episode == 44
        assert result["media-key"][1].total_episode == 48
        assert result["media-key"][1].require_complete_coverage

    def test_is_episode_range_covered_matches_pending_episodes(self):
        subscribe = self._build_subscribe(
            total_episode=12,
            episode_priority={
                **{str(ep): 100 for ep in range(1, 5)},
                **{str(ep): 100 for ep in range(8, 13)},
            },
        )

        assert SubscribeChain._is_episode_range_covered(
            meta=SimpleNamespace(episode_list=[5, 6, 7]),
            subscribe=subscribe,
        )
        assert not (
            SubscribeChain._is_episode_range_covered(
                meta=SimpleNamespace(episode_list=[1, 2, 3, 4]),
                subscribe=subscribe,
            )
        )
        assert SubscribeChain._is_episode_range_covered(
            meta=SimpleNamespace(episode_list=[]),
            subscribe=subscribe,
        )

    def test_full_best_version_rejects_episode_resource(self):
        subscribe = self._build_subscribe(best_version_full=1, total_episode=3)

        assert not (
            SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
                meta=SimpleNamespace(season_list=[1], episode_list=[1]),
                subscribe=subscribe,
            )
        )

    def test_full_best_version_accepts_full_pack_resource(self):
        subscribe = self._build_subscribe(best_version_full=1, total_episode=3)

        assert SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
            meta=SimpleNamespace(season_list=[1], episode_list=[]),
            subscribe=subscribe,
        )
        assert SubscribeChain._SubscribeChain__is_full_season_best_version_resource(
            meta=SimpleNamespace(season_list=[1], episode_list=[1, 2, 3]),
            subscribe=subscribe,
        )

    def test_episode_best_version_downloads_full_pack_before_episode_fallback(self):
        subscribe = self._build_subscribe(
            best_version_full=0,
            total_episode=3,
            custom_words="S04 => S01\n第 <> 集 >> EP+66",
        )
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

        assert downloads == [full_pack_context]
        assert lefts == {}
        assert len(calls) == 1
        assert calls[0]["contexts"] == [full_pack_context]
        assert calls[0]["no_exists"]["media-key"][1].episodes == []
        # 订阅识别词须作为入参随下载下传，供整理时复现识别（避免下载模块反查订阅的循环依赖）
        assert calls[0]["custom_words"] == "S04 => S01\n第 <> 集 >> EP+66"

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

        assert downloads == [episode_context]
        assert lefts == {}
        assert len(calls) == 2
        assert calls[0]["contexts"] == [full_pack_context]
        assert calls[1]["no_exists"] is no_exists

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

        assert downloads == [episode_context]
        assert lefts == {}
        assert len(calls) == 1
        assert calls[0]["contexts"] == [episode_context, full_pack_context]
        assert calls[0]["no_exists"] is no_exists

    def test_episode_best_version_falls_back_when_full_pack_does_not_exceed_every_target(self):
        """整包候选未严格高于每个目标集时，应回退到按集下载。"""
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
            """记录整包优先和回退调用，验证整体门槛口径。"""

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

        assert downloads == []
        assert lefts is no_exists
        assert len(calls) == 1
        assert calls[0]["contexts"] == [full_pack_context]
        assert calls[0]["no_exists"] is no_exists

    def test_episode_full_pack_priority_must_strictly_exceed_all_targets(self):
        """缺失集按 0 参与比较，且候选与任一目标集相等时也不优先整包。"""
        subscribe = self._build_subscribe(
            best_version_full=0,
            total_episode=3,
            episode_priority={"1": 90, "2": 80},
            current_priority=0,
        )

        assert not (
            SubscribeChain._SubscribeChain__should_prefer_full_pack_for_episode_best_version(
                subscribe=subscribe,
                priority=82,
            )
        )
        assert not (
            SubscribeChain._SubscribeChain__should_prefer_full_pack_for_episode_best_version(
                subscribe=subscribe,
                priority=90,
            )
        )
        assert SubscribeChain._SubscribeChain__should_prefer_full_pack_for_episode_best_version(
            subscribe=subscribe,
            priority=91,
        )

    def test_full_pack_priority_check_uses_current_priority_fallback(self):
        """没有按集优先级状态时使用 current_priority 兜底判断。"""
        subscribe = self._build_subscribe(total_episode=3, current_priority=80, episode_priority=None)

        assert not (
            SubscribeChain._SubscribeChain__should_prefer_full_pack_for_episode_best_version(
                subscribe=subscribe,
                priority=80,
            )
        )

    def test_full_best_version_completion_uses_baseline_priority(self):
        """全集洗版完成只认整包准入基线，不受保留的按集事实影响。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            current_priority=82,
            episode_priority={"1": 100, "2": 100, "3": 100},
        )

        assert not (SubscribeChain.is_best_version_complete(subscribe))
        completed = replace(
            subscribe,
            current_priority=100,
            episode_priority={"1": 80},
        )
        assert SubscribeChain.is_best_version_complete(completed)

    def test_full_best_version_candidate_uses_baseline_without_allowed_episodes(self):
        """全集候选只比较整包准入基线，不进入按集 interested/allowed_episodes 路径。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            current_priority=82,
            episode_priority={"1": 100, "2": 100, "3": 100},
        )
        context = SimpleNamespace(
            selected_episodes=None,
            meta_info=SimpleNamespace(episode_list=[1, 2, 3]),
            allowed_episodes=None,
        )

        with patch.object(
            SubscribeChain,
            "_SubscribeChain__get_best_version_interested_episodes",
            side_effect=AssertionError("full mode must not inspect episode priorities"),
        ):
            assert not (
                SubscribeChain._SubscribeChain__prepare_best_version_tv_candidate(
                    subscribe=subscribe,
                    context=context,
                    priority=82,
                )
            )
            assert SubscribeChain._SubscribeChain__prepare_best_version_tv_candidate(
                subscribe=subscribe,
                context=context,
                priority=83,
            )

        assert context.allowed_episodes is None

    def test_episode_best_version_candidate_sets_allowed_episodes(self):
        """分集候选继续把实际可提升剧集下传到下载层。"""
        subscribe = self._build_subscribe(
            best_version_full=0,
            current_priority=80,
            episode_priority={"1": 90, "2": 80},
        )
        context = SimpleNamespace(
            selected_episodes=None,
            meta_info=SimpleNamespace(episode_list=[1, 2, 3]),
        )

        accepted = SubscribeChain._SubscribeChain__prepare_best_version_tv_candidate(
            subscribe=subscribe,
            context=context,
            priority=85,
        )

        assert accepted
        assert context.allowed_episodes == {2, 3}

    def test_record_download_facts_uses_selected_episodes(self):
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
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

        snapshot = chain._SubscribeChain__record_subscribe_download_facts(
            subscribe=subscribe,
            mediainfo=mediainfo,
            downloads=[download],
        )

        subscribe_oper.update.assert_called_once()
        payload = subscribe_oper.update.call_args.args[1].to_payload()
        assert payload["episode_priority"] == {"1": 100, "2": 80, "3": 90, "4": 60}
        assert payload["note"] == [3]
        assert snapshot["episodes"] == [3]
        assert "current_priority" not in payload
        assert "lack_episode" not in payload
        assert snapshot["subscribe"].episode_priority == {"1": 100, "2": 80, "3": 90, "4": 60}
        assert subscribe.current_priority == 80
        assert subscribe.lack_episode == 3

    def test_record_download_facts_updates_all_target_episodes_without_finishing(self):
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
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        with (
            patch.object(SUBSCRIBE_CHAIN_MODULE, "logger") as logger_mock,
        ):
            subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

            chain._SubscribeChain__record_subscribe_download_facts(
                subscribe=subscribe,
                mediainfo=mediainfo,
                downloads=downloads,
            )

        payload = subscribe_oper.update.call_args.args[1].to_payload()
        assert payload["episode_priority"] == {"1": 100, "2": 100, "3": 100}
        assert payload["note"] == [2, 3]
        assert "current_priority" not in payload
        assert "lack_episode" not in payload
        assert not ([call for call in logger_mock.info.call_args_list if "洗版完成" in call.args[0]])

    def test_download_facts_require_full_coverage_confirmation_when_pack_has_no_episode_metadata(self):
        subscribe = self._build_subscribe(
            best_version_full=1,
            total_episode=3,
            episode_priority={"1": 80, "2": 80, "3": 80},
            current_priority=80,
            lack_episode=3,
        )
        download = self._build_download(priority=100, selected_episodes=[], meta_episodes=[])
        chain = SubscribeChain()
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

        snapshot = chain._SubscribeChain__record_subscribe_download_facts(
            subscribe=subscribe,
            mediainfo=mediainfo,
            downloads=[download],
        )

        assert snapshot["episodes"] == []
        subscribe_oper.update.assert_not_called()
        assert subscribe.episode_priority == {"1": 80, "2": 80, "3": 80}

    def test_download_facts_write_all_targets_when_full_coverage_is_confirmed(self):
        subscribe = self._build_subscribe(
            best_version_full=0,
            total_episode=3,
            episode_priority={"1": 80, "2": 80, "3": 80},
            current_priority=80,
            lack_episode=3,
        )
        download = self._build_download(priority=100, selected_episodes=[], meta_episodes=[])
        download.confirmed_full_coverage = True
        chain = SubscribeChain()
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

        chain._SubscribeChain__record_subscribe_download_facts(
            subscribe=subscribe,
            mediainfo=mediainfo,
            downloads=[download],
        )

        payload = subscribe_oper.update.call_args.args[1].to_payload()
        assert payload["episode_priority"] == {"1": 100, "2": 100, "3": 100}
        assert payload["note"] == [1, 2, 3]
        assert "current_priority" not in payload
        assert "lack_episode" not in payload

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
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper
        meta = SimpleNamespace(type=MediaType.TV)
        mediainfo = SimpleNamespace(title_year="Test Show (2026)")

        with (
            patch.object(
                SubscribeChain,
                "_SubscribeChain__finish_subscribe",
            ) as finish_mock,
        ):
            subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                downloads=downloads,
                lefts={},
            )

        finish_mock.assert_called_once()
        completed = finish_mock.call_args.kwargs["subscribe"]
        assert completed.note == [2, 3]
        assert completed.episode_priority == {"1": 100, "2": 100, "3": 100}
        assert finish_mock.call_args.kwargs["meta"] is meta
        assert finish_mock.call_args.kwargs["mediainfo"] is mediainfo

    def test_check_keeps_sparse_priority_when_new_episodes_expand_target_range(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            episode_priority={"1": 100, "2": 100, "3": 100},
            current_priority=100,
            lack_episode=0,
        )
        chain = SubscribeChain()
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper
        mediainfo = SimpleNamespace(
            seasons={1: [1, 2, 3, 4, 5]},
            title="Test Show",
            year="2026",
            vote_average=9.5,
            overview="overview",
            imdb_id="tt1234567",
            tvdb_id=99,
            source="themoviedb",
            tmdb_id=1,
            douban_id=None,
            bangumi_id=None,
            anilist_id=None,
            get_poster_image=lambda: "poster",
            get_backdrop_image=lambda: "backdrop",
        )

        with (
            _patch_media_recognize(SUBSCRIBE_CHAIN_MODULE, mediainfo),
        ):
            subscribe_oper.list.return_value = [subscribe]
            subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

            chain.check()

        payload = subscribe_oper.update.call_args.args[1].to_payload()
        assert payload["total_episode"] == 5
        assert payload["lack_episode"] == 2
        assert payload["current_priority"] == 0
        assert payload["episode_priority"] == {"1": 100, "2": 100, "3": 100}
        assert "4" not in payload["episode_priority"]
        assert "5" not in payload["episode_priority"]
        assert subscribe.total_episode == 3
        assert subscribe.lack_episode == 0
        assert subscribe.current_priority == 100

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

        assert interested == [3]

    def test_best_version_interested_episodes_uses_title_episode_list_for_full_pack(self):
        """整包候选（标题展开的集列表）只把仍可提升优先级的集纳入 interested。

        标题显示"第53-104集"，实际目标范围只有 1..92，episode_priority
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

        assert interested == list(range(84, 93))


class TestSubscribeFilterAllowedEpisodes:
    """验证洗版过滤循环会把 interested 集合落到 context.allowed_episodes 上。

    这条用例直接覆盖回归点：当 __get_best_version_interested_episodes 返回非空
    集合时，候选必须带着允许集进入下载层，下游 batch_download 才能在标题元数据
    与实际种子文件错位时做出正确取舍。
    """

    def _build_subscribe(self, **overrides):
        return TestSubscribeChain()._build_subscribe(**overrides)

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

        assert context.allowed_episodes is not None
        assert context.allowed_episodes == set(range(84, 93))
        # E83 已达到 99，不在允许集内；下游交集后即不会再下 E83。
        assert 83 not in context.allowed_episodes

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

        assert interested == []

    def test_filter_writes_allowed_episodes_in_match_path(self):
        """RSS/订阅刷新分支 match() 需要与 search() 对称地写入 allowed_episodes。

        match() 路径下候选是 `_context = copy.copy(context)`，再走 best_version
        判定。此用例复刻 match() 的过滤序列，验证浅拷贝后的 _context 在写入
        allowed_episodes 时不会污染原始 context，且写入结果与 search() 一致。
        若 match() 分支漏写 allowed_episodes，下游 batch_download 将看不到允许集
        约束，导致同优先级资源重复下载。
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

        assert _context.allowed_episodes == set(range(84, 93))
        # 浅拷贝 + 新字段写入不应反向污染源 context（match() 中 contexts 缓存可能跨多次匹配复用）。
        assert original_context.allowed_episodes is None


class TestSubscribeNoteTracking:
    """覆盖洗版与非洗版下 subscribe.note 的下载历史追踪。

    finish_subscribe_or_not 有下载事实时必须追加 note；__get_downloaded 在洗版
    分支只返回 priority==100 的完成集，普通订阅分支继续读取 note。
    """

    def _build_subscribe(self, **overrides):
        return TestSubscribeChain()._build_subscribe(**overrides)

    @staticmethod
    def _build_download_context(episodes):
        """构造一个最小化下载 context：只携带 finish_subscribe_or_not / __update_subscribe_note 路径会读到的字段。"""
        return SimpleNamespace(
            meta_info=SimpleNamespace(season_list=[1], episode_list=list(episodes)),
            media_info=SimpleNamespace(
                type=MediaType.TV,
                media_source="themoviedb",
                media_id="1",
            ),
            torrent_info=SimpleNamespace(pri_order=99, title="fake-torrent"),
            selected_episodes=list(episodes),
        )

    def test_finish_subscribe_writes_note_for_best_version_downloads(self):
        """洗版分支若产生 downloads，subscribe.note 必须被追加。"""
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
                captured_updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

            def get(self, *args, **kwargs):
                return subscribe

        chain.subscription_repository = _SubscribeOper()
        with (
            patch.object(
                SubscribeChain,
                "_SubscribeChain__update_movie_download_priority",
            ),
            patch.object(
                SubscribeChain,
                "_SubscribeChain__finish_subscribe",
            ),
        ):
            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.TV),
                mediainfo=SimpleNamespace(
                    title_year="Test Show (2026)",
                    type=MediaType.TV,
                    media_source="themoviedb",
                    media_id="1",
                ),
                downloads=downloads,
                lefts=None,
            )

        # note 更新必然发生在 SubscribeOper.update 上，定位"note" 键的最近一次写入。
        note_writes = [payload["note"] for _, payload in captured_updates if "note" in payload]
        assert note_writes
        assert 83 in note_writes[-1]
        assert 1 in note_writes[-1]  # 既有 note 保留

    def test_finish_subscribe_skips_note_when_no_downloads(self):
        """没有 downloads 时不应触碰 note，避免空写入或误清除。"""
        subscribe = self._build_subscribe(best_version=1, total_episode=92, note=[1, 2])
        chain = SubscribeChain()

        captured_updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                captured_updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

            def get(self, *args, **kwargs):
                return subscribe

        chain.subscription_repository = _SubscribeOper()
        with (
            patch.object(
                SubscribeChain,
                "_SubscribeChain__is_best_version_complete",
                return_value=False,
            ),
            patch.object(
                SubscribeChain,
                "_SubscribeChain__finish_subscribe",
            ),
        ):
            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.TV),
                mediainfo=SimpleNamespace(title_year="Test Show (2026)", type=MediaType.TV, tmdb_id=1, douban_id=None),
                downloads=None,
                lefts=None,
            )

        # 无下载时不应该有 note 写入。
        assert not ([payload for _, payload in captured_updates if "note" in payload])

    def test_get_downloaded_best_version_returns_only_completed_episodes(self):
        """洗版分支不得把 note 合并进 __get_downloaded 返回值。

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
        assert downloaded == [1, 2]
        assert 3 not in downloaded

    def test_get_downloaded_non_best_version_reads_note_after_wash_migration(self):
        """订阅切回普通模式时 __get_downloaded 从非洗版分支读取 note。"""
        subscribe = self._build_subscribe(
            best_version=0,
            total_episode=5,
            episode_priority={"1": 100, "2": 99},  # 普通分支不读取按集洗版优先级。
            note=[1, 2, 3],
        )

        downloaded = SubscribeChain._SubscribeChain__get_downloaded(subscribe)

        assert downloaded == [1, 2, 3]


class TestSubscribeProgressEntrypoint:
    def setup_method(self):
        self.module, self.SubscribeChain = _load_subscribe_chain_class()

    def _build_subscribe(self, **overrides):
        values = {
            "id": 1,
            "name": "测试剧",
            "type": MediaType.TV.value,
            "season": 1,
            "start_episode": 1,
            "total_episode": 5,
            "lack_episode": 5,
            "note": [],
            "best_version": 1,
            "best_version_full": 0,
            "current_priority": None,
            "episode_priority": {},
            "last_update": None,
            "media_source": "themoviedb",
            "media_id": "10001",
            "year": "2026",
            "manual_total_episode": 0,
        }
        values.update(overrides)
        return self.module.SubscriptionSnapshot(**values)

    def test_compute_lack_episode_counts_best_version_note_and_positive_priority(self):
        subscribe = self._build_subscribe(
            note=[1, "bad"],
            episode_priority={"2": 80, "3": 0, "4": 100},
        )

        lack = self.SubscribeChain.compute_lack_episode(subscribe)

        assert lack == 2

    def test_compute_lack_episode_normal_tv_no_exists_boundaries(self):
        subscribe = self._build_subscribe(best_version=0, note=[1])
        missing_all = {"tmdb:10001": {1: NotExistMediaInfo(season=1, episodes=[], total_episode=5, start_episode=1)}}
        missing_some = {
            "tmdb:10001": {1: NotExistMediaInfo(season=1, episodes=[2, 4], total_episode=5, start_episode=1)}
        }

        assert self.SubscribeChain.compute_lack_episode(subscribe, no_exists={}) == 0
        assert self.SubscribeChain.compute_lack_episode(subscribe, no_exists={"other": {}}) == 0
        assert self.SubscribeChain.compute_lack_episode(subscribe, no_exists=missing_all) == 5
        assert self.SubscribeChain.compute_lack_episode(subscribe, no_exists=missing_some) == 2

    def test_compute_lack_episode_defaults_empty_no_exists_for_normal_tv(self):
        subscribe = self._build_subscribe(best_version=0, note=[1])

        assert self.SubscribeChain.compute_lack_episode(subscribe) == 0

    def test_note_only_backfill_does_not_satisfy_best_version_quality_target(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            note=[1],
            episode_priority={},
            lack_episode=2,
        )

        assert self.SubscribeChain.compute_lack_episode(subscribe) == 2
        assert self.SubscribeChain._get_pending_best_version_episodes(subscribe) == [1, 2, 3]

    def test_backfill_existing_episodes_writes_note_only_without_priority(self):
        subscribe = self._build_subscribe(note=[1], episode_priority={"2": 80}, lack_episode=4)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            summary = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [1, 2, 3, 9, "bad"],
                priority=None,
                scene="unit",
            )

        assert summary["accepted"] == [2, 3]
        assert summary["ignored"] == [
            {"episode": 1, "reason": "duplicate"},
            {"episode": 9, "reason": "out_of_range"},
            {"episode": "bad", "reason": "invalid"},
        ]
        assert summary["subscribe"].note == [1, 2, 3]
        assert summary["subscribe"].episode_priority == {"2": 80}
        assert summary["subscribe"].lack_episode == 2
        assert updates[-1][1]["lack_episode"] == 2

    def test_backfill_existing_episodes_writes_priority_only_upwards(self):
        subscribe = self._build_subscribe(note=[], episode_priority={"1": 90, "2": 100}, lack_episode=5)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            summary = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [1, 2, 3],
                priority=100,
                scene="unit",
            )

        assert summary["accepted"] == [1, 2, 3]
        assert summary["subscribe"].note == [1, 2, 3]
        assert summary["subscribe"].episode_priority == {"1": 100, "2": 100, "3": 100}
        assert summary["subscribe"].current_priority == 0
        assert updates[-1]["current_priority"] == 0

    def test_backfill_existing_episodes_ignores_invalid_priority_and_does_not_downgrade(self):
        subscribe = self._build_subscribe(note=[], episode_priority={"1": 90}, lack_episode=5)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            invalid = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [1, 2],
                priority=101,
                scene="unit",
            )
            subscribe = invalid["subscribe"]
            lower = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [1, 2],
                priority=80,
                scene="unit",
            )
            subscribe = lower["subscribe"]
            boolean_priority = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [3],
                priority=True,
                scene="unit",
            )
            summary = boolean_priority

        assert invalid["accepted"] == [1, 2]
        assert invalid["ignored_priority"] == 101
        assert lower["accepted"] == []
        assert lower["ignored"] == [
            {"episode": 1, "reason": "duplicate"},
            {"episode": 2, "reason": "duplicate"},
        ]
        assert lower["priority_ignored"] == [
            {"episode": 1, "reason": "not_higher_priority"},
        ]
        assert lower["priority_updated"] == [2]
        assert boolean_priority["accepted"] == [3]
        assert boolean_priority["ignored_priority"] is True
        assert summary["subscribe"].note == [1, 2, 3]
        assert summary["subscribe"].episode_priority == {"1": 90, "2": 80}

    def test_backfill_existing_episodes_accepts_note_without_downgrading_priority(self):
        subscribe = self._build_subscribe(note=[], episode_priority={"1": 90}, lack_episode=5)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            summary = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [1],
                priority=80,
                scene="unit",
            )

        assert summary["accepted"] == [1]
        assert summary["priority_updated"] == []
        assert summary["subscribe"].note == [1]
        assert summary["subscribe"].episode_priority == {"1": 90}
        assert "episode_priority" not in updates[-1]

    def test_backfill_existing_episodes_updates_priority_for_existing_note(self):
        subscribe = self._build_subscribe(note=[1], episode_priority={}, lack_episode=4)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            summary = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [1],
                priority=100,
                scene="unit",
            )

        assert summary["accepted"] == []
        assert summary["ignored"] == [{"episode": 1, "reason": "duplicate"}]
        assert summary["priority_updated"] == [1]
        assert summary["subscribe"].note == [1]
        assert summary["subscribe"].episode_priority == {"1": 100}
        assert updates[-1]["episode_priority"] == {"1": 100}

    def test_backfill_existing_episodes_marks_current_priority_complete_only_when_all_targets_are_top(self):
        subscribe = self._build_subscribe(note=[], episode_priority={"1": 90}, lack_episode=5)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            summary = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [1, 2, 3, 4, 5],
                priority=100,
                scene="unit",
            )

        assert summary["accepted"] == [1, 2, 3, 4, 5]
        assert summary["subscribe"].current_priority == 100
        assert updates[-1]["current_priority"] == 100

    def test_backfill_materializes_legacy_current_priority_before_partial_write(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            current_priority=80,
            episode_priority=None,
            note=[],
            lack_episode=0,
        )
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            summary = self.SubscribeChain().backfill_existing_episodes(
                subscribe,
                [3],
                priority=100,
                scene="unit",
            )

        assert summary["subscribe"].episode_priority == {"1": 80, "2": 80, "3": 100}
        assert summary["subscribe"].note == [3]
        assert summary["subscribe"].current_priority == 80
        assert updates[-1]["episode_priority"] == {"1": 80, "2": 80, "3": 100}

    def test_backfill_existing_episodes_refreshes_normal_tv_with_public_progress_entrypoint(self):
        subscribe = self._build_subscribe(best_version=0, note=[], lack_episode=5)
        progress_calls = []
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        chain = self.SubscribeChain()
        chain.subscription_repository = _SubscribeOper()
        with (
            patch.object(
                chain,
                "refresh_subscribe_progress",
                return_value={
                    "scene": "unit",
                    "updated": True,
                    "fields": ["lack_episode"],
                    "lack_episode": 4,
                    "reason": "updated",
                },
            ) as refresh_progress,
        ):
            summary = chain.backfill_existing_episodes(
                subscribe,
                [1],
                priority=None,
                scene="unit",
            )
            progress_calls.append(refresh_progress.call_args)

        refresh_progress.assert_called_once()
        assert refresh_progress.call_args.args[0].note == [1]
        assert refresh_progress.call_args.kwargs == {"scene": "unit"}
        assert summary["accepted"] == [1]
        assert summary["progress"]["fields"] == ["lack_episode"]
        assert "lack_episode" not in updates[0]
        assert summary["subscribe"].note == [1]
        assert progress_calls

    def test_refresh_subscribe_progress_lowers_current_priority_for_partial_historical_episode_priority(self):
        subscribe = self._build_subscribe(
            total_episode=3,
            current_priority=80,
            episode_priority={"1": 100},
            lack_episode=0,
        )
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            summary = self.SubscribeChain()._SubscribeChain__refresh_subscribe_progress_with_no_exists(
                subscribe=subscribe,
                no_exists={},
                scene="unit",
            )

        assert summary["updated"]
        assert summary["subscribe"].current_priority == 0
        assert updates[-1]["current_priority"] == 0

    def test_refresh_full_best_version_progress_preserves_baseline_priority(self):
        """全集洗版刷新缺集进度时，不从按集事实覆盖整包准入基线。"""
        subscribe = self._build_subscribe(
            best_version_full=1,
            total_episode=3,
            current_priority=82,
            episode_priority={"1": 100},
            lack_episode=0,
        )
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            self.SubscribeChain()._SubscribeChain__refresh_subscribe_progress_with_no_exists(
                subscribe=subscribe,
                no_exists={},
                scene="unit",
            )

        assert subscribe.current_priority == 82
        assert "current_priority" not in updates[-1]

    def test_refresh_subscribe_progress_normal_tv_uses_resolve_missing_successfully(self):
        subscribe = self._build_subscribe(best_version=0, lack_episode=5)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            media_source="themoviedb",
            media_id="10001",
            title_year="测试剧 (2026)",
            seasons={1: [1, 2, 3, 4, 5]},
        )
        no_exists = {"tmdb:10001": {1: NotExistMediaInfo(season=1, episodes=[2, 4], total_episode=5, start_episode=1)}}
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with (
            patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            _patch_media_recognize(self.module, mediainfo),
            patch.object(
                self.SubscribeChain, "resolve_subscribe_missing", return_value=(False, no_exists)
            ) as resolve_missing,
        ):
            summary = self.SubscribeChain().refresh_subscribe_progress(subscribe, scene="unit")

        resolve_missing.assert_called_once()
        _, kwargs = resolve_missing.call_args
        assert kwargs["subscribe"] is subscribe
        assert kwargs["meta"] is not None
        assert kwargs["meta"].type == MediaType.TV
        assert kwargs["meta"].name == subscribe.name
        assert kwargs["meta"].season_seq == "1"
        assert kwargs["mediainfo"] is mediainfo
        assert kwargs["mediakey"] == "tmdb:10001"
        assert summary["updated"]
        assert summary["lack_episode"] == 2
        assert summary["subscribe"].lack_episode == 2
        assert updates[-1]["lack_episode"] == 2

    def test_refresh_subscribe_progress_normal_tv_resolve_failure_does_not_write_zero(self):
        subscribe = self._build_subscribe(best_version=0, lack_episode=5)
        mediainfo = SimpleNamespace(
            type=MediaType.TV,
            tmdb_id=10001,
            douban_id=None,
            title_year="测试剧 (2026)",
            seasons={1: [1, 2, 3, 4, 5]},
        )

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                raise AssertionError("resolve failure must not write progress")

        with (
            patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            _patch_media_recognize(self.module, mediainfo),
            patch.object(self.SubscribeChain, "resolve_subscribe_missing", return_value=(False, {})),
        ):
            summary = self.SubscribeChain().refresh_subscribe_progress(subscribe, scene="unit")

        assert not (summary["updated"])
        assert "reason" in summary
        assert subscribe.lack_episode == 5

    def test_refresh_subscribe_progress_normal_tv_recognition_failure_does_not_write_zero(self):
        subscribe = self._build_subscribe(best_version=0, lack_episode=5)

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                raise AssertionError("recognition failure must not write progress")

        with (
            patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            _patch_media_recognize(self.module, None),
        ):
            summary = self.SubscribeChain().refresh_subscribe_progress(subscribe, scene="unit")

        assert not (summary["updated"])
        assert "reason" in summary
        assert subscribe.lack_episode == 5

    def test_refresh_subscribe_progress_rejects_raw_no_exists_for_public_signature(self):
        subscribe = self._build_subscribe(best_version=0, lack_episode=5)

        with pytest.raises(TypeError):
            self.SubscribeChain().refresh_subscribe_progress(subscribe, no_exists={})

    def test_finish_subscribe_progress_writer_keeps_empty_lefts_as_zero_for_normal_tv(self):
        subscribe = self._build_subscribe(best_version=0, lack_episode=5)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with (
            patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(self.SubscribeChain, "_SubscribeChain__finish_subscribe"),
        ):
            self.SubscribeChain().finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.TV),
                mediainfo=SimpleNamespace(title_year="测试剧 (2026)"),
                downloads=None,
                lefts=None,
            )

        assert updates[-1]["lack_episode"] == 0


class TestSubscribeProgressConsolidation:
    def _mediainfo(self, total_episode=5):
        return SimpleNamespace(
            type=MediaType.TV,
            seasons={1: [object() for _ in range(total_episode)]},
            title="总集增长剧",
            title_year="总集增长剧 (2026)",
            year="2026",
            media_source="themoviedb",
            media_id="31000",
            vote_average=9.5,
            overview="overview",
            imdb_id="tt1234567",
            tvdb_id=99,
            get_poster_image=lambda: "poster",
            get_backdrop_image=lambda: "backdrop",
            get_message_image=lambda: "message-image",
            to_dict=lambda: {},
        )

    @staticmethod
    def _event_manager(total_episode=None, *, updated=True):
        captured = []

        def _apply(event_type, event_data):
            captured.append((event_type, event_data))
            if hasattr(event_data, "current_total_episode"):
                event_data.updated = updated
                event_data.total_episode = total_episode
                event_data.source = "unit"
                event_data.reason = "unit"
            return SimpleNamespace(event_data=event_data)

        class _EventManager:
            def send_event(self, event_type, event_data):
                return _apply(event_type, event_data)

            async def async_send_event(self, event_type, event_data):
                return _apply(event_type, event_data)

        return _EventManager(), captured

    def test_apply_episodes_refresh_clamps_external_total_to_current_total(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        eventmanager, captured = self._event_manager(5)

        with patch.object(module, "eventmanager", eventmanager):
            result = SubscribeChain._SubscribeChain__apply_episodes_refresh(
                10,
                season=1,
                mediainfo=self._mediainfo(total_episode=10),
                media_source="themoviedb",
                media_id="31030",
                subscribe_id=31,
                scene="precheck",
            )

        assert result == 10
        assert captured[0][1].current_total_episode == 10
        assert captured[0][1].total_episode == 10

    def test_async_apply_episodes_refresh_clamps_external_total_to_current_total(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        eventmanager, captured = self._event_manager(5)

        with patch.object(module, "eventmanager", eventmanager):
            result = asyncio.run(
                SubscribeChain._SubscribeChain__async_apply_episodes_refresh(
                    10,
                    season=1,
                    mediainfo=self._mediainfo(total_episode=10),
                    media_source="themoviedb",
                    media_id="31030",
                    subscribe_id=31,
                    scene="precheck",
                )
            )

        assert result == 10
        assert captured[0][1].current_total_episode == 10
        assert captured[0][1].total_episode == 10

    def test_refresh_total_episode_before_completion_reuses_progress_priority_snapshot(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=31,
            name="总集增长剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=3,
            start_episode=1,
            lack_episode=0,
            best_version=1,
            best_version_full=0,
            current_priority=80,
            episode_priority=None,
            note=[],
            media_source="themoviedb",
            media_id="31031",
            manual_total_episode=0,
        )
        mediainfo = self._mediainfo(total_episode=5)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        with patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            updated = SubscribeChain()._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                mediainfo,
            )

        assert updated.total_episode == 5
        assert updated.episode_priority == {"1": 80, "2": 80, "3": 80}
        assert updated.lack_episode == 2
        assert updated.current_priority == 0
        assert updates[-1][1]["lack_episode"] == 2
        assert updates[-1][1]["current_priority"] == 0

    def test_refresh_total_episode_before_completion_keeps_downloaded_best_version_floor(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=34,
            name="总集回落剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=100,
            best_version=1,
            best_version_full=0,
            current_priority=None,
            episode_priority={str(episode): 80 for episode in range(1, 101)},
            note=[],
            media_source="themoviedb",
            media_id="31034",
            manual_total_episode=0,
        )
        updates = []
        eventmanager, captured = self._event_manager(updated=False)

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        chain = SubscribeChain()
        resolve_calls = []

        def _resolve_missing(**kwargs):
            resolve_calls.append(kwargs)
            return True, {}

        chain.resolve_subscribe_missing = _resolve_missing

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
        ):
            updated = chain._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                self._mediainfo(total_episode=1),
                meta=SimpleNamespace(type=MediaType.TV, begin_season=1, season=1),
                mediakey=31034,
            )

        assert captured[0][1].current_total_episode == 1
        assert updated.total_episode == 100
        assert updates == []
        assert resolve_calls[0]["best_version_accept_downloaded"]

    def test_refresh_total_episode_before_completion_filters_best_version_priority_on_decrease(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=40,
            name="洗版回落剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=0,
            best_version=1,
            best_version_full=0,
            current_priority=100,
            episode_priority={str(episode): 100 for episode in range(1, 101)},
            note=[],
            media_source="themoviedb",
            media_id="31040",
            manual_total_episode=0,
        )
        updates = []
        eventmanager, _ = self._event_manager(updated=False)

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
        ):
            updated = SubscribeChain()._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                self._mediainfo(total_episode=10),
            )

        expected_priority = {str(episode): 100 for episode in range(1, 11)}
        payload = updates[-1][1]
        assert updated.total_episode == 10
        assert updated.episode_priority == expected_priority
        assert payload["episode_priority"] == expected_priority
        assert updated.lack_episode == 0
        assert updated.current_priority == 100
        assert SubscribeChain._SubscribeChain__get_best_version_completed_episodes(updated) == list(range(1, 11))

    def test_full_best_version_total_expansion_resets_baseline_priority(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=41,
            name="全集洗版扩展剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=3,
            start_episode=1,
            lack_episode=0,
            best_version=1,
            best_version_full=1,
            current_priority=82,
            episode_priority={"1": 90, "2": 80, "3": 82},
            note=[1, 2, 3],
            media_source="themoviedb",
            media_id="31041",
            manual_total_episode=0,
        )

        payload = SubscribeChain._SubscribeChain__prepare_best_version_total_change_fields(
            subscribe=subscribe,
            total_episode=5,
            old_total_episode=3,
        )

        assert payload["current_priority"] == 0
        assert payload["episode_priority"] == {"1": 90, "2": 80, "3": 82}

    def test_full_best_version_total_expansion_does_not_derive_episode_map_from_baseline(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=45,
            name="全集洗版无按集事实扩展剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=3,
            start_episode=1,
            lack_episode=0,
            best_version=1,
            best_version_full=1,
            current_priority=82,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="31045",
            manual_total_episode=0,
        )

        payload = SubscribeChain._SubscribeChain__prepare_best_version_total_expansion_fields(
            subscribe=subscribe,
            total_episode=5,
        )

        assert payload["current_priority"] == 0
        assert payload["episode_priority"] == {}

    def test_full_best_version_total_shrink_preserves_baseline_priority(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=43,
            name="全集洗版缩小剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=5,
            start_episode=1,
            lack_episode=0,
            best_version=1,
            best_version_full=1,
            current_priority=82,
            episode_priority={"1": 90, "2": 80, "3": 82, "4": 70, "5": 60},
            note=[1, 2, 3, 4, 5],
            media_source="themoviedb",
            media_id="31043",
            manual_total_episode=0,
        )

        payload = SubscribeChain._SubscribeChain__prepare_best_version_total_change_fields(
            subscribe=subscribe,
            total_episode=3,
            old_total_episode=5,
        )

        assert payload["current_priority"] == 82
        assert payload["episode_priority"] == {"1": 90, "2": 80, "3": 82}

    def test_refresh_total_episode_before_completion_resets_legacy_current_priority_when_filtered_empty(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=42,
            name="洗版空优先级回落剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=0,
            best_version=1,
            best_version_full=0,
            current_priority=100,
            episode_priority={str(episode): 100 for episode in range(11, 101)},
            note=[],
            media_source="themoviedb",
            media_id="31042",
            manual_total_episode=0,
        )
        updates = []
        eventmanager, _ = self._event_manager(updated=False)

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
        ):
            updated = SubscribeChain()._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                self._mediainfo(total_episode=10),
            )

        payload = updates[-1][1]
        assert updated.total_episode == 10
        assert updated.episode_priority == {}
        assert updated.current_priority == 0
        assert updated.lack_episode == 10
        assert payload["episode_priority"] == {}
        assert payload["current_priority"] == 0
        assert payload["lack_episode"] == 10
        assert SubscribeChain._SubscribeChain__get_best_version_completed_episodes(updated) == []

    def test_refresh_total_episode_before_completion_resets_priority_when_target_range_empty(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=44,
            name="洗版目标范围为空回落剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=11,
            lack_episode=0,
            best_version=1,
            best_version_full=0,
            current_priority=100,
            episode_priority={str(episode): 100 for episode in range(11, 101)},
            note=[],
            media_source="themoviedb",
            media_id="31044",
            manual_total_episode=0,
        )
        updates = []
        eventmanager, _ = self._event_manager(updated=False)

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
        ):
            updated = SubscribeChain()._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                self._mediainfo(total_episode=10),
            )

        payload = updates[-1][1]
        assert updated.total_episode == 10
        assert updated.episode_priority == {}
        assert updated.current_priority == 0
        assert updated.lack_episode == 0
        assert payload["episode_priority"] == {}
        assert payload["current_priority"] == 0
        assert payload["lack_episode"] == 0
        assert SubscribeChain._SubscribeChain__get_best_version_completed_episodes(updated) == []

    def test_refresh_total_episode_before_completion_clamps_lower_event_total_to_recognized_total(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=35,
            name="总集事件压低剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=100,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="31035",
            manual_total_episode=0,
        )
        updates = []
        eventmanager, _ = self._event_manager(9)

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
        ):
            updated = SubscribeChain()._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                self._mediainfo(total_episode=10),
            )

        assert updated.total_episode == 10
        assert updated.lack_episode == 10
        assert updates[-1][1]["total_episode"] == 10
        assert updates[-1][1]["lack_episode"] == 10

    def test_refresh_total_episode_before_completion_rejects_manual_total_decrease(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=37,
            name="手动总集数剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=100,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="31037",
            manual_total_episode=1,
        )

        class _EventManager:
            def send_event(self, *_args, **_kwargs):
                raise AssertionError("manual total episode must not ask external refresh")

        class _SubscribeOper:
            def update(self, *_args, **_kwargs):
                raise AssertionError("manual total episode must not be updated")

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                module,
                "eventmanager",
                _EventManager(),
            ),
        ):
            updated = SubscribeChain()._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                self._mediainfo(total_episode=10),
            )

        assert updated.total_episode == 100
        assert updated.lack_episode == 100

    def test_refresh_total_episode_before_completion_rejects_non_tv_decrease(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=38,
            name="非电视剧",
            type=MediaType.MOVIE.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=100,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="31038",
            manual_total_episode=0,
        )

        class _EventManager:
            def send_event(self, *_args, **_kwargs):
                raise AssertionError("non-tv subscribe must not ask external refresh")

        class _SubscribeOper:
            def update(self, *_args, **_kwargs):
                raise AssertionError("non-tv subscribe must not be updated")

        with (
            patch.object(SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(
                module,
                "eventmanager",
                _EventManager(),
            ),
        ):
            updated = SubscribeChain()._SubscribeChain__refresh_total_episode_before_completion(
                subscribe,
                self._mediainfo(total_episode=10),
            )

        assert updated.total_episode == 100
        assert updated.lack_episode == 100

    def test_check_total_growth_reuses_progress_priority_snapshot(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=33,
            name="总集增长剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=3,
            start_episode=1,
            lack_episode=0,
            best_version=1,
            best_version_full=0,
            current_priority=80,
            episode_priority=None,
            note=[],
            year="2026",
            episode_group=None,
            media_source="themoviedb",
            media_id="31033",
            manual_total_episode=0,
        )
        updates = []

        class _SubscribeOper:
            def list(self):
                return [subscribe]

            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        chain = SubscribeChain()
        chain.subscription_repository = _SubscribeOper()

        with (
            _patch_media_recognize(module, lambda **_kwargs: self._mediainfo(total_episode=5)),
        ):
            chain.check()

        payload = updates[-1][1]
        assert payload["total_episode"] == 5
        assert payload["episode_priority"] == {"1": 80, "2": 80, "3": 80}
        assert payload["lack_episode"] == 2
        assert payload["current_priority"] == 0

    def test_check_total_growth_still_uses_larger_event_total(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=39,
            name="总集事件增长剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=100,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            year="2026",
            episode_group=None,
            media_source="themoviedb",
            media_id="31039",
            manual_total_episode=0,
        )
        updates = []
        eventmanager, captured = self._event_manager(120)

        class _SubscribeOper:
            def list(self):
                return [subscribe]

            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        chain = SubscribeChain()
        chain.subscription_repository = _SubscribeOper()

        with (
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
            _patch_media_recognize(module, lambda **_kwargs: self._mediainfo(total_episode=10)),
        ):
            chain.check()

        payload = updates[-1][1]
        assert captured[0][1].current_total_episode == 10
        assert payload["total_episode"] == 120
        assert payload["lack_episode"] == 120

    def test_check_total_refresh_uses_confirmed_episode_floor(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=43,
            name="总集巡检回落剧",
            type=MediaType.TV.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=100,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            year="2026",
            episode_group=None,
            media_source="themoviedb",
            media_id="31043",
            manual_total_episode=0,
        )
        updates = []
        eventmanager, captured = self._event_manager(updated=False)

        class _SubscribeOper:
            def list(self):
                return [subscribe]

            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        chain = SubscribeChain()
        chain.subscription_repository = _SubscribeOper()
        chain.resolve_subscribe_missing = lambda **kwargs: (
            False,
            {
                "tmdb:31043": {
                    1: SimpleNamespace(
                        season=1,
                        episodes=list(range(91, 101)),
                        total_episode=100,
                        start_episode=1,
                        require_complete_coverage=False,
                    )
                }
            },
        )

        with (
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
            _patch_media_recognize(module, lambda **_kwargs: self._mediainfo(total_episode=1)),
        ):
            chain.check()

        payload = updates[-1][1]
        assert captured[0][1].current_total_episode == 1
        assert payload["total_episode"] == 90
        assert payload["lack_episode"] == 90

    def test_check_total_refresh_skips_non_tv_even_when_mediainfo_has_seasons(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        subscribe = module.SubscriptionSnapshot(
            id=45,
            name="电影误带季集",
            type=MediaType.MOVIE.value,
            season=1,
            total_episode=100,
            start_episode=1,
            lack_episode=100,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            year="2026",
            episode_group=None,
            media_source="themoviedb",
            media_id="31045",
            manual_total_episode=0,
        )
        updates = []
        mediainfo = self._mediainfo(total_episode=10)
        mediainfo.type = MediaType.MOVIE

        class _SubscribeOper:
            def list(self):
                return [subscribe]

            def update(self, subscribe_id, payload):
                updates.append((subscribe_id, payload.to_payload()))
                return replace(subscribe, **payload.to_payload())

        class _EventManager:
            def send_event(self, *_args, **_kwargs):
                raise AssertionError("non-tv subscribe must not ask external refresh")

        chain = SubscribeChain()
        chain.subscription_repository = _SubscribeOper()

        with (
            patch.object(
                module,
                "eventmanager",
                _EventManager(),
            ),
            _patch_media_recognize(module, mediainfo),
        ):
            chain.check()

        assert updates[-1][1]["total_episode"] == 100
        assert updates[-1][1]["lack_episode"] == 100

    def test_add_create_clamps_event_decrease_to_recognized_total(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        added = []
        eventmanager, captured = self._event_manager(5)
        mediainfo = self._mediainfo(total_episode=10)
        chain = SubscribeChain()
        chain.obtain_images = lambda **_kwargs: None

        # 落库入口已迁到 application/subscription/write.py，链路层的接缝是 add_subscribe；
        # 截在这里拿到的就是链路交给写入路径的原始字段，正是本用例要断言的总集数
        def _add_subscribe(**kwargs):
            added.append(kwargs)
            return 41, None

        with (
            patch.object(module, "add_subscribe", _add_subscribe),
            patch.object(
                module,
                "eventmanager",
                eventmanager,
            ),
            _patch_media_recognize(module, mediainfo),
        ):
            sid, err_msg = chain.add(
                title="总集创建剧",
                year="2026",
                mtype=MediaType.TV,
                media_source="themoviedb",
                media_id="31041",
                season=1,
                message=False,
            )

        assert sid == 41
        assert err_msg is None
        assert captured[0][1].scene == "create"
        assert captured[0][1].current_total_episode == 10
        assert added[-1]["total_episode"] == 10
        assert added[-1]["lack_episode"] == 10

    def test_completed_episode_uses_schema_function_directly_for_best_version(self):
        module, SubscribeChain = _load_subscribe_chain_class()
        values = {
            "id": 32,
            "name": "完成集数剧",
            "type": MediaType.TV.value,
            "season": 1,
            "total_episode": 8,
            "start_episode": 3,
            "lack_episode": 2,
            "best_version": 1,
            "episode_priority": {"3": 100, "4": 80, "5": 100, "8": 100},
        }
        chain_subscribe = module.SubscriptionSnapshot(**values)
        schema_subscribe = schemas.Subscribe(**values)

        assert not (hasattr(SubscribeChain, "compute_completed_episode"))
        assert schema_subscribe.completed_episode == schemas.compute_subscribe_completed_episode(chain_subscribe)

    def test_completed_episode_uses_current_priority_when_episode_priority_empty(self):
        module, _ = _load_subscribe_chain_class()
        values = {
            "id": 33,
            "name": "完成集数旧快照剧",
            "type": MediaType.TV.value,
            "season": 1,
            "total_episode": 3,
            "start_episode": 1,
            "lack_episode": 0,
            "best_version": 1,
            "current_priority": 100,
            "episode_priority": None,
        }

        chain_subscribe = module.SubscriptionSnapshot(**values)
        schema_subscribe = schemas.Subscribe(**values)

        assert schema_subscribe.completed_episode == 3
        assert schemas.compute_subscribe_completed_episode(chain_subscribe) == 3

    def test_full_best_version_completed_episode_uses_baseline_not_episode_map(self):
        data = {
            "name": "全集洗版响应剧",
            "type": MediaType.TV.value,
            "best_version": 1,
            "best_version_full": 1,
            "start_episode": 1,
            "total_episode": 3,
            "current_priority": 82,
            "episode_priority": {"1": 100, "2": 100, "3": 100},
        }

        assert schemas.compute_subscribe_completed_episode(SimpleNamespace(**data)) == 0
        data["current_priority"] = 100
        data["episode_priority"] = {"1": 80}
        assert schemas.compute_subscribe_completed_episode(SimpleNamespace(**data)) == 3


class TestSubscribeDownloadFacts:
    def setup_method(self):
        self.module, self.SubscribeChain = _load_subscribe_chain_class()

    def _build_subscribe(self, **overrides):
        values = {
            "id": 3,
            "name": "下载事实剧",
            "type": MediaType.TV.value,
            "season": 1,
            "start_episode": 1,
            "total_episode": 4,
            "lack_episode": 4,
            "note": [],
            "best_version": 0,
            "best_version_full": 0,
            "current_priority": None,
            "episode_priority": {},
            "media_source": "themoviedb",
            "media_id": "30003",
            "manual_total_episode": 0,
        }
        values.update(overrides)
        return self.module.SubscriptionSnapshot(**values)

    def _download(self, episodes=None, pri_order=80, selected_episodes=None, confirmed_full_coverage=False):
        return SimpleNamespace(
            selected_episodes=selected_episodes,
            confirmed_full_coverage=confirmed_full_coverage,
            torrent_info=SimpleNamespace(pri_order=pri_order),
            meta_info=SimpleNamespace(episode_list=episodes or [], season_list=[1]),
            media_info=SimpleNamespace(
                type=MediaType.TV,
                media_source="themoviedb",
                media_id="30003",
            ),
        )

    def test_normal_tv_download_records_note_and_episode_priority_without_current_priority(self):
        subscribe = self._build_subscribe(best_version=0)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            snapshot = self.SubscribeChain()._SubscribeChain__record_subscribe_download_facts(
                subscribe,
                mediainfo=SimpleNamespace(title_year="下载事实剧 (2026)"),
                downloads=[self._download(episodes=[1, 2], pri_order=70)],
            )

        assert snapshot["episodes"] == [1, 2]
        assert snapshot["subscribe"].note == [1, 2]
        assert snapshot["subscribe"].episode_priority == {"1": 70, "2": 70}
        assert snapshot["subscribe"].current_priority is None
        assert "current_priority" not in updates[-1]

    def test_normal_tv_download_records_full_pack_confirmed_coverage_episode_priority(self):
        subscribe = self._build_subscribe(best_version=0, best_version_full=0, total_episode=3, episode_priority={})
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            snapshot = self.SubscribeChain()._SubscribeChain__record_subscribe_download_facts(
                subscribe,
                mediainfo=SimpleNamespace(title_year="下载事实剧 (2026)"),
                downloads=[
                    self._download(
                        episodes=[],
                        pri_order=80,
                        selected_episodes=[],
                        confirmed_full_coverage=True,
                    )
                ],
            )

        assert snapshot["episodes"] == [1, 2, 3]
        assert snapshot["subscribe"].note == [1, 2, 3]
        assert snapshot["subscribe"].episode_priority == {"1": 80, "2": 80, "3": 80}
        assert "current_priority" not in updates[-1]

    def test_full_resource_without_episode_list_does_not_fallback_without_download_confirmation(self):
        subscribe = self._build_subscribe(best_version=1, best_version_full=1, episode_priority={"1": 60})
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            snapshot = self.SubscribeChain()._SubscribeChain__record_subscribe_download_facts(
                subscribe,
                mediainfo=SimpleNamespace(title_year="下载事实剧 (2026)"),
                downloads=[self._download(episodes=[], pri_order=90, selected_episodes=[])],
            )

        assert snapshot["episodes"] == []
        assert subscribe.note == []
        assert subscribe.episode_priority == {"1": 60}
        assert updates == []

    def test_full_resource_without_episode_list_uses_target_range_only_when_confirmed(self):
        subscribe = self._build_subscribe(best_version=1, best_version_full=1, episode_priority={"1": 60})
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            snapshot = self.SubscribeChain()._SubscribeChain__record_subscribe_download_facts(
                subscribe,
                mediainfo=SimpleNamespace(title_year="下载事实剧 (2026)"),
                downloads=[
                    self._download(
                        episodes=[],
                        pri_order=90,
                        selected_episodes=[],
                        confirmed_full_coverage=True,
                    )
                ],
            )

        assert snapshot["episodes"] == [1, 2, 3, 4]
        assert snapshot["subscribe"].note == [1, 2, 3, 4]
        assert snapshot["subscribe"].episode_priority == {"1": 90, "2": 90, "3": 90, "4": 90}
        assert snapshot["subscribe"].current_priority == 90
        assert updates[-1]["current_priority"] == 90

    def test_full_best_version_does_not_write_baseline_without_confirmed_full_coverage(self):
        subscribe = self._build_subscribe(
            best_version=1,
            best_version_full=1,
            current_priority=82,
            episode_priority={"1": 60},
        )

        with patch.object(self.SubscribeChain, "subscription_repository", create=True) as subscribe_oper:
            self.SubscribeChain()._SubscribeChain__record_subscribe_download_facts(
                subscribe,
                mediainfo=SimpleNamespace(title_year="下载事实剧 (2026)"),
                downloads=[self._download(episodes=[1], pri_order=90, confirmed_full_coverage=False)],
            )

        payload = subscribe_oper.update.call_args.args[1].to_payload()
        assert subscribe.current_priority == 82
        assert "current_priority" not in payload

    def test_normal_subscription_without_episode_list_does_not_use_target_range_without_download_confirmation(self):
        subscribe = self._build_subscribe(best_version=0, best_version_full=0)
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True):
            snapshot = self.SubscribeChain()._SubscribeChain__record_subscribe_download_facts(
                subscribe,
                mediainfo=SimpleNamespace(title_year="下载事实剧 (2026)"),
                downloads=[
                    self._download(
                        episodes=[],
                        pri_order=90,
                        selected_episodes=[],
                        confirmed_full_coverage=False,
                    )
                ],
            )

        assert snapshot["episodes"] == []
        assert subscribe.note == []
        assert subscribe.episode_priority == {}
        assert updates == []

    def test_movie_best_version_download_keeps_current_priority_without_episode_priority(self):
        subscribe = self._build_subscribe(
            type=MediaType.MOVIE.value,
            best_version=1,
            best_version_full=0,
            current_priority=60,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="30003",
            total_episode=1,
            lack_episode=1,
        )
        download = self._download(episodes=[], pri_order=90)
        download.media_info = SimpleNamespace(
            type=MediaType.MOVIE,
            tmdb_id=30003,
            douban_id=None,
            bangumi_id=None,
            anilist_id=None,
        )
        download.meta_info = SimpleNamespace(episode_list=[], season_list=[])
        updates = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        with (
            patch.object(self.SubscribeChain, "subscription_repository", _SubscribeOper(), create=True),
            patch.object(self.SubscribeChain, "_SubscribeChain__finish_subscribe"),
        ):
            self.SubscribeChain().finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.MOVIE),
                mediainfo=SimpleNamespace(title_year="下载事实电影 (2026)"),
                downloads=[download],
                lefts={},
            )

        assert updates[-1]["current_priority"] == 90
        assert updates[-1]["last_update"]
        assert subscribe.last_update is None
        assert subscribe.episode_priority == {}

    def test_movie_best_version_download_does_not_call_tv_progress_writer(self):
        subscribe = self._build_subscribe(
            type=MediaType.MOVIE.value,
            best_version=1,
            best_version_full=0,
            current_priority=60,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="30003",
            total_episode=1,
            lack_episode=1,
        )
        download = self._download(episodes=[], pri_order=90)
        download.media_info = SimpleNamespace(
            type=MediaType.MOVIE,
            tmdb_id=30003,
            douban_id=None,
            bangumi_id=None,
            anilist_id=None,
        )
        download.meta_info = SimpleNamespace(episode_list=[], season_list=[])
        chain = self.SubscribeChain()
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper

        with (
            patch.object(chain, "_SubscribeChain__refresh_subscribe_progress_with_no_exists") as refresh_mock,
            patch.object(chain, "_SubscribeChain__finish_subscribe"),
        ):
            subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.MOVIE),
                mediainfo=SimpleNamespace(title_year="下载事实电影 (2026)"),
                downloads=[download],
                lefts={},
            )

        refresh_mock.assert_not_called()

    def test_movie_normal_download_does_not_call_tv_progress_writer(self):
        subscribe = self._build_subscribe(
            type=MediaType.MOVIE.value,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="30003",
            total_episode=1,
            lack_episode=1,
        )
        download = self._download(episodes=[], pri_order=90)
        download.media_info = SimpleNamespace(
            type=MediaType.MOVIE,
            tmdb_id=30003,
            douban_id=None,
            bangumi_id=None,
            anilist_id=None,
        )
        download.meta_info = SimpleNamespace(episode_list=[], season_list=[])
        chain = self.SubscribeChain()
        subscribe_oper = MagicMock()
        chain.subscription_repository = subscribe_oper

        with (
            patch.object(chain, "_SubscribeChain__refresh_subscribe_progress_with_no_exists") as refresh_mock,
            patch.object(chain, "_SubscribeChain__finish_subscribe"),
        ):
            subscribe_oper.update.side_effect = lambda _sid, payload: replace(subscribe, **payload.to_payload())

            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.MOVIE),
                mediainfo=SimpleNamespace(title_year="下载事实电影 (2026)"),
                downloads=[download],
                lefts={},
            )

        refresh_mock.assert_not_called()

    def test_movie_normal_download_records_current_priority_before_completion(self):
        subscribe = self._build_subscribe(
            type=MediaType.MOVIE.value,
            best_version=0,
            best_version_full=0,
            current_priority=None,
            episode_priority={},
            note=[],
            media_source="themoviedb",
            media_id="30003",
            total_episode=1,
            lack_episode=1,
        )
        download = self._download(episodes=[], pri_order=90)
        download.media_info = SimpleNamespace(
            type=MediaType.MOVIE,
            tmdb_id=30003,
            douban_id=None,
            bangumi_id=None,
            anilist_id=None,
        )
        download.meta_info = SimpleNamespace(episode_list=[], season_list=[])
        updates = []
        finished = []

        class _SubscribeOper:
            def update(self, subscribe_id, payload):
                updates.append(payload.to_payload())
                return replace(subscribe, **payload.to_payload())

        chain = self.SubscribeChain()
        chain.subscription_repository = _SubscribeOper()

        def finish_probe(subscribe, **_kwargs):
            finished.append(subscribe.current_priority)

        with (
            patch.object(chain, "_SubscribeChain__finish_subscribe", side_effect=finish_probe),
        ):
            chain.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=SimpleNamespace(type=MediaType.MOVIE),
                mediainfo=SimpleNamespace(title_year="下载事实电影 (2026)"),
                downloads=[download],
                lefts={},
            )

        assert finished == [90]
        assert updates[-1]["current_priority"] == 90
        assert updates[-1]["last_update"]
        assert subscribe.last_update is None


def test_async_add_batch_reuses_prepared_defaults_notifications_and_effects():
    """批量新增把 Chain 准备好的默认字段、通知和原提交后回调逐季交给 writer。"""
    module, subscribe_chain = _load_subscribe_chain_class()
    chain = subscribe_chain()

    def context(season: int):
        """构造已完成识别、季集补齐和默认参数处理的一季上下文。"""
        mediainfo = SimpleNamespace(
            title="批量剧集",
            year="2026",
            type=MediaType.TV,
            media_source=MediaSource.TMDB,
            media_id="batch-chain-1",
            episode_group=None,
            seasons={1: [1], 2: [1, 2]},
            vote_average=8.5,
            overview="批量准备",
            get_poster_image=lambda: "poster.jpg",
            get_backdrop_image=lambda: "backdrop.jpg",
        )
        return module._SubscribeCreateContext(
            title="批量剧集",
            year="2026",
            mtype=MediaType.TV,
            episode_group=None,
            season=season,
            channel=None,
            source=None,
            userid=None,
            username="Seerr",
            message=True,
            exist_ok=False,
            options={
                "media_source": MediaSource.TMDB,
                "media_id": "batch-chain-1",
                "filter": "default-filter",
                "total_episode": season,
                "lack_episode": season,
            },
            explicit_identity=True,
            media_source=MediaSource.TMDB,
            media_id="batch-chain-1",
            requested_music_type=None,
            metainfo=SimpleNamespace(type=MediaType.TV),
            mediainfo=mediainfo,
        )

    prepared = AsyncMock(side_effect=[(context(1), None), (context(2), None)])
    notification = MagicMock(
        side_effect=[{"title": "第 1 季"}, {"title": "第 2 季"}]
    )
    post_commit = AsyncMock(return_value=True)
    setattr(chain, "_SubscribeChain__async_prepare_subscribe_create", prepared)
    setattr(chain, "_SubscribeChain__build_subscribe_notification", notification)
    setattr(chain, "_SubscribeChain__async_post_subscribe_added", post_commit)
    batch_writer = MagicMock()

    async def persist(requests):
        """模拟数据库批量 writer，并在提交点后调用冻结的逐季副作用。"""
        for subscribe_id, request in zip((31, 32), requests):
            assert request.after_commit is not None
            await request.after_commit(subscribe_id)
        return (
            SubscriptionWriteResult(31, "新增订阅成功", True),
            SubscriptionWriteResult(32, "新增订阅成功", True),
        )

    batch_writer.async_add = AsyncMock(side_effect=persist)

    result = asyncio.run(
        chain.async_add_batch(
            title="批量剧集",
            year="2026",
            seasons=[1, 2],
            batch_writer=batch_writer,
            mtype=MediaType.TV,
            media_source=MediaSource.TMDB,
            media_id="batch-chain-1",
            username="Seerr",
        )
    )

    assert result == (32, "新增订阅成功")
    requests = batch_writer.async_add.await_args.args[0]
    assert [request.identity.season for request in requests] == [1, 2]
    assert [request.payload.to_payload()["filter"] for request in requests] == [
        "default-filter",
        "default-filter",
    ]
    assert [dict(request.notification or {}) for request in requests] == [
        {"title": "第 1 季"},
        {"title": "第 2 季"},
    ]
    assert [call.args[0] for call in post_commit.await_args_list] == [31, 32]
