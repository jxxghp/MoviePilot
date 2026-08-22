"""媒体数据源能力面测试：从方法名推导、并入来源列表、且不改变任何分发行为。

一个只做发现的来源和一个做元数据识别的来源过去混在同一张扁平清单里，用户在识别源
里选中前者，调用分发下去无人认领，静默落空。能力面把「这个来源能拿来做什么」变成
来源列表上的一项事实，选择器据此过滤。
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.web.security.access import verify_token
from app.api.endpoints import media as media_endpoint
from app.runtime.extensions.contract.declaration import MediaSourceDeclaration
from app.runtime.extensions.projection.media_source_faces import (
    media_source_capabilities,
    method_capability,
    ordered_capabilities,
)
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.projection.media_source_routing import routes_by_source
from app.runtime.extensions.projection.plugin import PluginProjection
from app.schemas.types import MediaSource, MediaSourceCapability


def _impl(**kwargs):
    """媒体数据源实现桩，回显收到的调用参数。"""
    return kwargs


class _Plugin:
    """交出媒体数据源声明的最小运行态插件替身。"""

    plugin_name = "数据源插件"

    def __init__(self, declarations):
        """保存待交出的媒体数据源声明列表。"""
        self._declarations = declarations

    def get_state(self):
        """始终处于启用状态。"""
        return True

    def get_name(self):
        """返回插件展示名称。"""
        return self.plugin_name

    def provides_media_sources(self):
        """交出预设的媒体数据源声明。"""
        return self._declarations


class _EmptyModuleCatalog:
    """不提供任何宿主模块的空目录。"""

    def get_running_modules(self, _method: str):
        """始终返回空序列。"""
        return []

    def providers_for(self, _method: str):
        """始终返回空序列。"""
        return ()


def _dispatcher(projection: PluginProjection) -> ModuleInvocationDispatcher:
    """构造只接插件来源、不接宿主模块的最小调度器。"""

    class _Catalog:
        """把插件能力投影适配为调度器消费的目录端口。"""

        @staticmethod
        def get_plugin_modules() -> dict:
            """返回当前插件模块方法表快照。"""
            return projection.modules()

    return ModuleInvocationDispatcher(
        module_catalog=_EmptyModuleCatalog(),
        plugin_catalog=_Catalog(),
        plugin_error_handler=lambda *a, **k: None,
        system_error_handler=lambda *a, **k: None,
        rate_limit_handler=lambda *a, **k: None,
    )


def _source_entry(declarations, media_source: str) -> dict:
    """取指定来源标识在插件来源列表里的描述字典。"""
    projection = PluginProjection({"Demo": _Plugin(declarations)})
    return next(
        item
        for item in projection.media_sources()
        if item["media_source"] == media_source
    )


# ---------------------------------------------------------------------------
# 划分：哪些方法构成能力面
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "capability"),
    [
        ("recognize_media", MediaSourceCapability.RECOGNIZE),
        ("match_media", MediaSourceCapability.RECOGNIZE),
        ("search_medias", MediaSourceCapability.SEARCH),
        ("media_detail", MediaSourceCapability.DETAIL),
        ("media_credits", MediaSourceCapability.DETAIL),
        ("person_detail", MediaSourceCapability.DETAIL),
        ("person_credits", MediaSourceCapability.DETAIL),
        ("media_recommend", MediaSourceCapability.RECOMMEND),
        ("media_similar", MediaSourceCapability.RECOMMEND),
        ("discover", MediaSourceCapability.DISCOVER),
        ("discover_board", MediaSourceCapability.DISCOVER),
        ("obtain_images", MediaSourceCapability.SCRAPE),
        ("metadata_nfo", MediaSourceCapability.SCRAPE),
        ("metadata_img", MediaSourceCapability.SCRAPE),
    ],
)
def test_contract_method_belongs_to_its_capability(method, capability) -> None:
    """每个多来源契约方法都归入它所属的那一个能力面。"""
    assert method_capability(method) is capability


@pytest.mark.parametrize(
    "method",
    ["media_exists", "media_files", "storage_manage", "send_message", ""],
)
def test_non_source_scoped_method_constitutes_no_capability(method) -> None:
    """不按来源收窄的方法不构成任何能力面。

    ``media_exists`` 按 server/itemid 收窄，问的是「哪台媒体服务器有」，与媒体
    数据源无关，误算进来会让媒体服务器插件出现在来源选择器里。
    """
    assert method_capability(method) is None


def test_media_exists_never_enters_a_source_capability_set() -> None:
    """只挂 media_exists 的声明推导不出任何能力面。"""
    assert media_source_capabilities(["media_exists", "async_media_exists"]) == ()


# ---------------------------------------------------------------------------
# 推导：作者只写方法，能力面由宿主算
# ---------------------------------------------------------------------------


def test_discover_only_source_stays_out_of_the_recognition_face() -> None:
    """只声明 discover 的来源只占发现面，识别面里没有它。"""
    capabilities = media_source_capabilities(["discover", "discover_board"])

    assert capabilities == (MediaSourceCapability.DISCOVER,)
    assert MediaSourceCapability.RECOGNIZE not in capabilities


def test_source_covering_every_face_appears_in_every_face() -> None:
    """六个面都占的来源在每个面里都出现。"""
    capabilities = media_source_capabilities(
        [
            "recognize_media",
            "search_medias",
            "media_detail",
            "media_recommend",
            "discover",
            "obtain_images",
        ]
    )

    assert set(capabilities) == set(MediaSourceCapability)


def test_async_variant_folds_into_the_same_face_as_its_sync_twin() -> None:
    """async_ 变体去前缀后与同名同步方法归入同一面，不多出一个面。"""
    assert media_source_capabilities(["async_discover"]) == (
        MediaSourceCapability.DISCOVER,
    )
    assert media_source_capabilities(["discover", "async_discover"]) == (
        MediaSourceCapability.DISCOVER,
    )


def test_capability_order_does_not_depend_on_registration_order() -> None:
    """能力面按固定划分顺序列举，与方法表的书写与遍历顺序无关。"""
    forward = media_source_capabilities(
        ["recognize_media", "search_medias", "media_detail", "discover"]
    )
    backward = media_source_capabilities(
        ["discover", "media_detail", "search_medias", "recognize_media"]
    )

    assert forward == backward
    assert forward == (
        MediaSourceCapability.RECOGNIZE,
        MediaSourceCapability.SEARCH,
        MediaSourceCapability.DETAIL,
        MediaSourceCapability.DISCOVER,
    )


def test_ordered_capabilities_deduplicates_and_sorts() -> None:
    """并集去重后仍按固定划分顺序排列。"""
    assert ordered_capabilities(
        [
            MediaSourceCapability.SCRAPE,
            MediaSourceCapability.RECOGNIZE,
            MediaSourceCapability.SCRAPE,
        ]
    ) == (MediaSourceCapability.RECOGNIZE, MediaSourceCapability.SCRAPE)


def test_unknown_method_names_are_ignored() -> None:
    """插件自定义的非契约方法名不构成能力面，也不影响其余推导。"""
    assert media_source_capabilities(["acme_private_call", "discover"]) == (
        MediaSourceCapability.DISCOVER,
    )


# ---------------------------------------------------------------------------
# 来源列表：能力面随声明进入插件来源描述
# ---------------------------------------------------------------------------


def test_declared_source_carries_its_derived_capabilities() -> None:
    """插件声明的来源在来源列表里带上推导出的能力面。"""
    entry = _source_entry(
        [
            MediaSourceDeclaration(
                media_source="acme.video",
                name="Acme Video",
                methods={"discover": _impl, "async_discover_board": _impl},
            )
        ],
        "acme.video",
    )

    assert entry["capabilities"] == [MediaSourceCapability.DISCOVER.value]


def test_two_sources_on_one_instance_get_their_own_capabilities() -> None:
    """同一实例声明的两个来源各按自己的方法表推导，能力面互不串。"""
    declarations = [
        MediaSourceDeclaration(
            media_source="acme.video",
            name="Acme Video",
            methods={"discover": _impl},
        ),
        MediaSourceDeclaration(
            media_source="acme.music",
            name="Acme Music",
            methods={"recognize_media": _impl, "media_detail": _impl},
        ),
    ]

    assert _source_entry(declarations, "acme.video")["capabilities"] == [
        MediaSourceCapability.DISCOVER.value
    ]
    assert _source_entry(declarations, "acme.music")["capabilities"] == [
        MediaSourceCapability.RECOGNIZE.value,
        MediaSourceCapability.DETAIL.value,
    ]


# ---------------------------------------------------------------------------
# 内建来源与合并
# ---------------------------------------------------------------------------


def test_builtin_sources_declare_only_the_faces_they_implement() -> None:
    """内建来源的能力面与各自模块实际实现的契约方法一致。"""
    faces = {
        source.media_source: [item.value for item in source.capabilities]
        for source in media_endpoint._BUILTIN_MEDIA_SOURCES
    }
    everything = [item.value for item in MediaSourceCapability]

    assert faces[MediaSource.TMDB] == everything
    assert faces[MediaSource.Douban] == everything
    assert faces[MediaSource.Bangumi] == everything
    assert faces[MediaSource.AniList] == everything
    # TheTvDbModule 只实现 media_detail
    assert faces[MediaSource.TVDB] == [MediaSourceCapability.DETAIL.value]
    # 音乐来源只实现 recognize_media，其余走 music_ 族而非多来源契约
    assert faces[MediaSource.MusicBrainz] == [MediaSourceCapability.RECOGNIZE.value]
    assert faces[MediaSource.TheAudioDB] == [MediaSourceCapability.RECOGNIZE.value]
    assert faces[MediaSource.DoubanMusic] == [MediaSourceCapability.RECOGNIZE.value]
    # 内建侧无任何实现的占位来源，能力面由补上实现的插件带来
    for placeholder in (
        MediaSource.IMDb,
        MediaSource.Bilibili,
        MediaSource.MangoTV,
        MediaSource.MiguVideo,
        MediaSource.TencentVideo,
        MediaSource.Iqiyi,
    ):
        assert faces[placeholder] == []


def test_plugin_capabilities_merge_into_a_shadowed_builtin_row(monkeypatch) -> None:
    """插件补上占位内建来源的实现时，推导出的能力面并入那一行。"""

    class _Manager:
        """只交出一条插件来源的插件管理器替身。"""

        @staticmethod
        def get_media_sources():
            """交出一条接管爱奇艺的插件来源描述。"""
            return [
                {
                    "name": "爱奇艺发现",
                    "media_source": MediaSource.Iqiyi.value,
                    "capabilities": [MediaSourceCapability.DISCOVER.value],
                }
            ]

    monkeypatch.setattr(
        "app.application.plugin.runtime.get_plugin_manager", lambda: _Manager()
    )

    sources = media_endpoint._registered_media_sources()
    iqiyi = next(item for item in sources if item.media_source == MediaSource.Iqiyi)

    # 展示信息仍取内建行，能力面取并集
    assert iqiyi.name == "爱奇艺"
    assert iqiyi.capabilities == [MediaSourceCapability.DISCOVER]
    assert len(sources) == len(media_endpoint._BUILTIN_MEDIA_SOURCES)


def test_plugin_only_source_keeps_its_own_capabilities(monkeypatch) -> None:
    """内建表里没有的插件来源原样追加，能力面保持自身推导结果。"""

    class _Manager:
        """只交出一条全新插件来源的插件管理器替身。"""

        @staticmethod
        def get_media_sources():
            """交出一条内建表里不存在的来源描述。"""
            return [
                {
                    "name": "Acme Video",
                    "media_source": "acme.video",
                    "capabilities": [MediaSourceCapability.DETAIL.value],
                }
            ]

    monkeypatch.setattr(
        "app.application.plugin.runtime.get_plugin_manager", lambda: _Manager()
    )

    sources = media_endpoint._registered_media_sources()
    acme = next(item for item in sources if item.media_source == "acme.video")

    assert acme.capabilities == [MediaSourceCapability.DETAIL]
    assert len(sources) == len(media_endpoint._BUILTIN_MEDIA_SOURCES) + 1


# ---------------------------------------------------------------------------
# 端点：能力面穿过 response_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capabilities_survive_the_endpoint_response_model(monkeypatch) -> None:
    """来源列表端点的 response_model 必须把能力面原样交出。"""

    class _Manager:
        """不交出任何插件来源的插件管理器替身。"""

        @staticmethod
        def get_media_sources():
            """交出空来源列表。"""
            return []

    monkeypatch.setattr(
        "app.application.plugin.runtime.get_plugin_manager", lambda: _Manager()
    )

    app = FastAPI()
    app.include_router(media_endpoint.router, prefix="/media")
    app.dependency_overrides[verify_token] = lambda: None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/media/source")

    assert response.status_code == 200
    payload = {item["media_source"]: item for item in response.json()["data"]}
    assert payload[MediaSource.TMDB.value]["capabilities"] == [
        item.value for item in MediaSourceCapability
    ]
    assert payload[MediaSource.Iqiyi.value]["capabilities"] == []
    assert payload[MediaSource.TVDB.value]["capabilities"] == [
        MediaSourceCapability.DETAIL.value
    ]


# ---------------------------------------------------------------------------
# 分发不变：推导只读取事实，不参与路由
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "routed"),
    [
        ("match_media", True),
        ("media_detail", True),
        ("media_credits", True),
        ("person_detail", True),
        ("person_credits", True),
        ("media_recommend", True),
        ("media_similar", True),
        ("discover", True),
        ("discover_board", True),
        ("async_media_detail", True),
        # 有能力面但不由宿主按 source 路由，实现自认领
        ("recognize_media", False),
        ("search_medias", False),
        ("obtain_images", False),
        ("metadata_nfo", False),
        ("metadata_img", False),
        # 既无能力面也不按 source 路由
        ("media_exists", False),
    ],
)
def test_capability_face_does_not_change_source_routing(method, routed) -> None:
    """一个方法归入哪个能力面，与它是否由宿主按 source 路由完全无关。"""
    assert routes_by_source(method) is routed


def test_routed_method_still_yields_for_foreign_and_missing_source() -> None:
    """按 source 收窄的方法仍旧只应答本来源，其余一律让出。"""
    calls: list[dict] = []

    def _record(**kwargs):
        """记录被触达的调用。"""
        calls.append(kwargs)
        return "claimed"

    projection = PluginProjection(
        {
            "Demo": _Plugin(
                [
                    MediaSourceDeclaration(
                        media_source="acme.video",
                        name="Acme Video",
                        methods={"media_detail": _record},
                    )
                ]
            )
        }
    )
    dispatcher = _dispatcher(projection)

    assert dispatcher.unicast("media_detail", source="acme.video", media_id="1") == "claimed"
    assert dispatcher.unicast("media_detail", source=MediaSource.TMDB, media_id="1") is None
    assert dispatcher.unicast("media_detail", media_id="1") is None
    assert len(calls) == 1


def test_face_method_without_source_narrowing_is_still_mounted_plainly() -> None:
    """有能力面但不按 source 收窄的方法仍原样挂载，调用不带 source 也照常触达。"""
    projection = PluginProjection(
        {
            "Demo": _Plugin(
                [
                    MediaSourceDeclaration(
                        media_source="acme.video",
                        name="Acme Video",
                        methods={
                            "recognize_media": lambda **kwargs: "recognized",
                            "media_exists": lambda **kwargs: "exists",
                        },
                    )
                ]
            )
        }
    )
    dispatcher = _dispatcher(projection)

    assert dispatcher.unicast("recognize_media", title="x") == "recognized"
    assert dispatcher.unicast("media_exists", itemid="1") == "exists"


def test_empty_list_still_counts_as_claimed() -> None:
    """弃权协议不变：只有 None 算未认领，空列表照旧算已认领并短路。"""
    projection = PluginProjection(
        {
            "Demo": _Plugin(
                [
                    MediaSourceDeclaration(
                        media_source="acme.video",
                        name="Acme Video",
                        methods={"person_credits": lambda **kwargs: []},
                    )
                ]
            )
        }
    )
    dispatcher = _dispatcher(projection)

    assert dispatcher.unicast("person_credits", source="acme.video", person_id="1") == []
    assert (
        dispatcher.unicast("person_credits", source=MediaSource.Douban, person_id="1")
        is None
    )
