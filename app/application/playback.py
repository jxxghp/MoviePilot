"""
媒体服务器用户播放凭据加密存储与进程内播放会话注册表。

本模块遵循应用层边界，不直接导入数据库会话、ORM 模型或具体媒体服务器模块。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from app.application.configuration import get_configured_system_config
from app.application.security.token import decrypt, encrypt_message
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.mediaserver import (
    MediaServerAccountBinding,
    MediaServerBindingStatus,
    MediaServerPlaybackRoute,
    MediaServerSyncMode,
)
from app.schemas.system import MediaServerConf
from app.schemas.types import SystemConfigKey

# 进程内播放会话缓存上限，避免长时运行无限占用内存
_MAX_PLAYBACK_SESSIONS = 2048
# 单条播放会话上下文最长存活秒数（12 小时）
_SESSION_TTL_SECONDS = 12 * 60 * 60
# API Key UserData 兜底写入播放中进度的最小间隔秒数
USERDATA_FALLBACK_THROTTLE_SECONDS = 30.0
# 支持用户单独绑定个人令牌的媒体服务器类型
BINDABLE_MEDIA_SERVER_TYPES = frozenset({"emby", "jellyfin", "plex"})
# 支持在用户绑定个人令牌后向客户端签发直连路由的媒体服务器类型
DIRECT_PLAY_SERVER_TYPES = frozenset({"emby", "jellyfin", "plex"})
# 内置已完成用户态登录的 Emby 兼容服务器类型
BUILTIN_USER_SESSION_TYPES = frozenset({"zspace"})
# 客户端直连 URL 中必须剔除的凭据查询参数名（凭据统一走同源 Header）
_CREDENTIAL_QUERY_KEYS = frozenset({"api_key", "apikey", "x-emby-token", "x-mediabrowser-token", "x-plex-token"})
# 仅容器或宿主机本机可达、手机/平板客户端绝无法直连的主机名
_UNREACHABLE_CLIENT_HOSTS = frozenset({"localhost", "host.docker.internal", "gateway.docker.internal"})
# Docker 默认桥接网段（通常仅容器间互通）
_DOCKER_DEFAULT_BRIDGE = ipaddress.ip_network("172.17.0.0/16")
# 常见局域网本地域名后缀
_LAN_HOSTNAME_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal")


def sanitize_client_header_value(value: Optional[str], fallback: str, max_len: int = 80) -> str:
    """
    清理客户端传入的设备名与版本号，移除换行、双引号和控制字符以防止请求头注入。

    :param value: 客户端原始文本
    :param fallback: 清洗后为空时的默认回退值
    :param max_len: 允许保留的最大字符数
    :return: 可安全拼入 HTTP 请求头的纯文本
    """
    if not value:
        return fallback
    cleaned = "".join(
        ch for ch in str(value).strip()
        if ord(ch) >= 0x20 and ord(ch) != 0x7F and ch not in {'"', "\\", "\r", "\n"}
    ).strip()
    if not cleaned:
        return fallback
    return cleaned[:max_len]


def _credential_fernet_key() -> bytes:
    """
    从实例资源密钥派生 32 字节 Fernet 加密密钥。

    :return: URL 安全的 Base64 编码 Fernet 密钥
    """
    secret = str(get_runtime_setting("RESOURCE_SECRET_KEY") or get_runtime_setting("SECRET_KEY") or "moviepilot")
    return base64.urlsafe_b64encode(hashlib.sha256(f"mp-ms-cred:{secret}".encode("utf-8")).digest())


def _encrypt_token(token: str) -> str:
    """
    将媒体服务器用户令牌加密为持久化密文字符串。

    :param token: 原始媒体服务器访问令牌
    :return: Fernet 密文文本
    """
    return encrypt_message(token, _credential_fernet_key())


def _decrypt_token(cipher_text: str) -> Optional[str]:
    """
    解密持久化存储的媒体服务器用户令牌；密文损坏或密钥变更时返回 None。

    :param cipher_text: 已持久化的密文文本
    :return: 明文令牌；解密失败时返回 None
    """
    if not cipher_text:
        return None
    raw = decrypt(cipher_text.encode("utf-8"), _credential_fernet_key())
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True, slots=True)
class MediaServerUserCredential:
    """
    当前用户在单个媒体服务器实例上绑定的用户级访问凭据快照。

    :param server: 媒体服务器配置名称
    :param username: 所属 MoviePilot 本地用户名
    :param token: 解密后的媒体服务器用户访问令牌，禁止出现在 repr 与日志中
    :param remote_username: 媒体服务器侧对应的用户名，未提供时为 None
    :param remote_user_id: 媒体服务器侧对应的用户标识，未提供时为 None
    :param source: 凭据来源：manual 手动绑定或 auxiliary_auth 辅助登录自动捕获
    :param invalid: 上游返回 401 时标记为 True，提示用户重新绑定
    :param updated_at: 最近写入的 Unix 秒级时间戳
    """

    server: str
    username: str
    token: str = field(repr=False)
    remote_username: Optional[str] = None
    remote_user_id: Optional[str] = None
    source: Literal["manual", "auxiliary_auth"] = "manual"
    invalid: bool = False
    updated_at: int = 0


def _read_all_credentials_map() -> dict[str, Any]:
    """
    读取系统内部存储的媒体服务器用户凭据映射表。

    :return: 按用户名组织的凭据字典；配置尚未初始化或为空时返回空字典
    """
    try:
        raw = get_configured_system_config().get(SystemConfigKey.MediaServerUserCredentials)
    except RuntimeError:
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def get_user_mediaserver_credential(
    username: Optional[str],
    server: Optional[str],
    *,
    include_invalid: bool = False,
) -> Optional[MediaServerUserCredential]:
    """
    读取指定用户对目标媒体服务器绑定的加密用户令牌。

    :param username: MoviePilot 本地用户名
    :param server: 媒体服务器配置名称
    :param include_invalid: 是否同时返回已被标记为失效的记录，用于界面展示提示
    :return: 解密后的凭据对象；未绑定、失效（默认过滤）或解密失败时返回 None
    """
    if not username or not server:
        return None
    all_creds = _read_all_credentials_map()
    user_bucket = all_creds.get(username)
    if not isinstance(user_bucket, Mapping):
        return None
    entry = user_bucket.get(server)
    if not isinstance(entry, Mapping):
        return None
    is_invalid = bool(entry.get("invalid"))
    if is_invalid and not include_invalid:
        return None
    token_enc = str(entry.get("token_enc") or "")
    token = _decrypt_token(token_enc)
    if not token:
        if include_invalid:
            return MediaServerUserCredential(
                server=server,
                username=username,
                token="",
                remote_username=str(entry.get("remote_username") or "") or None,
                remote_user_id=str(entry.get("remote_user_id") or "") or None,
                source="auxiliary_auth" if entry.get("source") == "auxiliary_auth" else "manual",
                invalid=True,
                updated_at=int(entry.get("updated_at") or 0),
            )
        return None
    return MediaServerUserCredential(
        server=server,
        username=username,
        token=token,
        remote_username=str(entry.get("remote_username") or "") or None,
        remote_user_id=str(entry.get("remote_user_id") or "") or None,
        source="auxiliary_auth" if entry.get("source") == "auxiliary_auth" else "manual",
        invalid=is_invalid,
        updated_at=int(entry.get("updated_at") or 0),
    )


def save_user_mediaserver_credential(
    username: str,
    server: str,
    *,
    token: str,
    remote_username: Optional[str] = None,
    remote_user_id: Optional[str] = None,
    source: Literal["manual", "auxiliary_auth"] = "manual",
) -> MediaServerUserCredential:
    """
    原子加密持久化用户在单个媒体服务器上的用户级访问令牌。

    :param username: MoviePilot 本地用户名
    :param server: 媒体服务器配置名称
    :param token: 媒体服务器签发的用户访问令牌
    :param remote_username: 媒体服务器侧展示用户名
    :param remote_user_id: 媒体服务器侧用户标识
    :param source: 凭据来源：manual 或 auxiliary_auth
    :return: 已提交的凭据快照
    :raises ValueError: 用户名、服务器名或令牌为空时
    """
    clean_user = username.strip()
    clean_server = server.strip()
    clean_token = token.strip()
    if not clean_user or not clean_server or not clean_token:
        raise ValueError("用户名、媒体服务器名称与访问令牌不能为空")
    now = int(time.time())
    encrypted = _encrypt_token(clean_token)
    record: dict[str, Any] = {
        "token_enc": encrypted,
        "remote_username": (remote_username or "").strip() or None,
        "remote_user_id": (remote_user_id or "").strip() or None,
        "source": source,
        "invalid": False,
        "updated_at": now,
    }

    def _update(current: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        在短事务内仅更新当前用户目标服务器条目，不影响其他用户或服务器。
        """
        root = dict(current) if isinstance(current, Mapping) else {}
        existing_user = root.get(clean_user)
        user_map = dict(existing_user) if isinstance(existing_user, Mapping) else {}
        user_map[clean_server] = record
        root[clean_user] = user_map
        return record, root

    get_configured_system_config().update_atomically(SystemConfigKey.MediaServerUserCredentials, _update)
    return MediaServerUserCredential(
        server=clean_server,
        username=clean_user,
        token=clean_token,
        remote_username=record["remote_username"],
        remote_user_id=record["remote_user_id"],
        source=source,
        invalid=False,
        updated_at=now,
    )


