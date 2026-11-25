"""
验证原生播放器与媒体服务器（Emby / Jellyfin / Plex / 极影视 / 绿联 / Navidrome）
实时播放会话、进度同步兜底、同源流头透传、凭据加密绑定与辅助认证自动捕获。
"""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, Callable, Generator, Optional
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters import mediastream
from app.adapters.web.security.access import verify_token as api_verify_token
from app.api.endpoints import mediaserver as mediaserver_endpoint
from app.application import playback as playback_app
from app.chain.user import UserChain
from app.modules._base.embybrowse import (
    EmbyBrowseConfig,
    emby_prepare_playback_session,
    emby_report_progress_detail,
    emby_stream_url,
)
from app.modules._base.plexbrowse import (
    plex_prepare_playback_session,
    plex_report_progress_detail,
)
from app.modules.navidrome.navidrome import Navidrome
from app.modules.plex.plex import Plex
from app.schemas.event import AuthCredentials
from app.schemas.mediaserver import MediaServerItem
from app.schemas.system import MediaServerConf
from app.schemas.token import TokenPayload


class _InMemorySystemConfig:
    """测试专用的内存系统配置存根，支持原子更新。"""

    def __init__(self) -> None:
        """初始化空配置映射。"""
        self._store: dict[Any, Any] = {}

    def get(self, key: Any) -> Any:
        """按配置键读取值。"""
        return self._store.get(key)

    def set(self, key: Any, value: Any) -> None:
        """写入指定配置键。"""
        self._store[key] = value

    def update_atomically(self, key: Any, updater: Callable[[Any], tuple[Any, Any]]) -> Any:
        """按回调函数原地原子更新配置值。"""
        result, next_value = updater(self._store.get(key))
        self._store[key] = next_value
        return result


@pytest.fixture()
def isolated_playback_env(monkeypatch: pytest.MonkeyPatch) -> Generator[_InMemorySystemConfig, None, None]:
    """隔离内存系统配置、会话注册表与默认密钥，避免污染其他测试。"""
    store = _InMemorySystemConfig()
    monkeypatch.setattr(playback_app, "get_configured_system_config", lambda: store)
    monkeypatch.setattr(playback_app, "get_runtime_setting", lambda _name: "test-secret-key")
    playback_app.get_playback_session_registry().reset()
    yield store
    playback_app.get_playback_session_registry().reset()


def _mock_http_response(status_code: int = 200, payload: Optional[dict[str, Any]] = None) -> Mock:
    """构造最小 HTTP 响应测试替身。"""
    res = Mock()
    res.status_code = status_code
    res.text = "{}" if payload is not None else ""
    res.json.return_value = payload or {}
    return res


