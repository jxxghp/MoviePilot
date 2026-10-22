"""
媒体服务器审查问题的离线回归，覆盖认证、缺集、协议字段与同步失败保护。
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.network.http import RequestUtils
from app.application.mediaserver import MediaServerSyncItem, format_emby_family_item
from app.chain.download.existence import DownloadExistenceOwner
from app.chain.mediaserver import MediaServerChain
from app.db.adapters.mediaserver import TransactionalMediaServerRepository
from app.db.models.mediaserver import MediaServerItem as CachedItem
from app.domain.context import MediaInfo
from app.domain.meta.metavideo import MetaVideo
from app.modules.emby.emby import Emby
from app.modules.jellyfin.jellyfin import Jellyfin
from app.modules.navidrome.navidrome import Navidrome
from app.modules.plex.plex import Plex
from app.modules.trimemedia.api import Api as TrimeApi
from app.modules.trimemedia.api import User as TrimeUser
from app.modules.trimemedia.trimemedia import TrimeMedia
from app.modules.ugreen.api import Api as UgreenApi
from app.modules.ugreen.api import UgreenMediaApi
from app.modules.ugreen.ugreen import Ugreen
from app.modules.vyo.api import Api as VyoApi
from app.modules.vyo.vyo import Vyo
from app.modules.zspace.zspace import ZSpace
from app.schemas.mediaserver import ExistMediaInfo, MediaServerLibrary, MediaServerQueryOptions
from app.schemas.types import MediaSource, MediaType


def _response(payload, status=200):
    """
    创建不会访问网络的 HTTP 响应替身。
    """
    return Mock(status_code=status, ok=status < 400, json=Mock(return_value=payload))


def test_navidrome_concurrent_authentication_preserves_each_identity(monkeypatch):
    """
    并发辅助认证不得借用他人密码，也不得改变服务账号。
    """
    client = Navidrome.__new__(Navidrome)
    client._host = "https://navidrome.invalid/"
    client._username, client._password = "configured", "service-password"
    barrier = Barrier(2)
    original_params = Navidrome._params
    seen = []

    def synchronized_params(instance, **kwargs):
        """
        让两个线程同时构造请求，放大共享凭据覆盖窗口。
        """
        barrier.wait(timeout=5)
        params = original_params(instance, **kwargs)
        barrier.wait(timeout=5)
        return params

    def request(_instance, _url, params):
        """
        仅认可 valid 用户的独立密码签名。
        """
        seen.append(params["u"])
        valid = params["u"] == "valid" and params["t"] == hashlib.md5(
            f"right{params['s']}".encode()
        ).hexdigest()
        return _response({"subsonic-response": {"status": "ok" if valid else "failed"}})

    monkeypatch.setattr(Navidrome, "_params", synchronized_params)
    monkeypatch.setattr(RequestUtils, "get_res", request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        victim = pool.submit(client.authenticate, "victim", "wrong")
        valid = pool.submit(client.authenticate, "valid", "right")
        assert victim.result(timeout=10) is None
        assert valid.result(timeout=10) == "valid"
    assert sorted(seen) == ["valid", "victim"]
    assert (client._username, client._password) == ("configured", "service-password")


@pytest.mark.parametrize("played_at", [
    "2026-10-01T12:34:56Z", "2026-10-01T12:34:56+00:00",
    "2026-10-01T12:34:56.1234567Z", "2026-10-01T12:34:56.5+08:00",
    "2026-10-01T12:34:56",
])
def test_emby_family_accepts_iso_playback_dates(played_at):
    """
    ISO 日期的小数秒和时区变化不应导致整个媒体条目被丢弃。
    """
    item = format_emby_family_item({
        "Id": "film", "Name": "电影", "Type": "Movie",
        "UserData": {"Played": True, "LastPlayedDate": played_at},
    }, server="emby")
    assert item is not None
    assert item.user_state.last_played_date == "2026-10-01 12:34:56"


@pytest.mark.parametrize("client_class", [Emby, Jellyfin])
@pytest.mark.parametrize("end,expected", [(3, [1, 2, 3]), (None, [1]), (0, [1])])
def test_emby_family_combined_episode_includes_end(client_class, end, expected, monkeypatch):
    """
    合并剧集的每个集号都应存在，并映射到同一个物理条目标识。
    """
    client = client_class.__new__(client_class)
    client._host, client._apikey, client.user = "https://media.invalid/", "key", "user"
    monkeypatch.setattr(client, "get_iteminfo", lambda *_args, **_kwargs: Mock())
    monkeypatch.setattr("app.application.mediaserver.MediaServerIdentityHelper.is_compatible", lambda *_args: True)
    monkeypatch.setattr(RequestUtils, "get_res", lambda *_args, **_kwargs: _response({"Items": [{
        "Id": "episode", "ParentIndexNumber": 1, "IndexNumber": 1, "IndexNumberEnd": end,
    }]}))
    assert client.get_tv_episodes(item_id="series")[1] == {1: expected}
    assert client.get_season_episode_ids("series", 1) == {number: "episode" for number in expected}


@pytest.mark.parametrize("server_order", [("partial", "complete"), ("complete", "partial"), ("odd", "even")])
def test_tv_existence_merges_all_servers_independently_of_order(server_order, monkeypatch):
    """
    多个服务器上的完整或互补集数都应参与缺集计算。
    """
    owner = DownloadExistenceOwner.__new__(DownloadExistenceOwner)
    owner.media_server_repository = Mock(get_item_id=Mock(return_value=None))
    episodes = {"partial": [1], "complete": [1, 2], "odd": [1], "even": [2]}
    owner.media_exists = lambda **kwargs: ExistMediaInfo(type=MediaType.TV, seasons={1: episodes[kwargs["server"]]})
    monkeypatch.setattr("app.chain.download.existence.get_mediaserver_configs",
                        lambda: [SimpleNamespace(name=name) for name in server_order])
    media = MediaInfo()
    media.type = MediaType.TV
    media.media_source, media.media_id = MediaSource.TMDB, "123"
    media.seasons = {1: [1, 2]}
    result = owner._media_exists_with_server_cache(media, {})
    assert result.seasons == {1: [1, 2]}
    assert owner.get_no_exists_info(MetaVideo("示例 S01"), media) == (True, {})


@pytest.fixture
def plex_images_cache():
    """
    在测试前后清理媒体库封面区域，防止缓存影响其他测试。
    """
    function = getattr(Plex, "_Plex__get_cached_library_images", Plex._Plex__get_library_images)
    function.cache_clear()
    yield
    function.cache_clear()


def test_plex_library_images_do_not_share_server_or_token(plex_images_cache):
    """
    相同库标识的多个服务器以及令牌轮换应使用各自图片缓存。
    """
    clients = []
    for host, token in [("a", "token-a"), ("b", "token-b"), ("a", "rotated")]:
        client = Plex.__new__(Plex)
        client._host, client._token = f"https://{host}.invalid/", token
        client._plex = Mock()
        client._plex.fetchItems.return_value = [SimpleNamespace(type="movie", thumb="/poster")]
        clients.append(client)
    for client in clients:
        assert client._Plex__get_library_images("review-unique-library", 1) == [
            f"{client._host}poster?X-Plex-Token={client._token}"
        ]
        client._Plex__get_library_images("review-unique-library", 1)
        assert client._plex.fetchItems.call_count == 1


@pytest.mark.parametrize("summary", [None, "", "简介", "长" * 101])
def test_plex_webhook_accepts_nullable_summary(summary, monkeypatch):
    """
    无简介电影仍可解析播放事件，长简介保持截断规则。
    """
    client = Plex.__new__(Plex)
    monkeypatch.setattr(client, "get_remote_image_by_id", lambda **_kwargs: None)
    event = client.get_webhook_message({"payload": json.dumps({
        "event": "media.play", "Metadata": {"type": "movie", "title": "电影", "key": "1", "summary": summary},
    })})
    assert event is not None
    assert event.overview == (summary[:100] + "..." if summary and len(summary) > 100 else summary)


def test_trimemedia_auxiliary_login_revalidates_access_code(monkeypatch):
    """
    辅助认证的新会话必须使用配置访问码，且认证后关闭独立会话。
    """
    client = TrimeMedia.__new__(TrimeMedia)
    client._host, client._username, client._password = "https://trime.invalid/v", "service", "password"
    client._access_code = "device-code"
    verified_sessions = set()
    requests = []

    def get_response(session, url, **_kwargs):
        """
        访问码校验为当前会话授权，不向其他会话传播。
        """
        requests.append(url)
        if url.endswith("/c/device-code"):
            verified_sessions.add(id(session))
            return _response({})
        return None

    def request(session, **kwargs):
        """
        只有通过访问码校验的临时会话可访问版本与登录接口。
        """
        if id(session) not in verified_sessions:
            return None
        if kwargs["url"].endswith("/sys/version"):
            return _response({"code": 0, "data": {"version": "1", "mediasrvVersion": "1"}})
        return _response({"code": 0, "data": {"token": "user-token"}})

    monkeypatch.setattr(RequestUtils, "get_res", get_response)
    monkeypatch.setattr(RequestUtils, "request", request)
    assert client.authenticate("user", "correct") == "user-token"
    assert requests == ["https://trime.invalid/c/device-code"] * 2


@pytest.fixture
def sync_repository(tmp_path):
    """
    为每次同步场景创建独占临时 SQLite 缓存，预存一条旧媒体。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    factory = sessionmaker(bind=engine)
    CachedItem.__table__.create(bind=engine)
    repository = TransactionalMediaServerRepository(factory)
    repository.upsert(MediaServerSyncItem(
        server="test", library="library", item_id="old", item_type="电影", title="旧媒体",
        original_title=None, year=None, media_source=None, media_id=None, path=None,
        seasoninfo=(), note_json=None, lst_mod_date="2026-10-01 00:00:00",
    ))
    yield repository, factory
    engine.dispose()