def mark_user_mediaserver_credential_invalid(username: Optional[str], server: Optional[str]) -> None:
    """
    上游明确返回 401 时将该用户目标服务器的绑定令牌标记为失效。

    :param username: MoviePilot 本地用户名
    :param server: 媒体服务器配置名称
    """
    if not username or not server:
        return
    try:
        def _update(current: Any) -> tuple[bool, dict[str, Any]]:
            """
            仅在原条目存在时置位 invalid 标记。
            """
            root = dict(current) if isinstance(current, Mapping) else {}
            existing_user = root.get(username)
            if not isinstance(existing_user, Mapping) or server not in existing_user:
                return False, root
            user_map = dict(existing_user)
            entry = dict(user_map[server]) if isinstance(user_map[server], Mapping) else {}
            entry["invalid"] = True
            user_map[server] = entry
            root[username] = user_map
            return True, root

        get_configured_system_config().update_atomically(SystemConfigKey.MediaServerUserCredentials, _update)
    except RuntimeError:
        logger.debug("系统配置服务尚未初始化，跳过标记媒体服务器凭据失效")


def delete_user_mediaserver_credential(username: str, server: str) -> bool:
    """
    解绑并移除指定用户在目标媒体服务器上的持久化令牌。

    :param username: MoviePilot 本地用户名
    :param server: 媒体服务器配置名称
    :return: 原先存在并已删除返回 True，未绑定返回 False
    """
    clean_user = username.strip()
    clean_server = server.strip()
    if not clean_user or not clean_server:
        return False

    def _update(current: Any) -> tuple[bool, dict[str, Any]]:
        """
        从指定用户命名空间移除目标服务器条目并清理空字典。
        """
        root = dict(current) if isinstance(current, Mapping) else {}
        existing_user = root.get(clean_user)
        if not isinstance(existing_user, Mapping) or clean_server not in existing_user:
            return False, root
        user_map = dict(existing_user)
        user_map.pop(clean_server, None)
        if user_map:
            root[clean_user] = user_map
        else:
            root.pop(clean_user, None)
        return True, root

    return bool(
        get_configured_system_config().update_atomically(SystemConfigKey.MediaServerUserCredentials, _update)
    )