def test_emby_session_mode_lifecycle_self_heal_and_401_fallback() -> None:
    """验证 Emby/Jellyfin 用户令牌模式：PlaybackInfo、起播自愈、进度、停止及 401 降级 UserData。"""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    responses: list[Mock] = [
        _mock_http_response(200, {"PlaySessionId": "emby-official-sid", "MediaSources": [{"Id": "src-4k"}]}),
        _mock_http_response(204),  # auto-heal Sessions/Playing
        _mock_http_response(204),  # Sessions/Playing/Progress
        _mock_http_response(204),  # Sessions/Playing/Stopped
    ]

    def _make_client() -> Any:
        client = Mock()

        def _post(url: str, **kwargs: Any) -> Mock:
            calls.append(("POST", url, kwargs))
            return responses.pop(0)

        client.post_res.side_effect = _post
        return client

    cfg = EmbyBrowseConfig(
        host="http://emby.local/",
        api_key="admin-api-key",
        user_id="user-1",
        server_type="emby",
        api_prefix="emby/",
        request_factory=_make_client,
        is_user_token=False,
    )

    session = emby_prepare_playback_session(
        cfg,
        "item-1",
        media_source_id="src-4k",
        user_token="user-tok-123",
        client_id="client-abc",
        device_name="iPhone 16 Pro",
        platform="iOS",
        app_version="2.0.3",
    )
    assert session["play_session_id"] == "emby-official-sid"
    assert session["sync_mode"] == "session"
    assert 'Token="user-tok-123"' in session["stream_headers"]["Authorization"]
    assert 'DeviceId="client-abc"' in session["stream_headers"]["X-Emby-Authorization"]

    stream_url = emby_stream_url(
        cfg, "item-1", "Movie", media_source_id="src-4k", play_session_id="emby-official-sid", user_token="user-tok-123"
    )
    assert stream_url is not None
    assert "PlaySessionId=emby-official-sid" in stream_url
    assert "api_key=user-tok-123" in stream_url

    # 未发过 started 时直接收到 playing 心跳：应自动先补发 Sessions/Playing 再发 Progress
    progress = emby_report_progress_detail(
        cfg,
        "item-1",
        60_000,
        media_source_id="src-4k",
        duration_ms=3_600_000,
        state="playing",
        play_session_id="emby-official-sid",
        client_id="client-abc",
        device_name="iPhone 16 Pro",
        user_token="user-tok-123",
        started_reported=False,
    )
    assert progress["reported"] is True
    assert progress["mode"] == "session"
    assert progress["started_sent"] is True
    assert calls[1][1].endswith("/emby/Sessions/Playing")
    assert calls[2][1].endswith("/emby/Sessions/Playing/Progress")
    assert calls[2][2]["json"]["PlaySessionId"] == "emby-official-sid"
    assert calls[2][2]["json"]["PositionTicks"] == 600_000_000

    stopped = emby_report_progress_detail(
        cfg,
        "item-1",
        3_500_000,
        media_source_id="src-4k",
        duration_ms=3_600_000,
        state="stopped",
        play_session_id="emby-official-sid",
        client_id="client-abc",
        user_token="user-tok-123",
        started_reported=True,
    )
    assert stopped["reported"] is True
    assert calls[3][1].endswith("/emby/Sessions/Playing/Stopped")

    # 模拟用户令牌被服务端撤销（HTTP 401）：立即标记 token_invalid 并回退 API Key PlayedItems（>=90%）
    fallback_calls: list[tuple[str, dict[str, Any]]] = []
    fallback_responses = [
        _mock_http_response(401),  # Sessions/Playing/Stopped -> 401
        _mock_http_response(200),  # Users/user-1/PlayedItems/item-1 -> 200 (>=90%)
    ]

    def _fallback_client() -> Any:
        client = Mock()

        def _post(url: str, **kwargs: Any) -> Mock:
            fallback_calls.append((url, kwargs))
            return fallback_responses.pop(0)

        client.post_res.side_effect = _post
        return client

    cfg_fallback = EmbyBrowseConfig(
        host="http://emby.local/",
        api_key="admin-api-key",
        user_id="user-1",
        server_type="emby",
        api_prefix="emby/",
        request_factory=_fallback_client,
    )
    res_401 = emby_report_progress_detail(
        cfg_fallback,
        "item-1",
        3_400_000,
        duration_ms=3_600_000,
        state="stopped",
        play_session_id="emby-official-sid",
        user_token="expired-token",
        started_reported=True,
    )
    assert res_401["reported"] is True
    assert res_401["mode"] == "userdata"
    assert res_401["token_invalid"] is True
    assert res_401["needs_account_bind"] is True
    assert res_401["userdata_written"] is True
    assert fallback_calls[1][0].endswith("/emby/Users/user-1/PlayedItems/item-1")


def test_jellyfin_userdata_fallback_handles_10_9_404() -> None:
    """验证 API Key 兜底模式兼容 Jellyfin 10.9+ 优先新路径并在旧版 404 时回退到 Users/{uid}/Items/{id}/UserData。"""
    urls: list[str] = []
    responses = [
        _mock_http_response(404),  # UserItems/{id}/UserData -> 404 (older Jellyfin)
        _mock_http_response(200),  # Users/{uid}/Items/{id}/UserData -> 200
    ]

    http = Mock()

    def _post(url: str, **kwargs: Any) -> Mock:
        urls.append(url)
        _ = kwargs
        return responses.pop(0)

    http.post_res.side_effect = _post
    cfg = EmbyBrowseConfig(
        host="http://jf.local/",
        api_key="jf-api-key",
        user_id="jf-uid",
        server_type="jellyfin",
        api_prefix="",
        request_factory=lambda: http,
        is_user_token=False,
    )
    result = emby_report_progress_detail(
        cfg,
        "ep-99",
        180_000,
        duration_ms=2_400_000,
        state="paused",
        allow_userdata_write=True,
    )
    assert result["reported"] is True
    assert result["mode"] == "userdata"
    assert result["needs_account_bind"] is True
    assert urls[0].endswith("/UserItems/ep-99/UserData")
    assert urls[1].endswith("/Users/jf-uid/Items/ep-99/UserData")