def _sync_client(kind, monkeypatch, payload):
    """
    使用真实客户端和协议 API，仅替换 HTTP 传输边界。
    """
    calls = []

    def respond(*args, **kwargs):
        """
        为分页场景按请求顺序返回响应，其余场景复用固定响应。
        """
        calls.append((args, kwargs))
        return payload if payload is None or hasattr(payload, "status_code") else payload(*args, **kwargs)

    monkeypatch.setattr(RequestUtils, "get_res", respond)
    monkeypatch.setattr(RequestUtils, "request", respond)
    if kind == "navidrome":
        client = Navidrome.__new__(Navidrome)
        client._host, client._username, client._password, client.user = "https://nav.invalid/", "u", "p", "u"
    elif kind == "zspace":
        client = ZSpace.__new__(ZSpace)
        client._host, client._apikey, client.user = "https://z.invalid/", "key", "u"
    elif kind == "vyo":
        client = Vyo.__new__(Vyo)
        client._host, client._apikey = "https://vyo.invalid/", "key"
        client._api = VyoApi(client._host, client._apikey)
    elif kind == "trime":
        client = TrimeMedia.__new__(TrimeMedia)
        client._host, client._username, client._password = "https://trime.invalid/", "u", "p"
        client._api = TrimeApi(client._host, "key")
        client._api._token, client._userinfo = "token", TrimeUser("u", "u")
    else:
        client = Ugreen.__new__(Ugreen)
        client._host, client._username, client._password = "https://ugreen.invalid/", "u", "p"
        client._sync_libraries = []
        if kind == "ugreen-media":
            client._connection_mode = "media_service"
            client._media_api = UgreenMediaApi(client._host)
            client._media_api._token, client._media_api._user_id = "token", "u"
        else:
            client._connection_mode = "internal"
            client._api = UgreenApi(client._host)
            client._api._token, client._userinfo = "token", {"id": "u"}
            client._api._crypto = SimpleNamespace(
                build_encrypted_request=lambda **kwargs: SimpleNamespace(
                    url=kwargs["url"], headers={}, params=kwargs["params"], json=None, aes_key="fake-key"),
                decrypt_response=lambda data, _key: data,
            )
            client._library_paths = {"library": "/media"}
    return client, calls