def _is_ugreen_media_service_config(config: Optional[Mapping[str, Any]]) -> bool:
    """
    判断绿联影视配置是否运行在 Emby 兼容媒体库服务模式（9443 端口或显式 media_service）。

    :param config: 媒体服务器原始配置字典
    :return: 运行于媒体库服务模式时返回 True
    """
    if not isinstance(config, Mapping):
        return False
    mode = str(config.get("mode") or "").strip().casefold()
    if mode in {"media_service", "media-server", "media_server", "emby"}:
        return True
    if mode in {"internal", "ugreen"}:
        return False
    host = str(config.get("host") or "").strip()
    if not host:
        return False
    parsed = urlsplit(host if "://" in host else f"http://{host}")
    return parsed.port == 9443


def resolve_server_sync_mode(
    server_type: Optional[str],
    credential: Optional[MediaServerUserCredential],
    config: Optional[Mapping[str, Any]] = None,
) -> MediaServerSyncMode:
    """
    结合服务器类型、配置模式与当前用户凭据计算生效的进度同步模式。

    :param server_type: 媒体服务器类型标识
    :param credential: 当前用户的有效媒体服务器凭据，未绑定或已失效时为 None
    :param config: 可选媒体服务器配置字典，用于识别绿联模式
    :return: session 实时会话、userdata 仅进度同步、scrobble 音乐记录或 none
    """
    normalized = str(server_type or "").strip().casefold()
    if normalized in {"emby", "jellyfin"}:
        return "session" if (credential and not credential.invalid and credential.token) else "userdata"
    if normalized == "plex":
        return "session"
    if normalized in BUILTIN_USER_SESSION_TYPES:
        return "session"
    if normalized == "ugreen":
        return "session" if _is_ugreen_media_service_config(config) else "none"
    if normalized == "navidrome":
        return "scrobble"
    return "none"