def test_plex_direct_timeline_and_scrobble_without_fetch_item() -> None:
    """验证 Plex 直调 /:/timeline 隔离会话头并在 >=90% 停止时补发 /:/scrobble。"""
    captured: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def _factory(headers: dict[str, str]) -> Any:
        http = Mock()

        def _get(url: str, params: Optional[dict[str, Any]] = None) -> Mock:
            captured.append((url, dict(params or {}), dict(headers)))
            return _mock_http_response(200)

        http.get_res.side_effect = _get
        return http

    session = plex_prepare_playback_session(
        "plex-user-tok",
        media_source_id="55",
        client_id="client-plex-1",
        device_name="Pixel 9",
        platform="Android",
        app_version="2.0.3",
    )
    assert session["sync_mode"] == "session"
    assert session["stream_headers"]["X-Plex-Client-Identifier"] == "client-plex-1"
    assert session["stream_headers"]["X-Plex-Session-Identifier"] == session["play_session_id"]

    res = plex_report_progress_detail(
        "http://plex.local:32400/",
        "plex-user-tok",
        "/library/metadata/101",
        3_300_000,
        duration_ms=3_600_000,
        state="stopped",
        play_session_id=session["play_session_id"],
        client_id="client-plex-1",
        device_name="Pixel 9",
        platform="Android",
        app_version="2.0.3",
        playback_time_ms=120_000,
        request_factory=_factory,
    )
    assert res["reported"] is True
    assert res["mode"] == "session"
    assert res["scrobbled"] is True
    assert len(captured) == 2
    assert captured[0][0] == "http://plex.local:32400/:/timeline"
    assert captured[0][1]["ratingKey"] == "101"
    assert captured[0][1]["key"] == "/library/metadata/101"
    assert captured[0][1]["playbackTime"] == 120_000
    assert captured[0][2]["X-Plex-Session-Identifier"] == session["play_session_id"]
    assert captured[1][0] == "http://plex.local:32400/:/scrobble"


