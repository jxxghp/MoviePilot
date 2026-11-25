"""
验证自定义弹弹play 兼容弹幕代理服务的权限隔离、三种鉴权、匹配回退、手动绑定、
JSON/XML 双格式解析、单飞防击穿、L2 gzip 缓存与 ETag 304 协商及 stream-info 元数据。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Generator, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.web.security.access import verify_token as api_verify_token
from app.api.dependencies.auth import get_current_active_user
from app.api.endpoints import danmaku as danmaku_endpoint
from app.api.endpoints import mediaserver as mediaserver_endpoint
from app.application import danmaku as danmaku_app
from app.application.security.secrets import is_secret_setting_key
from app.chain.mediaserver import MediaServerChain
from app.schemas.danmaku import (
    DanmakuBindingRequest,
    DanmakuConfigUpdateRequest,
    DanmakuMatchRequest,
)
from app.schemas.mediaserver import (
    MediaServerItem,
    MediaServerItemDetail,
    MediaServerMediaSource,
)
from app.schemas.token import TokenPayload
from app.schemas.types import MediaSource, SystemConfigKey


class _InMemorySystemConfig:
    """
    测试专用内存系统配置存根，支持原子更新。
    """

    def __init__(self) -> None:
        """
        初始化空配置字典。
        """
        self.store: dict[Any, Any] = {}

    def get(self, key: Any) -> Any:
        """
        按配置键读取值。
        """
        return self.store.get(key)

    def set(self, key: Any, value: Any) -> None:
        """
        直接写入配置键值。
        """
        self.store[key] = value

    def update_atomically(self, key: Any, updater: Callable[[Any], tuple[Any, Any]]) -> Any:
        """
        按回调函数原子更新配置值。
        """
        result, next_value = updater(self.store.get(key))
        self.store[key] = next_value
        return result


class _RecordedRequest:
    """
    记录测试替身收到的单次上游 HTTP 请求明细。

    :param method: 请求方法
    :param url: 完整请求 URL
    :param headers: 请求头副本
    :param json_body: JSON 请求体副本
    :param timeout: 超时秒数
    :param proxies: 代理配置
    :param verify_tls: 是否校验 TLS 证书
    """

    def __init__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: Optional[dict[str, Any]],
        timeout: int,
        proxies: Any,
        verify_tls: bool,
    ) -> None:
        """
        保存单次请求快照。
        """
        self.method = method
        self.url = url
        self.headers = dict(headers)
        self.json_body = dict(json_body) if json_body is not None else None
        self.timeout = timeout
        self.proxies = proxies
        self.verify_tls = verify_tls


class _FakeDanmakuTransport:
    """
    可编程的上游弹幕 HTTP 传输测试替身。
    """

    def __init__(self, handler: Callable[[_RecordedRequest], danmaku_app.DanmakuHttpResponse]) -> None:
        """
        注册响应回调与调用日志。
        """
        self._handler = handler
        self.calls: list[_RecordedRequest] = []
        self._lock = threading.Lock()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Optional[dict[str, Any]] = None,
        timeout: int = 10,
        proxies: Any = None,
        verify_tls: bool = True,
    ) -> danmaku_app.DanmakuHttpResponse:
        """
        记录调用并委托自定义回调构造上游响应。
        """
        record = _RecordedRequest(method, url, headers, json_body, timeout, proxies, verify_tls)
        with self._lock:
            self.calls.append(record)
        return self._handler(record)


@pytest.fixture()
def isolated_danmaku_store(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[_InMemorySystemConfig, None, None]:
    """
    隔离系统配置与运行时密钥，避免污染宿主配置。
    """
    store = _InMemorySystemConfig()
    monkeypatch.setattr(danmaku_app, "get_configured_system_config", lambda: store)
    monkeypatch.setattr(danmaku_app, "get_runtime_setting", lambda name: "unit-test-secret" if "SECRET" in name else None)
    yield store


def _json_response(payload: Any, status_code: int = 200) -> danmaku_app.DanmakuHttpResponse:
    """
    构造 JSON 格式的上游模拟响应。
    """
    return danmaku_app.DanmakuHttpResponse(
        status_code=status_code,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json; charset=utf-8",
    )


def _build_test_client(service: danmaku_app.DanmakuService, *, is_superuser: bool) -> TestClient:
    """
    组装挂载弹幕路由的 FastAPI TestClient，并按角色注入认证依赖。
    """
    app = FastAPI()
    app.include_router(danmaku_endpoint.router, prefix="/api/v1/danmaku")
    app.dependency_overrides[danmaku_endpoint.get_danmaku_service] = lambda: service
    app.dependency_overrides[api_verify_token] = lambda: TokenPayload(
        sub=1 if is_superuser else 2,
        username="admin" if is_superuser else "viewer",
        super_user=is_superuser,
        level=1,
        purpose="authentication",
    )
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        id=1 if is_superuser else 2,
        name="admin" if is_superuser else "viewer",
        is_active=True,
        is_superuser=is_superuser,
        permissions={},
    )
    return TestClient(app)


def test_secret_key_registration_and_url_redaction() -> None:
    """
    验证 DanmakuConfig 已登记为敏感配置键，且 URL 脱敏会隐藏路径令牌与查询参数。
    """
    assert is_secret_setting_key("DanmakuConfig") is True
    assert is_secret_setting_key("danmaku_config") is True
    redacted = danmaku_app.redact_danmaku_url("https://danmu.example.com/secret-path-token/api/v2/match?k=1")
    assert redacted == "https://danmu.example.com/<redacted>"
    assert "secret-path-token" not in redacted
    assert "k=1" not in redacted


def test_permission_isolation_for_non_admin_and_admin(
    isolated_danmaku_store: _InMemorySystemConfig,
    tmp_path: Path,
) -> None:
    """
    验证普通用户访问管理端点返回 403，/status 仅返回 enabled 且不泄露上游地址与密钥。
    """
    transport = _FakeDanmakuTransport(lambda _req: _json_response({"success": True, "animes": []}))
    service = danmaku_app.DanmakuService(transport=transport, cache_root=tmp_path)
    admin_client = _build_test_client(service, is_superuser=True)
    user_client = _build_test_client(service, is_superuser=False)

    # 管理员写入带路径令牌与密钥的配置
    put_resp = admin_client.put(
        "/api/v1/danmaku/config",
        json={
            "enabled": True,
            "api_base_url": "https://danmu.example.com/my-secret-prefix/",
            "auth_mode": "dandanplay_v2",
            "app_id": "mp-app-id",
            "app_secret": "super-secret-value",
        },
    )
    assert put_resp.status_code == 200
    body = put_resp.json()["data"]
    assert body["enabled"] is True
    assert body["api_base_url"] == "https://danmu.example.com/my-secret-prefix"
    assert body["has_app_secret"] is True
    assert "super-secret-value" not in put_resp.text

    # 持久化存储中绝不保存明文密钥
    persisted = isolated_danmaku_store.get(SystemConfigKey.DanmakuConfig)
    assert persisted["app_secret_enc"]
    assert persisted["app_secret_enc"] != "super-secret-value"

    # 普通用户只能查询 /status，且不含任何敏感字段
    status_resp = user_client.get("/api/v1/danmaku/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["data"] == {"enabled": True}
    assert "danmu.example.com" not in status_resp.text
    assert "my-secret-prefix" not in status_resp.text

    # 普通用户调用管理员端点一律返回 403
    assert user_client.get("/api/v1/danmaku/config").status_code == 403
    assert user_client.put("/api/v1/danmaku/config", json={"enabled": False}).status_code == 403
    assert user_client.post("/api/v1/danmaku/config/test", json={}).status_code == 403
    assert user_client.delete("/api/v1/danmaku/cache").status_code == 403


def test_three_auth_modes_and_path_prefix_preservation(
    isolated_danmaku_store: _InMemorySystemConfig,
    tmp_path: Path,
) -> None:
    """
    验证 none（含路径前缀）、dandanplay_v2 签名与 header_token 三种鉴权模式。
    """
    fixed_ts = 1764075000
    transport = _FakeDanmakuTransport(lambda _req: _json_response({"success": True, "animes": []}))
    service = danmaku_app.DanmakuService(transport=transport, cache_root=tmp_path, clock=lambda: float(fixed_ts))

    # 1. none 模式保留路径前缀，不附加鉴权头
    service.update_config(
        DanmakuConfigUpdateRequest(
            enabled=True,
            api_base_url="https://danmu.example.com/custom-token-prefix/",
            auth_mode="none",
        )
    )
    res_none = service.test_config()
    assert res_none.ok is True
    req_none = transport.calls[-1]
    assert req_none.url == "https://danmu.example.com/custom-token-prefix/api/v2/search/anime?keyword=test"
    assert "X-Signature" not in req_none.headers
    assert "Authorization" not in req_none.headers

    # 2. dandanplay_v2 精确签名（仅对 /api/v2/search/anime 路径签名，不含查询串与前缀）
    service.update_config(
        DanmakuConfigUpdateRequest(
            enabled=True,
            api_base_url="https://api.dandanplay.example",
            auth_mode="dandanplay_v2",
            app_id="demo-app",
            app_secret="demo-secret",
        )
    )
    res_v2 = service.test_config()
    assert res_v2.ok is True
    req_v2 = transport.calls[-1]
    expected_sig = base64.b64encode(
        hashlib.sha256(f"demo-app{fixed_ts}/api/v2/search/animedemo-secret".encode("utf-8")).digest()
    ).decode("ascii")
    assert req_v2.headers["X-AppId"] == "demo-app"
    assert req_v2.headers["X-Timestamp"] == str(fixed_ts)
    assert req_v2.headers["X-Signature"] == expected_sig

    # 3. header_token 自定义头与前缀
    service.update_config(
        DanmakuConfigUpdateRequest(
            enabled=True,
            api_base_url="https://selfhost.example/danmu",
            auth_mode="header_token",
            token_header="X-API-Key",
            token_prefix="Token ",
            token="abc-999",
        )
    )
    res_tok = service.test_config()
    assert res_tok.ok is True
    req_tok = transport.calls[-1]
    assert req_tok.url == "https://selfhost.example/danmu/api/v2/search/anime?keyword=test"
    assert req_tok.headers["X-API-Key"] == "Token abc-999"


def test_match_fallback_and_manual_binding_priority(
    isolated_danmaku_store: _InMemorySystemConfig,
    tmp_path: Path,
) -> None:
    """
    验证自动匹配回退 /search/episodes、手动绑定优先级（item > tmdb）与解绑恢复自动匹配。
    """

    def _handler(req: _RecordedRequest) -> danmaku_app.DanmakuHttpResponse:
        if req.url.endswith("/api/v2/match"):
            return _json_response({"isMatched": False, "matches": []})
        if "/api/v2/search/episodes" in req.url:
            return _json_response(
                {
                    "animes": [
                        {
                            "animeId": 2001,
                            "animeTitle": "葬送的芙莉莲",
                            "typeDescription": "TV动画",
                            "episodes": [
                                {"episodeId": 20010001, "episodeTitle": "第1话 冒险的终点"},
                            ],
                        }
                    ]
                }
            )
        return _json_response({})

    transport = _FakeDanmakuTransport(_handler)
    service = danmaku_app.DanmakuService(transport=transport, cache_root=tmp_path)
    service.update_config(
        DanmakuConfigUpdateRequest(enabled=True, api_base_url="https://danmu.example.com")
    )

    # 1. /match 未命中时自动回退到 /search/episodes 并单候选高置信度命中
    auto_res = service.match(
        DanmakuMatchRequest(
            server="EmbyHome",
            item_id="ep-1",
            series_name="葬送的芙莉莲",
            season_number=1,
            episode_number=1,
            tmdb_id="209867",
            file_name="Frieren.S01E01.mkv",
        )
    )
    assert auto_res.matched is True
    assert auto_res.from_binding is False
    assert auto_res.episode is not None
    assert auto_res.episode.episode_id == 20010001

    # 2. 保存手动绑定（含 TMDB 与时间偏移）
    service.save_binding(
        DanmakuBindingRequest(
            server="EmbyHome",
            item_id="ep-1",
            tmdb_id="209867",
            season_number=1,
            episode_number=1,
            episode_id=999001,
            anime_id=999,
            anime_title="手工纠偏番剧",
            episode_title="第1话 修正版",
            shift_ms=-1500,
        ),
        updated_by="admin",
    )
    bound_res = service.match(
        DanmakuMatchRequest(
            server="EmbyHome",
            item_id="ep-1",
            tmdb_id="209867",
            season_number=1,
            episode_number=1,
        )
    )
    assert bound_res.matched is True
    assert bound_res.from_binding is True
    assert bound_res.shift_ms == -1500
    assert bound_res.episode is not None
    assert bound_res.episode.episode_id == 999001

    # 3. 另一台媒体服务器复用同一 TMDB+S+E 绑定
    cross_server_res = service.match(
        DanmakuMatchRequest(
            server="JellyfinOffice",
            item_id="jf-999",
            tmdb_id="209867",
            season_number=1,
            episode_number=1,
        )
    )
    assert cross_server_res.from_binding is True
    assert cross_server_res.episode is not None
    assert cross_server_res.episode.episode_id == 999001

    # 4. 解绑后恢复自动匹配
    removed = service.delete_binding(
        server="EmbyHome",
        item_id="ep-1",
        tmdb_id="209867",
        season_number=1,
        episode_number=1,
    )
    assert removed is True
    restored_res = service.match(
        DanmakuMatchRequest(
            server="EmbyHome",
            item_id="ep-1",
            series_name="葬送的芙莉莲",
            season_number=1,
            episode_number=1,
            tmdb_id="209867",
        )
    )
    assert restored_res.from_binding is False
    assert restored_res.episode is not None
    assert restored_res.episode.episode_id == 20010001


def test_singleflight_l2_gzip_etag_304_and_xml_parsing(
    isolated_danmaku_store: _InMemorySystemConfig,
    tmp_path: Path,
) -> None:
    """
    验证单飞并发防击穿、JSON/XML 双格式解析、L2 gzip 直出与 If-None-Match 304 协商。
    """
    upstream_hits = 0
    hit_lock = threading.Lock()

    def _handler(req: _RecordedRequest) -> danmaku_app.DanmakuHttpResponse:
        nonlocal upstream_hits
        if "/api/v2/comment/1001" in req.url:
            time.sleep(0.05)
            with hit_lock:
                upstream_hits += 1
            return _json_response(
                {
                    "comments": [
                        {"cid": 1, "p": "12.50,1,16777215,[BiliBili]user1", "m": "前方高能"},
                        {"cid": 2, "p": "5.25,5,16766720,[Tencent]user2", "m": "开场打卡"},
                    ]
                }
            )
        if "/api/v2/comment/1002" in req.url:
            xml_body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<!DOCTYPE i [<!ENTITY xxe "ignored">]>'
                '<i><d p="3.5,4,25,16711680,1700000000,0,abc,1">底部红字</d>'
                '<d p="1.2,1,25,16777215,1700000000,0,def,2">XML滚动弹幕</d></i>'
            )
            return danmaku_app.DanmakuHttpResponse(
                status_code=200,
                content=xml_body.encode("utf-8"),
                content_type="application/xml",
            )
        return _json_response({}, status_code=404)

    transport = _FakeDanmakuTransport(_handler)
    service = danmaku_app.DanmakuService(transport=transport, cache_root=tmp_path)
    service.update_config(
        DanmakuConfigUpdateRequest(enabled=True, api_base_url="https://danmu.example.com")
    )

    # 1. 并发 8 线程同时拉取同一分集，上游仅触发 1 次请求
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.get_comments(1001), range(8)))
    assert upstream_hits == 1
    assert len({res.etag for res in results}) == 1

    # 2. 通过 API 验证 gzip 零二次压缩命中与 ETag 304
    client = _build_test_client(service, is_superuser=False)
    r_hit = client.get("/api/v1/danmaku/comments/1001", headers={"Accept-Encoding": "gzip"})
    assert r_hit.status_code == 200
    assert r_hit.headers.get("content-encoding") == "gzip"
    etag = r_hit.headers["etag"]
    data_hit = r_hit.json()["data"]
    assert data_hit["cached"] is True
    assert data_hit["count"] == 2
    assert data_hit["items"] == [
        [5250, 5, 16766720, "开场打卡", "tencent"],
        [12500, 1, 16777215, "前方高能", "bilibili"],
    ]

    r_304 = client.get(
        "/api/v1/danmaku/comments/1001",
        headers={"If-None-Match": etag, "Accept-Encoding": "gzip"},
    )
    assert r_304.status_code == 304
    assert r_304.headers["etag"] == etag

    # 3. 验证 B 站 XML 弹幕解析兼容
    r_xml = client.get("/api/v1/danmaku/comments/1002", headers={"Accept-Encoding": "identity"})
    assert r_xml.status_code == 200
    xml_items = r_xml.json()["data"]["items"]
    assert xml_items == [
        [1200, 1, 16777215, "XML滚动弹幕", "xml"],
        [3500, 4, 16711680, "底部红字", "xml"],
    ]


def test_l2_cache_lru_eviction_and_clear(
    isolated_danmaku_store: _InMemorySystemConfig,
    tmp_path: Path,
) -> None:
    """
    验证 L2 磁盘缓存超出配额时按 LRU 淘汰最旧文件，且管理员清理接口清空缓存。
    """
    service = danmaku_app.DanmakuService(
        transport=_FakeDanmakuTransport(lambda _req: _json_response({"comments": []})),
        cache_root=tmp_path,
    )
    cache_dir = tmp_path / "danmaku" / "src"
    cache_dir.mkdir(parents=True, exist_ok=True)
    old_file = cache_dir / "1_r1_c1.json.gz"
    mid_file = cache_dir / "2_r1_c1.json.gz"
    new_file = cache_dir / "3_r1_c1.json.gz"
    old_file.write_bytes(gzip.compress(b"a" * 4000))
    mid_file.write_bytes(gzip.compress(b"b" * 4000))
    new_file.write_bytes(gzip.compress(b"c" * 4000))
    os.utime(old_file, (1000, 1000))
    os.utime(mid_file, (2000, 2000))
    os.utime(new_file, (3000, 3000))

    max_keep = mid_file.stat().st_size + new_file.stat().st_size
    service._enforce_lru_quota(max_bytes=max_keep, protected_path=new_file)  # pylint: disable=protected-access
    assert not old_file.exists()
    assert mid_file.exists()
    assert new_file.exists()

    cleared = service.clear_cache()
    assert cleared.removed_files == 2
    assert cleared.freed_bytes > 0
    assert service.get_cache_stats().file_count == 0


def test_stream_info_exposes_danmaku_metadata_without_server_directory_path(
    isolated_danmaku_store: _InMemorySystemConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证 /api/v1/mediaserver/stream-info 返回 danmaku_enabled 与安全文件名，不泄露完整目录路径。
    """
    isolated_danmaku_store.set(
        SystemConfigKey.DanmakuConfig,
        {"enabled": True, "api_base_url": "https://danmu.example.com"},
    )
    app = FastAPI()
    app.include_router(mediaserver_endpoint.router, prefix="/api/v1/mediaserver")
    app.dependency_overrides[api_verify_token] = lambda: TokenPayload(
        sub=1,
        username="admin",
        super_user=True,
        level=1,
        purpose="authentication",
    )

    fake_item = MediaServerItem(
        item_id="ep-42",
        title="冒险的终点",
        item_type="Episode",
        year=2023,
        media_source=MediaSource.TMDB,
        media_id="209867",
        path="/mnt/secret_nas/anime/Frieren.S01E01.1080p.mkv",
    )
    fake_detail = MediaServerItemDetail(
        item_id="ep-42",
        title="冒险的终点",
        series_name="葬送的芙莉莲",
        season_number=1,
        episode_number=1,
        year=2023,
        tmdb_id="209867",
        duration_ms=1440000,
        media_sources=[
            MediaServerMediaSource(id="src-1", size=1234567890, duration_ms=1440000),
        ],
    )
    monkeypatch.setattr(
        mediaserver_endpoint,
        "_resolve_stream_item",
        lambda _itemid, _uid, server=None: ("EmbyMain", fake_item, None),
    )
    monkeypatch.setattr(
        mediaserver_endpoint,
        "_playback_source",
        lambda *_args, **_kwargs: ("server", "http://emby.local/stream", set()),
    )
    monkeypatch.setattr(
        mediaserver_endpoint,
        "_prepare_manifest_playback_session",
        lambda **_kwargs: ("sid-1", "emby", "session"),
    )
    monkeypatch.setattr(
        mediaserver_endpoint,
        "_build_manifest_playback_routes",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(MediaServerChain, "item_detail", lambda self, **_kwargs: fake_detail)

    client = TestClient(app)
    resp = client.get("/api/v1/mediaserver/stream-info", params={"itemid": "ep-42", "server": "EmbyMain"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["danmaku_enabled"] is True
    assert data["series_name"] == "葬送的芙莉莲"
    assert data["season_number"] == 1
    assert data["episode_number"] == 1
    assert data["year"] == 2023
    assert data["tmdb_id"] == "209867"
    assert data["file_name"] == "Frieren.S01E01.1080p.mkv"
    assert data["file_size"] == 1234567890
    assert "/mnt/secret_nas" not in resp.text
