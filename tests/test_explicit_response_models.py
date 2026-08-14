"""API 响应中稳定集合与动态 JSON 字段的模型契约测试。"""

from app.schemas.event import DiscoverMediaSource
from app.schemas.file import FileItem, StorageTransType
from app.schemas.history import TransferHistory
from app.schemas.mediaserver import MediaServerLibrary, MediaServerPlayItem, NotExistMediaInfo
from app.schemas.plugin import Plugin, PluginDashboard
from app.schemas.site import SiteStatistic, SiteUserData
from app.schemas.subscribe import Subscribe
from app.schemas.tmdb import TmdbEpisode
from app.schemas.token import Token
from app.schemas.types import MediaSource
from app.schemas.user import User, UserCreate, UserUpdate


def _nonnull_branch(schema: dict) -> dict:
    """返回可空字段中非 null 的 JSON Schema 分支。"""
    return next(
        branch
        for branch in schema.get("anyOf", [schema])
        if branch.get("type") != "null"
    )


def test_stable_collections_serialize_without_changing_payload_shape():
    """稳定集合收窄后应继续输出原有对象、数组及字段名称。"""
    child = FileItem(type="file", name="episode.mkv")
    file_item = FileItem(type="dir", name="Season 1", children=[child])
    episode = TmdbEpisode(
        episode_number=1,
        crew=[{"id": 11, "name": "Writer", "job": "Writer"}],
        guest_stars=[{"id": 12, "name": "Actor", "character": "Guest"}],
    )

    assert file_item.model_dump(mode="json")["children"][0]["name"] == "episode.mkv"
    assert NotExistMediaInfo(episodes=[1, 2]).model_dump()["episodes"] == [1, 2]
    assert MediaServerLibrary(path=["/movies", "/tv"]).model_dump()["path"] == [
        "/movies",
        "/tv",
    ]
    assert MediaServerPlayItem(BackdropImageTags=["backdrop-tag"]).model_dump()[
        "BackdropImageTags"
    ] == ["backdrop-tag"]
    assert StorageTransType(transtype={"move": "移动"}).model_dump()["transtype"] == {
        "move": "移动"
    }
    history = TransferHistory(
        id=1,
        src_storage="local",
        dest_storage="alist",
        src="/downloads/demo.mkv",
        dest="/media/demo.mkv",
        src_fileitem={"path": "/downloads/demo.mkv", "size": 1024},
        dest_fileitem={"path": "/media/demo.mkv", "size": 1024},
        files=[{"path": "/downloads/demo.mkv"}],
    ).model_dump()
    assert history["src_storage"] == "local"
    assert history["dest_storage"] == "alist"
    assert history["src_fileitem"]["size"] == 1024
    assert history["dest_fileitem"]["size"] == 1024
    assert history["files"][0]["path"] == "/downloads/demo.mkv"
    assert episode.model_dump()["crew"][0]["job"] == "Writer"
    assert episode.model_dump()["guest_stars"][0]["character"] == "Guest"


def test_site_and_subscribe_legacy_values_remain_compatible():
    """站点消息的三项、四项协议以及订阅集号列表应继续兼容。"""
    userdata = SiteUserData(
        seeding_info=[[3, 1024]],
        message_unread_contents=[
            ["标题", "日期", "正文"],
            ["标题", "日期", "正文", "sunnypt-message:1"],
        ],
    )

    payload = userdata.model_dump(mode="json")
    assert payload["seeding_info"] == [[3, 1024]]
    assert payload["message_unread_contents"] == [
        ["标题", "日期", "正文"],
        ["标题", "日期", "正文", "sunnypt-message:1"],
    ]
    assert SiteStatistic(note={"2026-08-12 12:00:00": 2}).model_dump()["note"] == {
        "2026-08-12 12:00:00": 2
    }
    assert Subscribe(note=[1, 2]).model_dump()["note"] == [1, 2]