def list_user_mediaserver_bindings(
    username: str,
    configs: list[MediaServerConf],
) -> list[MediaServerAccountBinding]:
    """
    汇总当前用户对全部已启用媒体服务器的账号绑定状态与有效同步模式。

    :param username: 当前登录 MoviePilot 用户名
    :param configs: 已启用的媒体服务器配置列表
    :return: 按配置顺序排列的脱敏绑定状态列表
    """
    bindings: list[MediaServerAccountBinding] = []
    for conf in configs:
        if not conf.enabled or not conf.name or not conf.type:
            continue
        server_name = conf.name
        server_type = conf.type.strip().casefold()
        raw_cred = get_user_mediaserver_credential(username, server_name, include_invalid=True)
        valid_cred = raw_cred if (raw_cred and not raw_cred.invalid and raw_cred.token) else None
        sync_mode = resolve_server_sync_mode(server_type, valid_cred, conf.config)
        supports_binding = server_type in BINDABLE_MEDIA_SERVER_TYPES

        status: MediaServerBindingStatus
        if supports_binding:
            if valid_cred is not None:
                status = "bound"
            elif raw_cred is not None and raw_cred.invalid:
                status = "invalid"
            else:
                status = "unbound"
        elif sync_mode in {"session", "scrobble"}:
            status = "builtin"
        else:
            status = "unsupported"

        bindings.append(
            MediaServerAccountBinding(
                server=server_name,
                server_type=server_type,
                sync_mode=sync_mode,
                supports_binding=supports_binding,
                bound=valid_cred is not None,
                status=status,
                remote_username=raw_cred.remote_username if raw_cred else None,
                source=raw_cred.source if raw_cred else None,
                updated_at=raw_cred.updated_at if raw_cred else None,
            )
        )
    return bindings


def _classify_host_reachability(hostname: str) -> Optional[Literal["server_lan", "server_wan"]]:
    """
    判定目标主机名是否具备客户端直连条件，并区分局域网与外网类型。

    :param hostname: 已转为小写并去除末尾点号的主机名或 IP
    :return: `server_lan`、`server_wan`，或客户端不可达时返回 None
    """
    if not hostname or hostname in _UNREACHABLE_CLIENT_HOSTS:
        return None
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            # 无点号的单段主机名（如 http://emby:8096）属于容器内网别名
            return None
        if hostname.endswith(_LAN_HOSTNAME_SUFFIXES):
            return "server_lan"
        return "server_wan"
    if ip_obj.is_loopback or ip_obj.is_unspecified or ip_obj.is_multicast:
        return None
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _DOCKER_DEFAULT_BRIDGE:
        return None
    if ip_obj.is_private or ip_obj.is_link_local:
        return "server_lan"
    return "server_wan"