def test_navidrome_subsonic_scrobble_now_playing_and_threshold() -> None:
    """验证 Navidrome 起播发送 submission=false、中途心跳不刷屏、完播达标发送 submission=true。"""
    client = object.__new__(Navidrome)
    client.user = "music_user"
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_call(method: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((method, kwargs))
        return {"status": "ok"}

    client._call = _fake_call  # type: ignore[method-assign]

    started = client.report_progress("song-1", position_ms=0, duration_ms=200_000, state="started")
    assert started["reported"] is True
    assert started["started_sent"] is True
    assert calls[-1] == ("scrobble", {"id": "song-1", "submission": "false"})

    mid = client.report_progress("song-1", position_ms=40_000, duration_ms=200_000, state="playing", started_reported=True)
    assert mid["reported"] is True
    assert len(calls) == 1  # 中途不刷屏

    stopped = client.report_progress(
        "song-1", position_ms=120_000, duration_ms=200_000, state="stopped", started_reported=True
    )
    assert stopped["reported"] is True
    assert stopped["scrobbled"] is True
    assert calls[-1][0] == "scrobble"
    assert calls[-1][1]["submission"] == "true"
    assert calls[-1][1]["id"] == "song-1"


def test_upstream_stream_strips_extra_headers_on_cross_origin_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证上游流代理在同源主机透传会话头，遇跨源 302 重定向自动剥离会话头。"""
    captured_calls: list[tuple[str, dict[str, str]]] = []
    redirect_res = Mock(status_code=302, headers={"Location": "http://cdn.external/video.mkv"})
    final_res = Mock(status_code=206, headers={"Content-Type": "video/x-matroska"})

    class _FakeRequest:
        """捕获每次上游请求头。"""

        def get_stream(self, url: str, headers: dict[str, str], **_kwargs: Any) -> Any:
            captured_calls.append((url, dict(headers)))
            return nullcontext(redirect_res if len(captured_calls) == 1 else final_res)

        def close(self) -> None:
            """释放模拟连接。"""

    monkeypatch.setattr(mediastream, "RequestUtils", lambda **_kwargs: _FakeRequest())
    monkeypatch.setattr(mediastream, "_validate_stream_url", lambda url, **_kwargs: url)

    res, stream_cm, client = mediastream._open_upstream_stream(
        "http://emby.local:8096/emby/Videos/1/stream",
        "bytes=0-10",
        {"emby.local", "cdn.external"},
        extra_headers={"X-Emby-Token": "secret-tok", "X-Emby-Authorization": 'MediaBrowser Client="MoviePilot"'},
    )
    assert res.status_code == 206
    stream_cm.__exit__(None, None, None)
    client.close()
    assert captured_calls[0][1]["X-Emby-Token"] == "secret-tok"
    assert "X-Emby-Token" not in captured_calls[1][1]
    assert "X-Emby-Authorization" not in captured_calls[1][1]
    assert captured_calls[1][1]["Range"] == "bytes=0-10"


def test_session_registry_ordering_idempotency_and_throttle() -> None:
    """验证播放会话注册表具备乱序丢弃、终态停止幂等和 UserData 30 秒节流能力。"""
    registry = playback_app.PlaybackSessionRegistry()
    sess, skip1, allow1 = registry.begin_progress_event(
        play_session_id="sid-1",
        user_id=1,
        username="alice",
        server="Emby",
        server_type="emby",
        item_id="ep-1",
        media_source_id="src-1",
        state="playing",
        seq=2,
        client_id="cid-1",
        device_name="iPhone",
        platform="iOS",
        app_version="2.0.3",
        play_method="DirectPlay",
    )
    assert skip1 is False
    assert allow1 is True
    registry.record_progress_outcome(sess, state="playing", userdata_written=True)

    # 乱序旧包 seq=1 应被跳过
    _, skip_old, _ = registry.begin_progress_event(
        play_session_id="sid-1",
        user_id=1,
        username="alice",
        server="Emby",
        server_type="emby",
        item_id="ep-1",
        media_source_id="src-1",
        state="playing",
        seq=1,
        client_id="cid-1",
        device_name="iPhone",
        platform="iOS",
        app_version="2.0.3",
        play_method="DirectPlay",
    )
    assert skip_old is True

    # 30 秒内连续 playing 应节流 UserData 写入
    _, skip_next, allow_next = registry.begin_progress_event(
        play_session_id="sid-1",
        user_id=1,
        username="alice",
        server="Emby",
        server_type="emby",
        item_id="ep-1",
        media_source_id="src-1",
        state="playing",
        seq=3,
        client_id="cid-1",
        device_name="iPhone",
        platform="iOS",
        app_version="2.0.3",
        play_method="DirectPlay",
    )
    assert skip_next is False
    assert allow_next is False

    # 终态 stopped 后再次收到 playing/stopped 均幂等忽略
    sess_stop, skip_stop, allow_stop = registry.begin_progress_event(
        play_session_id="sid-1",
        user_id=1,
        username="alice",
        server="Emby",
        server_type="emby",
        item_id="ep-1",
        media_source_id="src-1",
        state="stopped",
        seq=4,
        client_id="cid-1",
        device_name="iPhone",
        platform="iOS",
        app_version="2.0.3",
        play_method="DirectPlay",
    )
    assert skip_stop is False
    assert allow_stop is True
    registry.record_progress_outcome(sess_stop, state="stopped")

    _, skip_dup_stop, _ = registry.begin_progress_event(
        play_session_id="sid-1",
        user_id=1,
        username="alice",
        server="Emby",
        server_type="emby",
        item_id="ep-1",
        media_source_id="src-1",
        state="stopped",
        seq=5,
        client_id="cid-1",
        device_name="iPhone",
        platform="iOS",
        app_version="2.0.3",
        play_method="DirectPlay",
    )
    assert skip_dup_stop is True


def test_account_binding_endpoints_and_auxiliary_auth_capture(
    isolated_playback_env: _InMemorySystemConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证账号绑定 REST 接口、密文落库不泄密、以及辅助登录自动留存用户令牌。"""
    configs = [
        MediaServerConf(name="客厅 Emby", type="emby", enabled=True, config={"host": "http://emby.local"}),
        MediaServerConf(name="主 Plex", type="plex", enabled=True, config={"host": "http://plex.local"}),
        MediaServerConf(name="音乐库", type="navidrome", enabled=True, config={"host": "http://navi.local"}),
    ]
    chain = Mock()
    chain.authenticate_user.side_effect = lambda server, **kwargs: (
        AuthCredentials(
            grant_type="token" if kwargs.get("token") else "password",
            username=kwargs.get("username") or "plex_user",
            password=kwargs.get("password"),
            mfa_code=None,
            code=None,
            token=kwargs.get("token") or "emby-secret-user-token",
            service=server,
            channel="Emby" if server == "客厅 Emby" else "Plex",
        )
    )
    chain.find_item.return_value = ("客厅 Emby", MediaServerItem(item_id="m-1", title="测试片", item_type="Movie"))
    chain.item_detail.return_value = None
    chain.get_stream_url.return_value = "http://emby.local/emby/Videos/m-1/stream?static=true"
    chain.prepare_playback_session.return_value = {
        "play_session_id": "emby-psid-1",
        "media_source_id": "src-1",
        "stream_headers": {"X-Emby-Token": "emby-secret-user-token"},
        "sync_mode": "session",
    }
    chain.report_progress_detail.return_value = {
        "reported": True,
        "mode": "session",
        "started_sent": True,
        "scrobbled": False,
        "userdata_written": False,
        "token_invalid": False,
        "needs_account_bind": False,
        "detail": None,
    }

    monkeypatch.setattr(mediaserver_endpoint, "get_mediaserver_configs", lambda: configs)
    monkeypatch.setattr(mediaserver_endpoint, "MediaServerChain", lambda: chain)
    monkeypatch.setattr(mediaserver_endpoint, "_configured_stream_hosts", lambda *_args, **_kwargs: {"emby.local"})

    app = FastAPI()
    app.include_router(mediaserver_endpoint.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[api_verify_token] = lambda: TokenPayload(sub=7, username="alice")

    with TestClient(app) as client:
        # 1. 初始状态：Emby 为 userdata/unbound，Plex 为 session/unbound，Navidrome 为 scrobble/builtin
        initial = client.get("/api/v1/mediaserver/account-bindings").json()["data"]
        by_name = {entry["server"]: entry for entry in initial}
        assert by_name["客厅 Emby"]["sync_mode"] == "userdata"
        assert by_name["客厅 Emby"]["status"] == "unbound"
        assert by_name["主 Plex"]["sync_mode"] == "session"
        assert by_name["音乐库"]["status"] == "builtin"

        # 2. 绑定 Emby 账号：密码不落库，令牌加密落库且响应中绝不回显令牌
        bind_resp = client.post(
            "/api/v1/mediaserver/account-bindings",
            json={"server": "客厅 Emby", "username": "alice_emby", "password": "super-secret-password"},
        )
        assert bind_resp.status_code == 200
        assert "super-secret-password" not in bind_resp.text
        assert "emby-secret-user-token" not in bind_resp.text
        bind_data = bind_resp.json()["data"]
        assert bind_data["bound"] is True
        assert bind_data["sync_mode"] == "session"
        assert bind_data["remote_username"] == "alice_emby"

        # 验证持久化存储中为密文而非明文令牌/密码
        raw_stored = str(isolated_playback_env._store)
        assert "emby-secret-user-token" not in raw_stored
        assert "super-secret-password" not in raw_stored

        # 2.5 错误凭据应返回 400 而非 401，避免触发客户端退出登录
        orig_side_effect = chain.authenticate_user.side_effect
        chain.authenticate_user.side_effect = None
        chain.authenticate_user.return_value = None
        bad_bind = client.post(
            "/api/v1/mediaserver/account-bindings",
            json={"server": "客厅 Emby", "username": "alice_emby", "password": "wrong-password"},
        )
        assert bad_bind.status_code == 400
        assert "wrong-password" not in bad_bind.text
        chain.authenticate_user.side_effect = orig_side_effect

        # 3. 绑定 Plex 支持直接提交 token
        plex_bind = client.post(
            "/api/v1/mediaserver/account-bindings",
            json={"server": "主 Plex", "token": "plex-direct-token-xyz"},
        )
        assert plex_bind.status_code == 200
        assert plex_bind.json()["data"]["bound"] is True
        assert "plex-direct-token-xyz" not in plex_bind.text

        # 4. stream-info 应返回 play_session_id、server_type、progress_sync_mode 并拼入 content_url
        manifest_resp = client.get(
            "/api/v1/mediaserver/stream-info",
            params={"itemid": "m-1", "server": "客厅 Emby"},
            headers={"X-MP-Client-Id": "ios-cid-1", "X-MP-Platform": "iOS", "X-MP-Device-Name": "iPhone"},
        )
        assert manifest_resp.status_code == 200
        manifest = manifest_resp.json()["data"]
        assert manifest["play_session_id"] == "emby-psid-1"
        assert manifest["server_type"] == "emby"
        assert manifest["progress_sync_mode"] == "session"
        assert parse_qs(urlsplit(manifest["content_url"]).query)["play_session_id"] == ["emby-psid-1"]

        # 5. 上报进度应返回结构化回执
        progress_resp = client.post(
            "/api/v1/mediaserver/progress",
            json={
                "item_id": "m-1",
                "server": "客厅 Emby",
                "position_ms": 15_000,
                "duration_ms": 120_000,
                "state": "started",
                "play_session_id": "emby-psid-1",
                "seq": 1,
            },
        )
        assert progress_resp.status_code == 200
        prog_json = progress_resp.json()
        assert prog_json["success"] is True
        assert prog_json["data"]["mode"] == "session"
        assert prog_json["data"]["play_session_id"] == "emby-psid-1"

        # 6. 解绑后恢复 userdata/unbound
        del_resp = client.delete("/api/v1/mediaserver/account-bindings/客厅 Emby")
        assert del_resp.status_code == 200
        after_del = {entry["server"]: entry for entry in client.get("/api/v1/mediaserver/account-bindings").json()["data"]}
        assert after_del["客厅 Emby"]["bound"] is False
        assert after_del["客厅 Emby"]["sync_mode"] == "userdata"

    # 7. 辅助认证成功时自动捕获令牌
    import app.chain.user as user_chain_mod

    monkeypatch.setattr(user_chain_mod, "get_mediaserver_configs", lambda: configs)
    UserChain._capture_auxiliary_mediaserver_token(
        "bob",
        AuthCredentials(
            grant_type="password",
            username="bob_emby",
            password="aux-secret-password",
            mfa_code=None,
            code=None,
            token="aux-emby-tok-999",
            service="客厅 Emby",
            channel="Emby",
        ),
    )
    captured_cred = playback_app.get_user_mediaserver_credential("bob", "客厅 Emby")
    assert captured_cred is not None
    assert captured_cred.token == "aux-emby-tok-999"
    assert captured_cred.source == "auxiliary_auth"


def test_plex_verify_user_token_and_managed_user_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 Plex 直接令牌校验以及 Plex Home 托管用户令牌自动解析。"""
    plex_client = object.__new__(Plex)
    plex_client._host = "http://plex.local:32400/"
    plex_client._token = "owner-tok"
    plex_client._timeout = 10
    plex_client._managed_user_tokens = {}
    plex_client._plex = SimpleNamespace(machineIdentifier="machine-1")

    shared_xml = (
        '<MediaContainer><SharedServer username="family_kid" '
        'title="Family Kid" accessToken="managed-user-token" /></MediaContainer>'
    )
    fake_http = Mock()
    fake_http.get_res.side_effect = [
        SimpleNamespace(status_code=200, text=shared_xml, json=lambda: {}),
        _mock_http_response(200),
        _mock_http_response(200, {"MyPlex": {"username": "plex_2fa_user"}}),
    ]
    monkeypatch.setattr("app.modules.plex.plex.RequestUtils", lambda **_kwargs: fake_http)

    resolved_token, is_explicit = plex_client._resolve_user_token(username="family_kid")
    assert resolved_token == "managed-user-token"
    assert is_explicit is False

    # 验证直接令牌校验
    assert plex_client.verify_user_token("tok-2fa") == "plex_2fa_user"