def test_permissions_and_extension_json_keep_values_but_have_explicit_schemas():
    """权限分类和功能映射应保持原值，扩展数据应展示合法 JSON 类型。"""
    permissions = {
        "manage": True,
        "search": False,
        "features": {"search.resource": False},
    }
    token = Token(
        access_token="token",
        token_type="bearer",
        super_user=False,
        user_id=1,
        user_name="tester",
        permissions=permissions,
    )
    user = User(
        name="tester",
        permissions=permissions,
        settings={"nickname": "测试", "layout": {"dense": True}},
    )
    user_create = UserCreate(name="created", permissions={"features": {}})
    user_update = UserUpdate(id=1, name="updated", permissions=permissions)
    plugin = Plugin(history={"v1.0.0": "首次发布"})
    dashboard = PluginDashboard(
        attrs={"class": ["pa-2", {"active": True}]},
        cols={"md": 6},
        elements=[{"component": "VAlert", "text": "状态正常"}],
    )

    assert token.model_dump()["permissions"] == permissions
    assert user.model_dump()["permissions"] == permissions
    assert user_create.model_dump()["permissions"] == {"features": {}}
    assert user_update.model_dump()["permissions"] == permissions
    assert user.model_dump()["settings"]["layout"] == {"dense": True}
    assert plugin.model_dump()["history"] == {"v1.0.0": "首次发布"}
    assert dashboard.model_dump()["elements"][0]["component"] == "VAlert"

    user_schema = User.model_json_schema()
    permission_schema = _nonnull_branch(user_schema["properties"]["permissions"])
    permission_schema = user_schema["$defs"][permission_schema["$ref"].rsplit("/", 1)[-1]]
    settings_schema = _nonnull_branch(user_schema["properties"]["settings"])
    assert permission_schema["properties"]["manage"] == {
        "title": "Manage",
        "type": "boolean",
    }
    assert permission_schema["properties"]["features"]["additionalProperties"] == {
        "type": "boolean"
    }
    assert settings_schema["additionalProperties"]["$ref"].endswith("/JsonData")


def test_collection_json_schemas_define_items_or_tuple_members():
    """集合字段在 OpenAPI 生成前就应声明元素或元组成员结构。"""
    file_schema = FileItem.model_json_schema()
    file_item_schema = file_schema["$defs"]["FileItem"]
    children_schema = _nonnull_branch(file_item_schema["properties"]["children"])
    site_schema = SiteUserData.model_json_schema()
    seeding_schema = _nonnull_branch(site_schema["properties"]["seeding_info"])
    messages_schema = _nonnull_branch(
        site_schema["properties"]["message_unread_contents"]
    )
    episode_schema = TmdbEpisode.model_json_schema()
    crew_schema = _nonnull_branch(episode_schema["properties"]["crew"])

    assert children_schema["items"]["$ref"].endswith("/FileItem")
    assert len(seeding_schema["items"]["prefixItems"]) == 2
    assert all(
        branch.get("prefixItems")
        for branch in messages_schema["items"]["anyOf"]
    )
    assert crew_schema["items"]["$ref"].endswith("/TmdbEpisodeCrew")


def test_discover_media_source_keeps_legacy_prefix_compatible():
    """发现源应兼容旧插件前缀，并同时输出规范媒体来源。"""
    legacy = DiscoverMediaSource(
        name="哔哩哔哩",
        mediaid_prefix="bilibili",
        api_path="plugin/BilibiliDiscover/discover",
    )
    current = DiscoverMediaSource(
        name="腾讯视频",
        media_source=MediaSource.TencentVideo,
        api_path="plugin/TencentVideoDiscover/discover",
    )
    historical_alias = DiscoverMediaSource(
        name="芒果 TV",
        mediaid_prefix="mangguo",
        api_path="plugin/MangoTVDiscover/discover",
    )
    plugin_source = DiscoverMediaSource(
        name="Acme Video",
        media_source=MediaSource("acme.video"),
        api_path="plugin/AcmeVideo/discover",
    )

    assert legacy.media_source is MediaSource.Bilibili
    assert legacy.model_dump(mode="json")["mediaid_prefix"] == "bilibili"
    assert current.mediaid_prefix == MediaSource.TencentVideo.value
    assert historical_alias.media_source is MediaSource.MangoTV
    assert plugin_source.media_source == MediaSource("acme.video")
    assert plugin_source.mediaid_prefix == "acme.video"