def classify_client_reachable_base_url(
    raw_url: Optional[str],
) -> Optional[tuple[str, Literal["server_lan", "server_wan"]]]:
    """
    校验媒体服务器配置地址是否允许下发给客户端直连，并返回规整后的根地址与网络类型。

    :param raw_url: 媒体服务器的 `host` 或 `play_host` 配置值
    :return: `(不含尾斜杠的规范根地址, server_lan 或 server_wan)`；不可达或含内嵌凭据时返回 None
    """
    cleaned = str(raw_url or "").strip()
    if not cleaned:
        return None
    if "://" not in cleaned:
        cleaned = f"http://{cleaned}"
    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        _ = parsed.port
    except ValueError:
        return None
    kind = _classify_host_reachability(parsed.hostname.casefold().rstrip("."))
    if kind is None:
        return None
    base_path = parsed.path.rstrip("/")
    normalized_base = urlunsplit((scheme, parsed.netloc, base_path, "", ""))
    return normalized_base, kind


def _strip_credential_query(url: str) -> str:
    """
    从媒体流地址中剥离可能存在的凭据查询参数，确保访问令牌仅经同源 HTTP Header 传输。

    :param url: 原始媒体流地址
    :return: 移除凭据查询参数后的完整地址
    """
    parsed = urlsplit(url)
    filtered = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.casefold() not in _CREDENTIAL_QUERY_KEYS
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(filtered), ""))


def _rewrite_stream_url_to_base(upstream_url: str, configured_host: Optional[str], target_base: str) -> Optional[str]:
    """
    将上游流 URL 的根地址安全改写到指定的客户端直连根地址（保留反代子路径与流查询参数，剔除凭据参数）。

    :param upstream_url: 媒体服务器适配器生成的原始流 URL
    :param configured_host: 媒体服务器配置的 `host` 原始根地址（用于剥离已有子路径前缀）
    :param target_base: 目标直连根地址（不含尾斜杠）
    :return: 改写并清洗凭据参数后的直连流 URL
    """
    sanitized_upstream = _strip_credential_query(upstream_url)
    up_parts = urlsplit(sanitized_upstream)
    target_parts = urlsplit(target_base)
    if up_parts.scheme.casefold() not in {"http", "https"} or not up_parts.path:
        return None
    rel_path = up_parts.path
    if configured_host:
        raw_cfg = configured_host.strip()
        cfg_parts = urlsplit(raw_cfg if "://" in raw_cfg else f"http://{raw_cfg}")
        cfg_prefix = cfg_parts.path.rstrip("/")
        if cfg_prefix and rel_path.startswith(f"{cfg_prefix}/"):
            rel_path = rel_path[len(cfg_prefix):]
    target_prefix = target_parts.path.rstrip("/")
    merged_path = f"{target_prefix}{rel_path}" if rel_path.startswith("/") else f"{target_prefix}/{rel_path}"
    return urlunsplit((target_parts.scheme, target_parts.netloc, merged_path, up_parts.query, ""))


def _build_direct_probe_url(server_type: str, target_base: str) -> Optional[str]:
    """
    为指定类型的媒体服务器构造无需鉴权的毫秒级连通性探测端点。

    :param server_type: 媒体服务器类型（emby / jellyfin / plex）
    :param target_base: 规整后的目标根地址（不含尾斜杠）
    :return: 探测 URL
    """
    base = target_base.rstrip("/")
    if server_type == "emby":
        return f"{base}/emby/System/Ping"
    if server_type == "jellyfin":
        return f"{base}/System/Ping"
    if server_type == "plex":
        return f"{base}/identity"
    return None