_EMPTY_PAYLOADS = {
    "navidrome": {"subsonic-response": {"status": "ok", "albumList2": {}}},
    "zspace": {"Items": [], "TotalRecordCount": 0},
    "vyo": {"success": True, "data": {"items": [], "total": 0}},
    "trime": {"code": 0, "data": {"list": []}},
    "ugreen-media": {"Items": [], "TotalRecordCount": 0},
    "ugreen-internal": {"code": 200, "data": {"video_arr": [], "folder_arr": [], "is_last_page": True}},
}

_FAILED_PAYLOADS = {
    "navidrome": {"subsonic-response": {"status": "failed", "error": {"code": 40}}},
    "zspace": {},
    "vyo": {"success": False, "message": "unauthorized"},
    "trime": {"code": -15, "msg": "unauthorized"},
    "ugreen-media": {},
    "ugreen-internal": {"code": 401, "msg": "unauthorized"},
}

_MALFORMED_PAYLOADS = {
    "navidrome": {"subsonic-response": {"status": "ok", "albumList2": {"album": {}}}},
    "zspace": {"Items": {}},
    "vyo": {"success": True, "data": {"items": {}}},
    "trime": {"code": 0, "data": {"list": {}}},
    "ugreen-media": {"Items": {}},
    "ugreen-internal": {"code": 200, "data": {"video_arr": {}, "folder_arr": [], "is_last_page": True}},
}