def _build_direct_auth_headers(
    server_type: str,
    user_token: str,
    upstream_headers: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """
    组装客户端直连媒体服务器所需的用户级同源请求头（绝不使用管理员 API Key）。

    :param server_type: 媒体服务器类型（emby / jellyfin / plex）
    :param user_token: 当前用户绑定的个人访问令牌
    :param upstream_headers: 可选会话级附加头（如 `X-Emby-Authorization` / `X-Plex-*`）
    :return: 仅限发往媒体服务器同源主机的请求头字典
    """
    headers: dict[str, str] = {}
    if upstream_headers:
        for key, value in upstream_headers.items():
            if key and value:
                headers[str(key)] = str(value)
    if server_type in {"emby", "jellyfin"}:
        headers["X-Emby-Token"] = user_token
        if server_type == "jellyfin" and "Authorization" not in headers and "X-Emby-Authorization" in headers:
            headers["Authorization"] = headers["X-Emby-Authorization"]
    elif server_type == "plex":
        headers["X-Plex-Token"] = user_token
    return headers


def build_client_playback_routes(
    *,
    content_url: str,
    strm_url: Optional[str] = None,
    server_type: Optional[str] = None,
    server_config: Optional[Mapping[str, Any]] = None,
    upstream_stream_url: Optional[str] = None,
    user_token: Optional[str] = None,
    upstream_headers: Optional[Mapping[str, str]] = None,
) -> list[MediaServerPlaybackRoute]:
    """
    按优先级生成客户端可选播放路由列表：STRM 直链 > 局域网直连 > 外网直连 > MoviePilot 后台中转。

    仅当媒体服务器属于支持直连的类型且当前用户已绑定有效个人令牌时，才签发 `server_lan` / `server_wan` 路由，
    绝不向客户端暴露管理员 API Key 或全局密码。

    :param content_url: 当前 MoviePilot 实例的代理流地址（恒为最终兜底路由）
    :param strm_url: 可选已净化的无凭据 STRM HTTP(S) 直链地址
    :param server_type: 媒体服务器类型标识
    :param server_config: 媒体服务器配置字典（读取 `host` 与 `play_host`）
    :param upstream_stream_url: 上游媒体服务器流 URL
    :param user_token: 当前用户已绑定的有效个人令牌；未绑定时为 None
    :param upstream_headers: 当前播放会话关联的设备/鉴权请求头
    :return: 按优先级升序排列的候选播放路由列表
    """
    routes: list[MediaServerPlaybackRoute] = []
    if strm_url:
        routes.append(
            MediaServerPlaybackRoute(
                kind="strm_direct",
                url=strm_url,
                probe_url=None,
                headers={},
                priority=10,
                requires_same_origin_auth=False,
            )
        )

    normalized_type = str(server_type or "").strip().casefold()
    clean_token = (user_token or "").strip()
    if (
        normalized_type in DIRECT_PLAY_SERVER_TYPES
        and clean_token
        and upstream_stream_url
        and isinstance(server_config, Mapping)
    ):
        raw_host = str(server_config.get("host") or "").strip() or None
        raw_play_host = str(server_config.get("play_host") or "").strip() or None
        seen_bases: set[str] = set()
        direct_candidates: list[MediaServerPlaybackRoute] = []
        direct_headers = _build_direct_auth_headers(normalized_type, clean_token, upstream_headers)
        for raw_candidate in (raw_host, raw_play_host):
            classified = classify_client_reachable_base_url(raw_candidate)
            if not classified:
                continue
            target_base, route_kind = classified
            if target_base in seen_bases:
                continue
            seen_bases.add(target_base)
            direct_url = _rewrite_stream_url_to_base(upstream_stream_url, raw_host, target_base)
            if not direct_url:
                continue
            direct_candidates.append(
                MediaServerPlaybackRoute(
                    kind=route_kind,
                    url=direct_url,
                    probe_url=_build_direct_probe_url(normalized_type, target_base),
                    headers=dict(direct_headers),
                    priority=20 if route_kind == "server_lan" else 30,
                    requires_same_origin_auth=False,
                )
            )
        direct_candidates.sort(key=lambda item: item.priority)
        routes.extend(direct_candidates)

    routes.append(
        MediaServerPlaybackRoute(
            kind="mp_proxy",
            url=content_url,
            probe_url=None,
            headers={},
            priority=100,
            requires_same_origin_auth=True,
        )
    )
    return routes


@dataclass(slots=True)
class PlaybackSessionState:
    """
    记录单次播放会话在后端的运行态上下文，用于关联拉流请求与保序上报。

    :param play_session_id: 对客户端公开的本次播放会话标识
    :param upstream_play_session_id: 上游媒体服务器签发或使用的会话标识
    :param user_id: 发起播放的 MoviePilot 用户主键
    :param username: 发起播放的 MoviePilot 用户名
    :param server: 媒体服务器配置名称
    :param server_type: 媒体服务器类型标识
    :param item_id: 媒体服务器条目标识
    :param media_source_id: 本次播放绑定的媒体源标识
    :param client_id: 客户端匿名安装标识
    :param device_name: 客户端设备名称
    :param platform: 客户端平台标识
    :param app_version: 客户端版本号
    :param play_method: 播放方式
    :param upstream_headers: 仅限发往源媒体服务器同源主机的拉流会话头
    :param started_reported: 是否已成功向上游发送起播事件
    :param stopped_reported: 是否已处理终态停止事件
    :param scrobbled: 是否已向 Plex 或 Navidrome 提交完播标记
    :param last_seq: 已接收的最大事件序号
    :param last_userdata_write_at: 上次执行 API Key UserData 写入的单调时钟秒数
    :param updated_at: 最近活跃的单调时钟秒数，用于过期清理
    """

    play_session_id: str
    upstream_play_session_id: str
    user_id: Optional[int]
    username: Optional[str]
    server: str
    server_type: str
    item_id: str
    media_source_id: Optional[str] = None
    client_id: Optional[str] = None
    device_name: Optional[str] = None
    platform: Optional[str] = None
    app_version: Optional[str] = None
    play_method: str = "DirectPlay"
    upstream_headers: dict[str, str] = field(default_factory=dict, repr=False)
    started_reported: bool = False
    stopped_reported: bool = False
    scrobbled: bool = False
    last_seq: int = -1
    last_userdata_write_at: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)