@pytest.mark.parametrize("kind", list(_EMPTY_PAYLOADS))
@pytest.mark.parametrize("state", ["network", "rejected", "malformed", "empty"])
def test_remote_library_failure_preserves_sqlite_cache(kind, state, monkeypatch, sync_repository):
    """
    真实同步链只在确认成功的空库后删除旧缓存，网络失败必须保留旧记录。
    """
    repository, factory = sync_repository
    successful_empty = state == "empty"
    payload = None if state == "network" else _response(
        _EMPTY_PAYLOADS[kind] if successful_empty else (
            _MALFORMED_PAYLOADS[kind] if state == "malformed" else _FAILED_PAYLOADS[kind]
        ),
        status=403 if state == "rejected" and kind in {"zspace", "ugreen-media"} else 200,
    )
    client, calls = _sync_client(kind, monkeypatch, payload)
    chain = MediaServerChain.__new__(MediaServerChain)
    chain.items = lambda **kwargs: (
        client.get_items(query=kwargs["query"]) if kind == "navidrome" else client.get_items(kwargs["library_id"])
    )
    def operation():
        """
        对真实短事务缓存执行完整服务器枚举。
        """
        return chain._sync_server_libraries(
            "test", [MediaServerLibrary(id="library", name="媒体库")], {"library": None}, repository,
            "2026-10-23 00:00:00", None, 1, 1, None, 0,
        )
    if successful_empty:
        assert operation() == (0, 0)
    else:
        with pytest.raises(ConnectionError):
            operation()
    with factory() as session:
        assert session.query(CachedItem).count() == (0 if successful_empty else 1)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", list(_EMPTY_PAYLOADS))
@pytest.mark.parametrize("failure", ["network", "malformed"])
def test_partial_library_failure_preserves_older_sqlite_rows(kind, failure, monkeypatch, sync_repository):
    """
    后续页或子目录失败时，即使已写入部分新媒体，也不能清理尚未枚举的旧媒体。
    """
    movie = {"Id": "new", "Type": "Movie", "Name": "电影", "ProductionYear": 2026,
             "Path": "/media/new", "ProviderIds": {"Tmdb": "123"}}
    first_pages = {
        "navidrome": {"subsonic-response": {"status": "ok", "albumList2": {
            "album": [{"id": f"new-{index}", "name": "专辑"} for index in range(500)],
        }}},
        "zspace": {"Items": [movie], "TotalRecordCount": 2},
        "vyo": {"success": True, "data": {"items": [
            {"id": f"new-{index}", "kind": "Movie", "title": "电影"} for index in range(100)
        ]}},
        "trime": {"code": 0, "data": {"list": [
            {"guid": "new", "type": "Movie", "title": "电影"},
            {"guid": "folder", "type": "Directory"},
        ]}},
        "ugreen-media": {"Items": [dict(movie, Id=f"new-{index}") for index in range(100)],
                         "TotalRecordCount": 101},
        "ugreen-internal": {"code": 200, "data": {
            "video_arr": [{"ug_video_info_id": "new", "type": 1, "name": "电影"}],
            "folder_arr": [{"path": "/media/subfolder"}], "is_last_page": True,
        }},
    }
    responses = iter([_response(first_pages[kind]),
                      _response(_MALFORMED_PAYLOADS[kind]) if failure == "malformed" else None])
    client, calls = _sync_client(kind, monkeypatch, lambda *_args, **_kwargs: next(responses))
    chain = MediaServerChain.__new__(MediaServerChain)
    chain.items = lambda **kwargs: (
        client.get_items(query=kwargs["query"]) if kind == "navidrome" else client.get_items(kwargs["library_id"])
    )
    repository, factory = sync_repository
    with pytest.raises(ConnectionError):
        chain._sync_server_libraries(
            "test", [MediaServerLibrary(id="library", name="媒体库")], {"library": None}, repository,
            "2026-10-23 00:00:00", None, 1, 1, None, 0,
        )
    with factory() as session:
        assert session.query(CachedItem).filter(CachedItem.item_id == "old").count() == 1
        assert session.query(CachedItem).count() > 1
    assert len(calls) == 2


@pytest.mark.parametrize("projected", [False, True])
def test_navidrome_partial_page_failure_is_not_successful_enumeration(projected, monkeypatch):
    """
    两种查询模式都必须在后续页失败时中止，不能返回半份完整库。
    """
    client = Navidrome.__new__(Navidrome)
    responses = iter([{"albumList2": {"album": [{"id": str(index)} for index in range(500)]}}, None])
    monkeypatch.setattr(client, "_call", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(ConnectionError):
        list(client.get_items(**({"query": MediaServerQueryOptions(fields=frozenset())} if projected else {})))