class PlaybackSessionRegistry:
    """
    线程安全的进程内播放会话注册表，负责流头关联、防乱序、终态幂等与兜底节流。
    """

    def __init__(self, *, max_entries: int = _MAX_PLAYBACK_SESSIONS, ttl_seconds: float = _SESSION_TTL_SECONDS) -> None:
        """
        初始化带容量和存活时间上限的播放会话注册表。

        :param max_entries: 最大并发保留会话数
        :param ttl_seconds: 单会话不活跃过期时间（秒）
        """
        self._lock = threading.RLock()
        self._sessions: dict[str, PlaybackSessionState] = {}
        self._max_entries = max(16, int(max_entries))
        self._ttl_seconds = max(60.0, float(ttl_seconds))

    def _evict_locked(self, now: float) -> None:
        """
        在持锁状态下清理过期或超出容量上限的最久未活跃会话。
        """
        expired = [
            key for key, sess in self._sessions.items()
            if (now - sess.updated_at) > self._ttl_seconds
        ]
        for key in expired:
            self._sessions.pop(key, None)
        while len(self._sessions) >= self._max_entries:
            oldest_key = min(self._sessions, key=lambda key: self._sessions[key].updated_at)
            self._sessions.pop(oldest_key, None)

    def register(
        self,
        *,
        play_session_id: Optional[str],
        upstream_play_session_id: Optional[str] = None,
        user_id: Optional[int],
        username: Optional[str],
        server: str,
        server_type: str,
        item_id: str,
        media_source_id: Optional[str] = None,
        client_id: Optional[str] = None,
        device_name: Optional[str] = None,
        platform: Optional[str] = None,
        app_version: Optional[str] = None,
        upstream_headers: Optional[Mapping[str, str]] = None,
    ) -> PlaybackSessionState:
        """
        在 stream-info 阶段登记本次播放会话上下文。

        :return: 新登记的会话状态对象
        """
        session_id = (play_session_id or "").strip() or uuid4().hex
        upstream_id = (upstream_play_session_id or "").strip() or session_id
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            state = PlaybackSessionState(
                play_session_id=session_id,
                upstream_play_session_id=upstream_id,
                user_id=user_id,
                username=username,
                server=server,
                server_type=server_type,
                item_id=str(item_id),
                media_source_id=media_source_id,
                client_id=client_id,
                device_name=device_name,
                platform=platform,
                app_version=app_version,
                upstream_headers=dict(upstream_headers or {}),
                updated_at=now,
            )
            self._sessions[session_id] = state
            return state

    def get_for_stream(
        self,
        play_session_id: Optional[str],
        *,
        user_id: Optional[int],
        server: Optional[str],
        item_id: str,
    ) -> Optional[PlaybackSessionState]:
        """
        校验归属并返回可供 `/stream` 代理透传的播放会话上下文。

        :param play_session_id: 流请求携带的播放会话标识
        :param user_id: 当前已认证用户主键
        :param server: 目标媒体服务器配置名称
        :param item_id: 目标媒体条目标识
        :return: 归属匹配的会话上下文；不存在、过期或不匹配时返回 None
        """
        if not play_session_id or not server:
            return None
        now = time.monotonic()
        with self._lock:
            sess = self._sessions.get(play_session_id)
            if not sess:
                return None
            if (now - sess.updated_at) > self._ttl_seconds:
                self._sessions.pop(play_session_id, None)
                return None
            if sess.server != server or sess.item_id != str(item_id):
                return None
            if sess.user_id is not None and user_id is not None and sess.user_id != user_id:
                return None
            sess.updated_at = now
            return sess

    def begin_progress_event(
        self,
        *,
        play_session_id: Optional[str],
        user_id: Optional[int],
        username: Optional[str],
        server: str,
        server_type: str,
        item_id: str,
        media_source_id: Optional[str],
        state: str,
        seq: Optional[int],
        client_id: Optional[str],
        device_name: Optional[str],
        platform: Optional[str],
        app_version: Optional[str],
        play_method: str,
    ) -> tuple[PlaybackSessionState, bool, bool]:
        """
        登记或续用进度事件对应的播放会话，并执行防乱序、终态幂等与兜底节流判定。

        :return: `(会话状态, 是否应跳过重复或乱序事件, 本次是否允许执行 UserData 写入)`
        """
        session_id = (play_session_id or "").strip() or uuid4().hex
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            sess = self._sessions.get(session_id)
            if sess is None or sess.server != server or sess.item_id != str(item_id):
                sess = PlaybackSessionState(
                    play_session_id=session_id,
                    upstream_play_session_id=session_id,
                    user_id=user_id,
                    username=username,
                    server=server,
                    server_type=server_type,
                    item_id=str(item_id),
                    media_source_id=media_source_id,
                    client_id=client_id,
                    device_name=device_name,
                    platform=platform,
                    app_version=app_version,
                    play_method=play_method,
                    updated_at=now,
                )
                self._sessions[session_id] = sess
            else:
                if media_source_id:
                    sess.media_source_id = media_source_id
                if client_id:
                    sess.client_id = client_id
                if device_name:
                    sess.device_name = device_name
                if platform:
                    sess.platform = platform
                if app_version:
                    sess.app_version = app_version
                if play_method:
                    sess.play_method = play_method
                sess.updated_at = now

            if sess.stopped_reported and state != "started":
                return sess, True, False
            if state == "started" and sess.stopped_reported:
                # 复用同一会话 ID 重新起播时重置生命周期状态
                sess.stopped_reported = False
                sess.started_reported = False
                sess.scrobbled = False
                sess.last_seq = -1
            if seq is not None and seq <= sess.last_seq:
                return sess, True, False
            if seq is not None:
                sess.last_seq = seq

            allow_userdata_write = (
                state in {"paused", "stopped"}
                or sess.last_userdata_write_at <= 0.0
                or (now - sess.last_userdata_write_at) >= USERDATA_FALLBACK_THROTTLE_SECONDS
            )
            return sess, False, allow_userdata_write

    def record_progress_outcome(
        self,
        session: PlaybackSessionState,
        *,
        state: str,
        started_sent: bool = False,
        scrobbled: bool = False,
        userdata_written: bool = False,
    ) -> None:
        """
        根据本次上游调用结果回写播放会话运行态。
        """
        now = time.monotonic()
        with self._lock:
            session.updated_at = now
            if started_sent:
                session.started_reported = True
            if scrobbled:
                session.scrobbled = True
            if userdata_written:
                session.last_userdata_write_at = now
            if state == "stopped":
                session.stopped_reported = True

    def reset(self) -> None:
        """
        清空全部内存播放会话状态，供测试隔离使用。
        """
        with self._lock:
            self._sessions.clear()


_playback_session_registry = PlaybackSessionRegistry()


def get_playback_session_registry() -> PlaybackSessionRegistry:
    """
    获取当前进程共享的播放会话注册表实例。

    :return: 单例播放会话注册表
    """
    return _playback_session_registry
