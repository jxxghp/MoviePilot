"""
自定义弹弹play 兼容弹幕代理、匹配、绑定与多级缓存应用服务。

本模块遵循应用层边界，不直接导入具体 HTTP Adapter、ORM 模型或数据库 Session。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib.parse import urlencode, urlsplit

from app.application.configuration import get_configured_system_config
from app.application.security.token import (
    decrypt_with_keys,
    derive_domain_fernet_keys,
    encrypt_message,
)
from app.runtime.cache import TTLCache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.danmaku import (
    DanmakuAnimeSearchItem,
    DanmakuAuthMode,
    DanmakuBangumiResponse,
    DanmakuBindingRequest,
    DanmakuCacheClearResult,
    DanmakuCacheStats,
    DanmakuConfigResponse,
    DanmakuConfigTestRequest,
    DanmakuConfigTestResult,
    DanmakuConfigUpdateRequest,
    DanmakuEpisodeCandidate,
    DanmakuHttpResponse,
    DanmakuMatchRequest,
    DanmakuMatchResponse,
)
from app.schemas.types import SystemConfigKey

# 手动绑定最多保留条数，超出后按最近更新时间淘汰最旧记录
_MAX_BINDINGS = 5000
# L1 元数据缓存有效期（命中结果 6 小时，空结果/故障 5 分钟防雪崩）
_META_CACHE_TTL_SECONDS = 6 * 60 * 60
_META_NEGATIVE_TTL_SECONDS = 5 * 60
# 紧凑载荷协议版本号
_SCHEMA_VERSION = 1
# 去除 XML 非法控制字符
_INVALID_XML_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 解析非严格 B 站 XML 弹幕节点的兜底表达式
_XML_D_NODE_RE = re.compile(r'<d\s+[^>]*p="([^"]+)"[^>]*>(.*?)</d>', re.DOTALL | re.IGNORECASE)
# 上游响应允许的最大字节数，防止异常上游导致内存耗尽（32 MB）
_MAX_UPSTREAM_BYTES = 32 * 1024 * 1024


class DanmakuError(RuntimeError):
    """
    弹幕代理服务的业务与上游请求异常。

    :param message: 已脱敏的中文错误说明
    :param status_code: 建议映射给 API 层的 HTTP 状态码
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        """
        初始化弹幕业务异常。

        :param message: 已脱敏的中文错误说明
        :param status_code: 建议返回给客户端的 HTTP 状态码
        """
        super().__init__(message)
        self.status_code = status_code


class DanmakuTransport(Protocol):
    """
    请求自定义弹弹play 兼容上游 API 的抽象 HTTP 传输端口。
    """

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
    ) -> DanmakuHttpResponse:
        """
        向上游弹幕 API 发起同步 HTTP 请求。

        :param method: HTTP 方法，支持 GET 与 POST
        :param url: 完整目标地址（含路径前缀与查询参数）
        :param headers: 已构造的鉴权与协商请求头，禁止携带 MoviePilot 令牌
        :param json_body: 可选 JSON 请求体
        :param timeout: 单次请求超时时间（秒）
        :param proxies: 可选全局代理配置
        :param verify_tls: 是否校验 HTTPS 证书
        :return: 统一的上游响应快照
        """
        ...


@dataclass(frozen=True, slots=True)
class DanmakuRuntimeConfig:
    """
    解密并规范化后的弹幕服务运行时配置快照。

    :param enabled: 是否启用弹幕代理服务
    :param api_base_url: 规范化后的自定义 API 根地址（无尾斜杠，保留路径前缀）
    :param auth_mode: 上游鉴权模式
    :param app_id: dandanplay_v2 鉴权 AppId
    :param app_secret: 解密后的 AppSecret，禁止出现在 repr 与日志中
    :param token_header: header_token 鉴权请求头名称
    :param token_prefix: header_token 鉴权令牌前缀
    :param token: 解密后的请求头令牌，禁止出现在 repr 与日志中
    :param with_related: 拉取弹幕时是否聚合关联第三方源
    :param ch_convert: 简繁转换设置（0/1/2）
    :param cache_ttl_hours: 磁盘缓存有效期（小时）
    :param cache_max_mb: 磁盘缓存容量上限（MB）
    :param timeout_seconds: 上游请求超时时间（秒）
    :param use_proxy: 是否复用全局代理
    :param verify_tls: 是否校验 HTTPS 证书
    """

    enabled: bool = False
    api_base_url: str = ""
    auth_mode: DanmakuAuthMode = "none"
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    token_header: str = "Authorization"
    token_prefix: str = "Bearer "
    token: str = field(default="", repr=False)
    with_related: bool = True
    ch_convert: int = 1
    cache_ttl_hours: int = 24
    cache_max_mb: int = 512
    timeout_seconds: int = 10
    use_proxy: bool = False
    verify_tls: bool = True

    @property
    def is_ready(self) -> bool:
        """
        判断弹幕服务是否已启用且具备合法上游地址。

        :return: 已启用且配置了上游根地址时返回 True
        """
        return self.enabled and bool(self.api_base_url)

    def source_hash(self, *, ch_convert: Optional[int] = None) -> str:
        """
        计算与上游地址和聚合选项绑定的缓存隔离哈希。

        :param ch_convert: 可选覆盖的简繁转换参数
        :return: 12 位十六进制稳定摘要
        """
        effective_convert = self.ch_convert if ch_convert is None else ch_convert
        raw = f"{self.api_base_url}|{int(self.with_related)}|{effective_convert}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class DanmakuCommentsResult:
    """
    单集弹幕拉取结果，供 API 端点直接输出 304 或零二次压缩 gzip 字节流。

    :param episode_id: 弹幕分集标识
    :param gz_bytes: 已压缩好的完整响应 JSON 字节流（缓存命中时直接来自磁盘文件）
    :param etag: 基于压缩内容派生的强 ETag 字符串
    :param cached: 本次请求是否命中后端 L2 磁盘缓存
    :param count: 归一化后的弹幕总条数
    :param updated_at: 缓存生成的 Unix 秒级时间戳
    """

    episode_id: int
    gz_bytes: bytes
    etag: str
    cached: bool
    count: int
    updated_at: int


def _danmaku_fernet_keys() -> list[bytes]:
    """
    从实例持久化密钥派生弹幕专用 32 字节 Fernet 密钥及兼容回退列表。

    :return: URL 安全 Base64 编码的 Fernet 密钥字节列表
    """
    return derive_domain_fernet_keys("mp-danmaku-cred", get_runtime_setting)


def _danmaku_fernet_key() -> bytes:
    """
    返回弹幕上游密钥的首选持久化 32 字节 Fernet 密钥。

    :return: URL 安全 Base64 编码的 Fernet 密钥字节
    """
    return _danmaku_fernet_keys()[0]


def _encrypt_secret(plain_text: str) -> str:
    """
    加密弹幕上游密钥，空文本返回空串。

    :param plain_text: 明文密钥
    :return: Fernet 密文字符串
    """
    cleaned = plain_text.strip()
    if not cleaned:
        return ""
    return encrypt_message(cleaned, _danmaku_fernet_key())


def _decrypt_secret(cipher_text: str) -> str:
    """
    解密持久化存储的弹幕上游密钥；损坏或密钥变更时返回空串。

    :param cipher_text: 已持久化的密文字符串
    :return: 解密后的明文密钥
    """
    if not cipher_text:
        return ""
    raw = decrypt_with_keys(cipher_text.encode("utf-8"), _danmaku_fernet_keys())
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def normalize_danmaku_base_url(raw_url: str, *, allow_empty: bool = False) -> str:
    """
    校验并规范化自定义弹弹play 兼容 API 根地址，保留路径前缀并去除尾斜杠。

    :param raw_url: 管理员输入的根地址
    :param allow_empty: 是否允许空地址（仅禁用状态可为空）
    :return: 规范化后的根地址
    :raises ValueError: 协议非法、缺少主机名或包含内嵌账号密码时
    """
    cleaned = (raw_url or "").strip().rstrip("/")
    if not cleaned:
        if allow_empty:
            return ""
        raise ValueError("请输入弹幕 API 根地址")
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("弹幕 API 根地址仅支持 http:// 或 https://")
    if not parts.hostname:
        raise ValueError("弹幕 API 根地址缺少有效主机名")
    if parts.username or parts.password:
        raise ValueError("弹幕 API 根地址不支持内嵌账号密码")
    if parts.query or parts.fragment:
        raise ValueError("弹幕 API 根地址不应包含查询参数或锚点")
    return cleaned


def redact_danmaku_url(url: str) -> str:
    """
    对上游弹幕地址做脱敏处理，隐藏路径前缀令牌与查询参数。

    :param url: 完整上游请求地址
    :return: 仅保留协议与主机端口的脱敏描述
    """
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return "<invalid-danmaku-url>"
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://{host}{port}/<redacted>"
    except ValueError:
        return "<invalid-danmaku-url>"


def _sanitize_header_name(name: str) -> str:
    """
    校验并清洗自定义令牌请求头名称，防止换行或非法字符注入。

    :param name: 原始请求头名称
    :return: 合法的 HTTP 请求头名称
    """
    cleaned = "".join(ch for ch in (name or "").strip() if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "Authorization"


def _sanitize_header_value(value: str) -> str:
    """
    移除请求头值中的控制字符与换行符，防止头注入。

    :param value: 原始请求头值
    :return: 清洗后的单行 ASCII 安全字符串
    """
    return "".join(ch for ch in value if ord(ch) >= 0x20 and ord(ch) != 0x7F and ch not in {"\r", "\n"})


def build_dandanplay_v2_signature(app_id: str, timestamp: str, api_path: str, app_secret: str) -> str:
    """
    按弹弹play 开放平台 v2 规范计算请求签名。

    公式：``Base64(SHA256(f"{app_id}{timestamp}{api_path}{app_secret}"))``。

    :param app_id: 开放平台 AppId
    :param timestamp: 秒级 Unix 时间戳字符串
    :param api_path: 不含查询串的接口路径（例如 ``/api/v2/match``）
    :param app_secret: 开放平台 AppSecret
    :return: Base64 编码的 SHA-256 签名文本
    """
    raw = f"{app_id}{timestamp}{api_path}{app_secret}".encode("utf-8")
    return base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")


def build_upstream_headers(
    config: DanmakuRuntimeConfig,
    api_path: str,
    *,
    now_ts: Optional[int] = None,
) -> dict[str, str]:
    """
    构造发往自定义上游弹幕服务的隔离请求头，绝不附带 MoviePilot 令牌或 Cookie。

    :param config: 解密后的弹幕运行时配置
    :param api_path: 不含根路径前缀和查询串的规范接口路径
    :param now_ts: 可选固定时间戳（供单测确定性校验签名）
    :return: 请求头映射
    """
    headers: dict[str, str] = {
        "Accept": "application/json, application/xml;q=0.9, text/xml;q=0.8",
        "User-Agent": "MoviePilot-Danmaku/1.0",
    }
    if config.auth_mode == "dandanplay_v2":
        ts = str(int(time.time()) if now_ts is None else int(now_ts))
        app_id = _sanitize_header_value(config.app_id.strip())
        signature = build_dandanplay_v2_signature(app_id, ts, api_path, config.app_secret)
        headers["X-AppId"] = app_id
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = signature
    elif config.auth_mode == "header_token" and config.token:
        header_name = _sanitize_header_name(config.token_header)
        header_value = _sanitize_header_value(f"{config.token_prefix}{config.token}")
        if header_value:
            headers[header_name] = header_value
    return headers


def _normalize_mode(raw_mode: int) -> int:
    """
    将上游弹幕模式归一为 1（滚动）、4（底部）或 5（顶部）。

    :param raw_mode: 上游原始模式编号
    :return: 规范化后的弹幕模式
    """
    if raw_mode in {4, 5}:
        return raw_mode
    return 1


def _normalize_color(raw_color: int) -> int:
    """
    约束 24 位 RGB 整数合法范围，异常值回退白色 16777215。

    :param raw_color: 上游原始十进制 RGB 颜色值
    :return: 0 至 16777215 之间的整数
    """
    if 0 <= raw_color <= 0xFFFFFF:
        return raw_color
    return 0xFFFFFF


def _clean_danmaku_text(raw_text: str) -> str:
    """
    清洗单条弹幕文本中的首尾控制字符与换行。

    :param raw_text: 上游原始弹幕文本
    :return: 适合客户端渲染的单行清洗文本
    """
    cleaned = _INVALID_XML_CHAR_RE.sub("", raw_text).replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:300]


def _extract_source_tag(raw_user_or_source: str) -> str:
    """
    从弹弹play 聚合源标识中提取简洁来源短标（如 bilibili、tencent 等）。

    :param raw_user_or_source: ``p`` 字段第四段原始字符串
    :return: 简短来源标识或空字符串
    """
    text = (raw_user_or_source or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for prefix in ("bilibili", "tencent", "iqiyi", "youku", "mgtv", "gamer", "dandan", "acfun", "nico"):
        if prefix in lowered:
            return prefix
    if text.startswith("[") and "]" in text:
        inner = text[1:text.index("]")].strip().lower()
        return inner[:24]
    return ""


def parse_upstream_comments(raw_bytes: bytes, content_type: str = "") -> list[list[Any]]:
    """
    双兼容解析弹弹play JSON 与 B 站 XML 弹幕响应，输出紧凑有序数组。

    每条元素格式为 ``[time_ms, mode, color_rgb, text, source]``。

    :param raw_bytes: 上游返回的原始响应字节
    :param content_type: 上游响应的 Content-Type
    :return: 按 ``time_ms`` 升序排列的紧凑弹幕列表
    """
    if not raw_bytes:
        return []
    text = raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    if not text:
        return []
    looks_xml = text.startswith("<") or "xml" in (content_type or "").lower()
    items = _parse_xml_comments(text) if looks_xml else _parse_json_comments(text)
    if not items and not looks_xml and "<d " in text:
        items = _parse_xml_comments(text)
    items.sort(key=lambda row: row[0])
    return items


def _parse_single_json_comment(entry: Any) -> Optional[list[Any]]:
    """
    解析单条紧凑数组或弹弹play 对象格式弹幕记录。

    :param entry: 上游原始单条弹幕对象或数组
    :return: 规范化后的 5 元组列表；非法条目返回 None
    """
    if isinstance(entry, list) and len(entry) >= 4:
        try:
            time_ms = int(entry[0])
            mode = _normalize_mode(int(entry[1]))
            color_rgb = _normalize_color(int(entry[2]))
            body = _clean_danmaku_text(str(entry[3]))
            source = str(entry[4])[:24] if len(entry) >= 5 and entry[4] is not None else ""
        except (TypeError, ValueError):
            return None
        return [time_ms, mode, color_rgb, body, source] if time_ms >= 0 and body else None
    if not isinstance(entry, Mapping):
        return None
    raw_p = str(entry.get("p") or "")
    body = _clean_danmaku_text(str(entry.get("m") or entry.get("text") or ""))
    parts = raw_p.split(",")
    if not body or len(parts) < 3:
        return None
    try:
        time_ms = round(float(parts[0]) * 1000)
        mode = _normalize_mode(int(float(parts[1])))
        color_rgb = _normalize_color(int(float(parts[2])))
    except (TypeError, ValueError):
        return None
    if time_ms < 0:
        return None
    source = _extract_source_tag(parts[3]) if len(parts) >= 4 else ""
    return [time_ms, mode, color_rgb, body, source]


def _parse_json_comments(text: str) -> list[list[Any]]:
    """
    解析标准弹弹play JSON 弹幕列表或已紧凑化的 JSON 数组。

    :param text: JSON 响应文本
    :return: 归一化后的紧凑弹幕列表
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    comments: Any = None
    if isinstance(payload, Mapping):
        if isinstance(payload.get("comments"), list):
            comments = payload["comments"]
        elif isinstance(payload.get("data"), Mapping) and isinstance(payload["data"].get("items"), list):
            comments = payload["data"]["items"]
    elif isinstance(payload, list):
        comments = payload
    if not isinstance(comments, list):
        return []
    return [item for entry in comments if (item := _parse_single_json_comment(entry)) is not None]


def _parse_xml_comments(text: str) -> list[list[Any]]:
    """
    安全解析 B 站标准 XML 弹幕文档（禁止 DOCTYPE/实体展开，兼容轻度非严格 XML）。

    :param text: XML 响应文本
    :return: 归一化后的紧凑弹幕列表
    """
    sanitized = _INVALID_XML_CHAR_RE.sub("", text)
    if "<!DOCTYPE" in sanitized.upper() or "<!ENTITY" in sanitized.upper():
        sanitized = re.sub(r"<!(?:DOCTYPE|ENTITY)[^>]*>", "", sanitized, flags=re.IGNORECASE)

    parsed_nodes: list[tuple[str, str]] = []
    try:
        root = ET.fromstring(sanitized)
        for node in root.iter("d"):
            p_attr = node.attrib.get("p")
            if p_attr:
                parsed_nodes.append((p_attr, node.text or ""))
    except ET.ParseError:
        for match in _XML_D_NODE_RE.finditer(sanitized):
            parsed_nodes.append((match.group(1), html.unescape(match.group(2))))

    normalized: list[list[Any]] = []
    for p_attr, raw_body in parsed_nodes:
        body = _clean_danmaku_text(raw_body)
        if not body:
            continue
        parts = p_attr.split(",")
        if len(parts) < 4:
            continue
        try:
            time_ms = round(float(parts[0]) * 1000)
            mode = _normalize_mode(int(float(parts[1])))
            # B 站 XML 的 p 字段格式：time,mode,fontSize,color,timestamp,pool,uid,dmid
            color_rgb = _normalize_color(int(float(parts[3])))
        except (TypeError, ValueError):
            continue
        if time_ms < 0:
            continue
        normalized.append([time_ms, mode, color_rgb, body, "xml"])
    return normalized


def _parse_int_optional(value: Any) -> Optional[int]:
    """
    安全转换可选整数值，忽略空值与非法文本。

    :param value: 原始字段值
    :return: 整数值或 None
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _binding_keys(
    *,
    server: Optional[str],
    item_id: Optional[str],
    tmdb_id: Optional[str],
    season_number: Optional[int],
    episode_number: Optional[int],
) -> tuple[Optional[str], Optional[str]]:
    """
    生成手动绑定的主查找键（条目级）与次查找键（TMDB 跨服复用级）。

    :param server: 媒体服务器配置名称
    :param item_id: 媒体服务器条目标识
    :param tmdb_id: 可选 TMDB 标识
    :param season_number: 可选季号
    :param episode_number: 可选集号
    :return: ``(primary_key, secondary_key)`` 元组
    """
    primary: Optional[str] = None
    clean_item = (item_id or "").strip()
    if clean_item:
        clean_server = (server or "local").strip() or "local"
        primary = f"item:{clean_server}:{clean_item}"

    secondary: Optional[str] = None
    clean_tmdb = (tmdb_id or "").strip()
    if clean_tmdb:
        if season_number is not None and episode_number is not None:
            secondary = f"tmdb:{clean_tmdb}:s{season_number}:e{episode_number}"
        elif episode_number is not None:
            secondary = f"tmdb:{clean_tmdb}:e{episode_number}"
        else:
            secondary = f"tmdb:{clean_tmdb}"
    return primary, secondary


def _read_raw_danmaku_config() -> dict[str, Any]:
    """
    从系统配置仓储安全读取弹幕配置原始字典。

    :return: 原始配置字典；未初始化或为空时返回空字典
    """
    try:
        raw = get_configured_system_config().get(SystemConfigKey.DanmakuConfig)
    except RuntimeError:
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def is_danmaku_enabled() -> bool:
    """
    快速判断当前实例是否已启用并配置了自定义弹幕代理服务。

    :return: 已启用且具备合法上游根地址时返回 True
    """
    raw = _read_raw_danmaku_config()
    if not raw or not bool(raw.get("enabled")):
        return False
    return bool(str(raw.get("api_base_url") or "").strip())


def _build_candidate_from_mapping(raw: Mapping[str, Any], *, fallback_anime_id: Optional[int] = None,
                                  fallback_anime_title: str = "", fallback_anime_type: Optional[str] = None
                                  ) -> Optional[DanmakuEpisodeCandidate]:
    """
    将上游 match/search/bangumi 返回的单集字典归一化为 ``DanmakuEpisodeCandidate``。

    :param raw: 上游原始单集字典
    :param fallback_anime_id: 上级作品标识兜底值
    :param fallback_anime_title: 上级作品标题兜底值
    :param fallback_anime_type: 上级作品类型兜底值
    :return: 规范化后的分集候选对象，缺少合法 episodeId 时返回 None
    """
    episode_id = _parse_int_optional(raw.get("episodeId") or raw.get("episode_id"))
    if not episode_id or episode_id <= 0:
        return None
    anime_id = _parse_int_optional(raw.get("animeId") or raw.get("anime_id")) or fallback_anime_id
    anime_title = str(raw.get("animeTitle") or raw.get("anime_title") or fallback_anime_title or "").strip()
    episode_title = str(raw.get("episodeTitle") or raw.get("episode_title") or "").strip()
    raw_ep_num = raw.get("episodeNumber") or raw.get("episode_number")
    episode_number = str(raw_ep_num).strip() if raw_ep_num not in (None, "") else None
    anime_type = str(
        raw.get("typeDescription") or raw.get("type") or raw.get("anime_type") or fallback_anime_type or ""
    ).strip() or None
    shift_sec = raw.get("shift")
    shift_ms = 0
    if isinstance(shift_sec, (int, float)) and not isinstance(shift_sec, bool):
        shift_ms = round(float(shift_sec) * 1000)
    elif raw.get("shift_ms") is not None:
        shift_ms = _parse_int_optional(raw.get("shift_ms")) or 0
    return DanmakuEpisodeCandidate(
        episode_id=episode_id,
        anime_id=anime_id,
        anime_title=anime_title,
        episode_title=episode_title,
        episode_number=episode_number,
        anime_type=anime_type,
        shift_ms=shift_ms,
    )


class DanmakuService:
    """
    自定义弹弹play 兼容弹幕代理、自动匹配、手动绑定与两级缓存服务。

    :param transport: 请求上游弹幕服务的 HTTP 传输端口实现
    :param cache_root: 可选本地磁盘缓存根目录覆盖（默认读取 ``CACHE_PATH``）
    :param clock: 可选时钟函数（供测试控制缓存过期与签名时间戳）
    """

    def __init__(
        self,
        transport: DanmakuTransport,
        *,
        cache_root: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        初始化弹幕代理服务与进程内单飞锁表。

        :param transport: 上游 HTTP 传输端口
        :param cache_root: 可选缓存根目录
        :param clock: 可选时间源函数
        """
        self._transport = transport
        self._cache_root_override = cache_root
        self._clock = clock
        self._meta_cache = TTLCache(region="danmaku_meta", maxsize=2048, ttl=_META_CACHE_TTL_SECONDS)
        self._singleflight_guard = threading.Lock()
        self._singleflight_locks: dict[str, threading.Lock] = {}

    def _cache_dir(self) -> Path:
        """
        解析弹幕 L2 磁盘压缩缓存根目录。

        :return: 弹幕磁盘缓存根目录路径
        """
        if self._cache_root_override is not None:
            return self._cache_root_override / "danmaku"
        configured = get_runtime_setting("CACHE_PATH")
        base = Path(str(configured)) if configured else (Path.cwd() / ".cache")
        return base / "danmaku"

    def _get_singleflight_lock(self, key: str) -> threading.Lock:
        """
        获取同一分集拉取键共享的进程内单飞锁，防止热门单集并发击穿上游。

        :param key: 单集缓存唯一键
        :return: 对应的互斥锁对象
        """
        with self._singleflight_guard:
            lock = self._singleflight_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._singleflight_locks[key] = lock
            return lock

    def get_runtime_config(self) -> DanmakuRuntimeConfig:
        """
        读取并解密当前弹幕运行时配置，缺失字段按文档默认值补齐。

        :return: 运行时配置快照
        """
        raw = _read_raw_danmaku_config()
        auth_mode_raw = str(raw.get("auth_mode") or "none").strip()
        auth_mode: DanmakuAuthMode
        if auth_mode_raw == "dandanplay_v2":
            auth_mode = "dandanplay_v2"
        elif auth_mode_raw == "header_token":
            auth_mode = "header_token"
        else:
            auth_mode = "none"
        raw_base = str(raw.get("api_base_url") or "").strip().rstrip("/")
        try:
            normalized_base = normalize_danmaku_base_url(raw_base, allow_empty=True)
        except ValueError:
            normalized_base = ""
        ch_convert_val = _parse_int_optional(raw.get("ch_convert"))
        ch_convert = max(0, min(2, 1 if ch_convert_val is None else ch_convert_val))
        return DanmakuRuntimeConfig(
            enabled=bool(raw.get("enabled", False)),
            api_base_url=normalized_base,
            auth_mode=auth_mode,
            app_id=str(raw.get("app_id") or "").strip(),
            app_secret=_decrypt_secret(str(raw.get("app_secret_enc") or "")),
            token_header=_sanitize_header_name(str(raw.get("token_header") or "Authorization")),
            token_prefix=str(raw.get("token_prefix") if raw.get("token_prefix") is not None else "Bearer "),
            token=_decrypt_secret(str(raw.get("token_enc") or "")),
            with_related=bool(raw.get("with_related", True)),
            ch_convert=ch_convert,
            cache_ttl_hours=max(1, min(720, _parse_int_optional(raw.get("cache_ttl_hours")) or 24)),
            cache_max_mb=max(16, min(10240, _parse_int_optional(raw.get("cache_max_mb")) or 512)),
            timeout_seconds=max(2, min(60, _parse_int_optional(raw.get("timeout_seconds")) or 10)),
            use_proxy=bool(raw.get("use_proxy", False)),
            verify_tls=bool(raw.get("verify_tls", True)),
        )

    def get_cache_stats(self, *, max_mb: Optional[int] = None) -> DanmakuCacheStats:
        """
        统计当前弹幕 L2 磁盘压缩缓存文件数量与总字节占用。

        :param max_mb: 可选覆盖的配额上限（MB）
        :return: 缓存统计摘要
        """
        quota = max_mb if max_mb is not None else self.get_runtime_config().cache_max_mb
        root = self._cache_dir()
        if not root.exists():
            return DanmakuCacheStats(file_count=0, total_bytes=0, max_mb=quota)
        file_count = 0
        total_bytes = 0
        for file_path in root.rglob("*.json.gz"):
            try:
                total_bytes += file_path.stat().st_size
                file_count += 1
            except OSError:
                continue
        return DanmakuCacheStats(file_count=file_count, total_bytes=total_bytes, max_mb=quota)

    def get_admin_config(self) -> DanmakuConfigResponse:
        """
        生成面向超级管理员的脱敏配置视图与缓存统计，不回传任何密钥明文或密文。

        :return: 管理员配置响应模型
        """
        raw = _read_raw_danmaku_config()
        cfg = self.get_runtime_config()
        return DanmakuConfigResponse(
            enabled=cfg.enabled,
            api_base_url=cfg.api_base_url,
            auth_mode=cfg.auth_mode,
            app_id=cfg.app_id,
            has_app_secret=bool(str(raw.get("app_secret_enc") or "").strip()),
            token_header=cfg.token_header,
            token_prefix=cfg.token_prefix,
            has_token=bool(str(raw.get("token_enc") or "").strip()),
            with_related=cfg.with_related,
            ch_convert=cfg.ch_convert,
            cache_ttl_hours=cfg.cache_ttl_hours,
            cache_max_mb=cfg.cache_max_mb,
            timeout_seconds=cfg.timeout_seconds,
            use_proxy=cfg.use_proxy,
            verify_tls=cfg.verify_tls,
            cache_stats=self.get_cache_stats(max_mb=cfg.cache_max_mb),
        )

    def update_config(self, request: DanmakuConfigUpdateRequest) -> DanmakuConfigResponse:
        """
        校验并原子保存弹幕上游服务配置，变更上游来源或聚合参数时隔离旧元数据缓存。

        :param request: 管理员提交的弹幕配置更新请求
        :return: 更新后的管理员脱敏配置视图
        :raises DanmakuError: 地址或鉴权配置非法时抛出 400 错误
        """
        try:
            normalized_url = normalize_danmaku_base_url(
                request.api_base_url,
                allow_empty=not request.enabled,
            )
        except ValueError as err:
            raise DanmakuError(str(err), status_code=400) from err

        previous_cfg = self.get_runtime_config()
        previous_hash = previous_cfg.source_hash()

        def _update(current: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            existing = dict(current) if isinstance(current, Mapping) else {}
            app_secret_enc = str(existing.get("app_secret_enc") or "")
            if request.clear_app_secret:
                app_secret_enc = ""
            elif request.app_secret is not None:
                app_secret_enc = _encrypt_secret(request.app_secret)

            token_enc = str(existing.get("token_enc") or "")
            if request.clear_token:
                token_enc = ""
            elif request.token is not None:
                token_enc = _encrypt_secret(request.token)

            record: dict[str, Any] = {
                "enabled": bool(request.enabled),
                "api_base_url": normalized_url,
                "auth_mode": request.auth_mode,
                "app_id": request.app_id.strip(),
                "app_secret_enc": app_secret_enc,
                "token_header": _sanitize_header_name(request.token_header),
                "token_prefix": request.token_prefix,
                "token_enc": token_enc,
                "with_related": bool(request.with_related),
                "ch_convert": int(request.ch_convert),
                "cache_ttl_hours": int(request.cache_ttl_hours),
                "cache_max_mb": int(request.cache_max_mb),
                "timeout_seconds": int(request.timeout_seconds),
                "use_proxy": bool(request.use_proxy),
                "verify_tls": bool(request.verify_tls),
            }
            return record, record

        get_configured_system_config().update_atomically(SystemConfigKey.DanmakuConfig, _update)
        next_cfg = self.get_runtime_config()
        if next_cfg.source_hash() != previous_hash:
            self._meta_cache.clear()
        return self.get_admin_config()

    def test_config(self, request: Optional[DanmakuConfigTestRequest] = None) -> DanmakuConfigTestResult:
        """
        使用表单覆盖项或已保存配置向上游发起轻量连通性与鉴权探测。

        :param request: 可选表单临时覆盖配置
        :return: 探测结果（含延迟与脱敏诊断文案）
        """
        base = self.get_runtime_config()
        raw_url = request.api_base_url if request and request.api_base_url is not None else base.api_base_url
        try:
            normalized_url = normalize_danmaku_base_url(raw_url, allow_empty=False)
        except ValueError as err:
            return DanmakuConfigTestResult(ok=False, latency_ms=0, message=str(err))

        probe_cfg = DanmakuRuntimeConfig(
            enabled=True,
            api_base_url=normalized_url,
            auth_mode=request.auth_mode if request and request.auth_mode is not None else base.auth_mode,
            app_id=request.app_id.strip() if request and request.app_id is not None else base.app_id,
            app_secret=(
                request.app_secret.strip()
                if request and request.app_secret is not None and request.app_secret.strip()
                else base.app_secret
            ),
            token_header=(
                _sanitize_header_name(request.token_header)
                if request and request.token_header is not None
                else base.token_header
            ),
            token_prefix=request.token_prefix if request and request.token_prefix is not None else base.token_prefix,
            token=(
                request.token.strip()
                if request and request.token is not None and request.token.strip()
                else base.token
            ),
            timeout_seconds=(
                request.timeout_seconds if request and request.timeout_seconds is not None else base.timeout_seconds
            ),
            use_proxy=request.use_proxy if request and request.use_proxy is not None else base.use_proxy,
            verify_tls=request.verify_tls if request and request.verify_tls is not None else base.verify_tls,
        )
        started = time.monotonic()
        try:
            resp = self._call_upstream(
                probe_cfg,
                "GET",
                "/api/v2/search/anime",
                query={"keyword": "test"},
            )
        except DanmakuError as err:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            return DanmakuConfigTestResult(ok=False, latency_ms=elapsed_ms, message=str(err))
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if resp.status_code == 200:
            try:
                data = json.loads(resp.text)
                if isinstance(data, Mapping) and data.get("success") is False:
                    err_msg = str(data.get("errorMessage") or "上游返回失败状态")
                    return DanmakuConfigTestResult(
                        ok=False,
                        latency_ms=elapsed_ms,
                        status_code=resp.status_code,
                        message=f"上游鉴权或参数未通过：{err_msg[:120]}",
                    )
            except json.JSONDecodeError:
                return DanmakuConfigTestResult(
                    ok=False,
                    latency_ms=elapsed_ms,
                    status_code=resp.status_code,
                    message="上游返回内容不是有效的弹弹play JSON 响应",
                )
            return DanmakuConfigTestResult(
                ok=True,
                latency_ms=elapsed_ms,
                status_code=resp.status_code,
                message=f"连接成功，响应耗时 {elapsed_ms} ms",
            )
        if resp.status_code in {401, 403}:
            return DanmakuConfigTestResult(
                ok=False,
                latency_ms=elapsed_ms,
                status_code=resp.status_code,
                message=f"上游鉴权失败（HTTP {resp.status_code}），请检查密钥或路径令牌",
            )
        return DanmakuConfigTestResult(
            ok=False,
            latency_ms=elapsed_ms,
            status_code=resp.status_code,
            message=f"上游服务异常响应（HTTP {resp.status_code}）",
        )

    def _require_ready_config(self) -> DanmakuRuntimeConfig:
        """
        读取并确保弹幕代理服务已启用，未启用时抛出明确错误。

        :return: 已就绪的弹幕运行时配置
        :raises DanmakuError: 弹幕服务未启用或未配置地址时抛出 503
        """
        cfg = self.get_runtime_config()
        if not cfg.is_ready:
            raise DanmakuError("弹幕代理服务未启用或尚未配置上游地址", status_code=503)
        return cfg

    def _call_upstream(
        self,
        config: DanmakuRuntimeConfig,
        method: str,
        api_path: str,
        *,
        query: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> DanmakuHttpResponse:
        """
        拼接保留路径前缀的上游 URL、附加隔离鉴权头并调用传输端口。

        :param config: 弹幕运行时配置
        :param method: HTTP 方法
        :param api_path: 以 ``/api/v2/`` 开头的接口路径（不含查询参数）
        :param query: 可选查询参数字典
        :param json_body: 可选 JSON 请求体
        :return: 上游响应快照
        :raises DanmakuError: 上游网络异常或响应体积超限时
        """
        clean_path = api_path if api_path.startswith("/") else f"/{api_path}"
        full_url = f"{config.api_base_url}{clean_path}"
        if query:
            filtered = {k: str(v) for k, v in query.items() if v is not None and str(v) != ""}
            if filtered:
                full_url = f"{full_url}?{urlencode(filtered)}"
        headers = build_upstream_headers(config, clean_path, now_ts=int(self._clock()))
        proxies = get_runtime_setting("PROXY") if config.use_proxy else None
        try:
            response = self._transport.request(
                method.upper(),
                full_url,
                headers=headers,
                json_body=json_body,
                timeout=config.timeout_seconds,
                proxies=proxies,
                verify_tls=config.verify_tls,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                f"请求弹幕上游失败：{type(err).__name__}，目标：{redact_danmaku_url(full_url)}"
            )
            raise DanmakuError("请求上游弹幕服务失败，请检查网络或配置", status_code=502) from err
        if len(response.content) > _MAX_UPSTREAM_BYTES:
            raise DanmakuError("上游弹幕响应体积超出安全上限", status_code=502)
        return response

    def _read_bindings_map(self) -> dict[str, dict[str, Any]]:
        """
        读取系统配置中保存的弹幕手动绑定映射表。

        :return: 键到绑定记录字典的映射
        """
        try:
            raw = get_configured_system_config().get(SystemConfigKey.DanmakuBindings)
        except RuntimeError:
            return {}
        if not isinstance(raw, Mapping):
            return {}
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, Mapping)}

    def _lookup_binding(self, request: DanmakuMatchRequest) -> Optional[DanmakuMatchResponse]:
        """
        按 ``item:{server}:{item_id}`` 优先、``tmdb:{tmdb_id}...`` 次选顺序查找手动绑定。

        :param request: 匹配请求元数据
        :return: 命中绑定时的匹配响应；未命中返回 None
        """
        primary, secondary = _binding_keys(
            server=request.server,
            item_id=request.item_id,
            tmdb_id=request.tmdb_id,
            season_number=request.season_number,
            episode_number=request.episode_number,
        )
        if not primary and not secondary:
            return None
        bindings = self._read_bindings_map()
        record = bindings.get(primary) if primary else None
        if record is None and secondary:
            record = bindings.get(secondary)
        if not record:
            return None
        candidate = _build_candidate_from_mapping(record)
        if not candidate:
            return None
        return DanmakuMatchResponse(
            matched=True,
            from_binding=True,
            shift_ms=candidate.shift_ms,
            episode=candidate,
            candidates=[candidate],
        )

    def save_binding(self, request: DanmakuBindingRequest, *, updated_by: str) -> DanmakuEpisodeCandidate:
        """
        持久化或覆盖媒体条目与弹幕分集的手动绑定与时间偏移，并控制总容量上限。

        :param request: 手动绑定请求参数
        :param updated_by: 执行操作的用户名
        :return: 已保存的分集候选信息
        :raises DanmakuError: 既未提供 item_id 也未提供 tmdb_id 时抛出 400
        """
        primary, secondary = _binding_keys(
            server=request.server,
            item_id=request.item_id,
            tmdb_id=request.tmdb_id,
            season_number=request.season_number,
            episode_number=request.episode_number,
        )
        if not primary and not secondary:
            raise DanmakuError("保存弹幕绑定至少需要提供 item_id 或 tmdb_id", status_code=400)
        now = int(self._clock())
        record: dict[str, Any] = {
            "episode_id": request.episode_id,
            "anime_id": request.anime_id,
            "anime_title": request.anime_title.strip(),
            "episode_title": request.episode_title.strip(),
            "shift_ms": int(request.shift_ms),
            "updated_by": updated_by,
            "updated_at": now,
        }

        def _update(current: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            root = {str(k): dict(v) for k, v in current.items() if isinstance(v, Mapping)} if isinstance(current, Mapping) else {}
            if primary:
                root[primary] = dict(record)
            if secondary:
                root[secondary] = dict(record)
            if len(root) > _MAX_BINDINGS:
                ordered = sorted(
                    root.items(),
                    key=lambda item: int(item[1].get("updated_at") or 0),
                )
                overflow = len(root) - _MAX_BINDINGS
                for old_key, _ in ordered[:overflow]:
                    root.pop(old_key, None)
            return record, root

        get_configured_system_config().update_atomically(SystemConfigKey.DanmakuBindings, _update)
        return DanmakuEpisodeCandidate(
            episode_id=request.episode_id,
            anime_id=request.anime_id,
            anime_title=request.anime_title.strip(),
            episode_title=request.episode_title.strip(),
            shift_ms=int(request.shift_ms),
        )

    def delete_binding(
        self,
        *,
        server: Optional[str] = None,
        item_id: Optional[str] = None,
        tmdb_id: Optional[str] = None,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
    ) -> bool:
        """
        删除指定媒体条目的弹幕手动绑定以恢复自动匹配。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体服务器条目标识
        :param tmdb_id: 可选 TMDB 标识
        :param season_number: 可选季号
        :param episode_number: 可选集号
        :return: 至少删除一条绑定记录时返回 True
        """
        primary, secondary = _binding_keys(
            server=server,
            item_id=item_id,
            tmdb_id=tmdb_id,
            season_number=season_number,
            episode_number=episode_number,
        )
        if not primary and not secondary:
            raise DanmakuError("删除弹幕绑定至少需要提供 item_id 或 tmdb_id", status_code=400)

        def _update(current: Any) -> tuple[bool, dict[str, Any]]:
            root = {str(k): dict(v) for k, v in current.items() if isinstance(v, Mapping)} if isinstance(current, Mapping) else {}
            removed = False
            if primary and primary in root:
                root.pop(primary, None)
                removed = True
            if secondary and secondary in root:
                root.pop(secondary, None)
                removed = True
            return removed, root

        return bool(get_configured_system_config().update_atomically(SystemConfigKey.DanmakuBindings, _update))

    def _build_match_filenames(self, request: DanmakuMatchRequest) -> list[str]:
        """
        依据媒体元数据构造有序的 ``/api/v2/match`` 候选文件名列表。

        :param request: 媒体匹配参数
        :return: 去重后的候选文件名列表（按命中率从高到低）
        """
        names: list[str] = []
        series = (request.series_name or "").strip()
        title = (request.title or "").strip()
        if series and request.season_number is not None and request.episode_number is not None:
            names.append(f"{series} S{request.season_number:02d}E{request.episode_number:02d}")
        elif series and request.episode_number is not None:
            names.append(f"{series} E{request.episode_number:02d}")
        elif title and request.year:
            names.append(f"{title} ({request.year})")
        elif title:
            names.append(title)

        raw_file = Path(request.file_name).name.strip() if request.file_name else ""
        if raw_file and raw_file not in names:
            names.append(raw_file)
        if series and series not in names and not names:
            names.append(series)
        return names

    def match(self, request: DanmakuMatchRequest) -> DanmakuMatchResponse:
        """
        按「手动绑定 → L1 匹配缓存 → ``POST /api/v2/match`` → ``GET /api/v2/search/episodes``」优先级匹配弹幕分集。

        :param request: 媒体匹配请求参数
        :return: 弹幕匹配结果与候选分集列表
        """
        cfg = self._require_ready_config()
        bound = self._lookup_binding(request)
        if bound is not None:
            return bound

        filenames = self._build_match_filenames(request)
        keyword = (request.series_name or request.title or "").strip()
        if not filenames and not keyword:
            return DanmakuMatchResponse(matched=False)

        fingerprint_src = json.dumps(
            {
                "s": cfg.source_hash(),
                "f": filenames,
                "k": keyword,
                "ep": request.episode_number,
                "dur": (request.duration_ms or 0) // 1000,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        cache_key = f"match:{hashlib.sha256(fingerprint_src.encode('utf-8')).hexdigest()}"
        cached = self._meta_cache.get(cache_key)
        if isinstance(cached, Mapping):
            return DanmakuMatchResponse.model_validate(cached)

        result = self._match_from_upstream(cfg, request, filenames, keyword)
        ttl = _META_CACHE_TTL_SECONDS if (result.matched or result.candidates) else _META_NEGATIVE_TTL_SECONDS
        self._meta_cache.set(cache_key, result.model_dump(mode="json"), ttl=ttl)
        return result

    def _match_from_upstream(
        self,
        cfg: DanmakuRuntimeConfig,
        request: DanmakuMatchRequest,
        filenames: list[str],
        keyword: str,
    ) -> DanmakuMatchResponse:
        """
        依次调用上游 ``/api/v2/match`` 与回退接口 ``/api/v2/search/episodes``。

        :param cfg: 弹幕运行时配置
        :param request: 匹配请求参数
        :param filenames: 候选文件名列表
        :param keyword: 兜底搜索剧名关键词
        :return: 归一化匹配结果
        """
        collected_candidates: list[DanmakuEpisodeCandidate] = []
        seen_episode_ids: set[int] = set()
        duration_sec = max(0, (request.duration_ms or 0) // 1000)
        file_size = max(0, request.file_size or 0)

        for file_name in filenames:
            resp = self._call_upstream(
                cfg,
                "POST",
                "/api/v2/match",
                json_body={
                    "fileName": file_name,
                    "fileHash": "0" * 32,
                    "fileSize": file_size,
                    "videoDuration": duration_sec,
                    "matchMode": "fileNameOnly",
                },
            )
            if resp.status_code != 200:
                continue
            try:
                payload = json.loads(resp.text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            matches = payload.get("matches")
            if not isinstance(matches, list):
                continue
            current_candidates: list[DanmakuEpisodeCandidate] = []
            for item in matches:
                if not isinstance(item, Mapping):
                    continue
                cand = _build_candidate_from_mapping(item)
                if cand and cand.episode_id not in seen_episode_ids:
                    seen_episode_ids.add(cand.episode_id)
                    collected_candidates.append(cand)
                    current_candidates.append(cand)
            if bool(payload.get("isMatched")) and current_candidates:
                primary = current_candidates[0]
                return DanmakuMatchResponse(
                    matched=True,
                    from_binding=False,
                    shift_ms=primary.shift_ms,
                    episode=primary,
                    candidates=collected_candidates,
                )

        if collected_candidates:
            return DanmakuMatchResponse(
                matched=len(collected_candidates) == 1,
                from_binding=False,
                shift_ms=collected_candidates[0].shift_ms if len(collected_candidates) == 1 else 0,
                episode=collected_candidates[0] if len(collected_candidates) == 1 else None,
                candidates=collected_candidates,
            )

        if keyword:
            fallback_candidates = self._search_episodes_candidates(
                cfg,
                keyword=keyword,
                episode=str(request.episode_number) if request.episode_number is not None else None,
            )
            if fallback_candidates:
                matched = len(fallback_candidates) == 1
                return DanmakuMatchResponse(
                    matched=matched,
                    from_binding=False,
                    shift_ms=fallback_candidates[0].shift_ms if matched else 0,
                    episode=fallback_candidates[0] if matched else None,
                    candidates=fallback_candidates,
                )

        return DanmakuMatchResponse(matched=False)

    def _search_episodes_candidates(
        self,
        cfg: DanmakuRuntimeConfig,
        *,
        keyword: str,
        episode: Optional[str] = None,
    ) -> list[DanmakuEpisodeCandidate]:
        """
        调用上游 ``GET /api/v2/search/episodes`` 召回候选分集列表。

        :param cfg: 弹幕运行时配置
        :param keyword: 作品名称关键词
        :param episode: 可选集号文本
        :return: 扁平化候选分集列表
        """
        query: dict[str, Any] = {"anime": keyword}
        if episode:
            query["episode"] = episode
        try:
            resp = self._call_upstream(cfg, "GET", "/api/v2/search/episodes", query=query)
        except DanmakuError:
            return []
        if resp.status_code != 200:
            return []
        try:
            payload = json.loads(resp.text)
        except json.JSONDecodeError:
            return []
        animes = payload.get("animes") if isinstance(payload, Mapping) else None
        if not isinstance(animes, list):
            return []
        candidates: list[DanmakuEpisodeCandidate] = []
        seen: set[int] = set()
        for anime in animes:
            if not isinstance(anime, Mapping):
                continue
            anime_id = _parse_int_optional(anime.get("animeId"))
            anime_title = str(anime.get("animeTitle") or "").strip()
            anime_type = str(anime.get("typeDescription") or anime.get("type") or "").strip() or None
            episodes = anime.get("episodes")
            if not isinstance(episodes, list):
                continue
            for ep in episodes:
                if not isinstance(ep, Mapping):
                    continue
                cand = _build_candidate_from_mapping(
                    ep,
                    fallback_anime_id=anime_id,
                    fallback_anime_title=anime_title,
                    fallback_anime_type=anime_type,
                )
                if cand and cand.episode_id not in seen:
                    seen.add(cand.episode_id)
                    candidates.append(cand)
        return candidates

    def search(
        self,
        keyword: str,
        *,
        episode: Optional[str] = None,
        anime_type: Optional[str] = None,
    ) -> list[DanmakuAnimeSearchItem]:
        """
        供播放页手动搜剧与选集使用，优先携带 episode 时调用 ``/search/episodes``，否则调用 ``/search/anime``。

        :param keyword: 搜索关键词
        :param episode: 可选集号筛选
        :param anime_type: 可选作品类型筛选
        :return: 匹配的作品及可选分集列表
        """
        cfg = self._require_ready_config()
        clean_keyword = keyword.strip()
        if not clean_keyword:
            return []
        cache_key = f"search:{cfg.source_hash()}:{hashlib.sha256(f'{clean_keyword}|{episode or ''}|{anime_type or ''}'.encode('utf-8')).hexdigest()}"
        cached = self._meta_cache.get(cache_key)
        if isinstance(cached, list):
            return [DanmakuAnimeSearchItem.model_validate(item) for item in cached]

        if episode:
            resp = self._call_upstream(
                cfg,
                "GET",
                "/api/v2/search/episodes",
                query={"anime": clean_keyword, "episode": episode.strip()},
            )
        else:
            query: dict[str, Any] = {"keyword": clean_keyword}
            if anime_type:
                query["type"] = anime_type.strip()
            resp = self._call_upstream(cfg, "GET", "/api/v2/search/anime", query=query)

        if resp.status_code != 200:
            raise DanmakuError(f"上游弹幕搜索失败（HTTP {resp.status_code}）", status_code=502)
        try:
            payload = json.loads(resp.text)
        except json.JSONDecodeError as err:
            raise DanmakuError("上游弹幕搜索返回了无效 JSON", status_code=502) from err

        raw_animes = payload.get("animes") if isinstance(payload, Mapping) else None
        results = self._parse_search_animes(raw_animes)

        ttl = _META_CACHE_TTL_SECONDS if results else _META_NEGATIVE_TTL_SECONDS
        self._meta_cache.set(cache_key, [item.model_dump(mode="json") for item in results], ttl=ttl)
        return results

    @staticmethod
    def _parse_search_animes(raw_animes: Any) -> list[DanmakuAnimeSearchItem]:
        """
        将上游搜索响应中的作品数组归一化为结构化搜索条目列表。

        :param raw_animes: 上游 ``animes`` 原始字段值
        :return: 规范化后的作品列表
        """
        if not isinstance(raw_animes, list):
            return []
        results: list[DanmakuAnimeSearchItem] = []
        for anime in raw_animes:
            if not isinstance(anime, Mapping):
                continue
            anime_id = _parse_int_optional(anime.get("animeId"))
            if not anime_id:
                continue
            anime_title = str(anime.get("animeTitle") or "").strip()
            a_type = str(anime.get("typeDescription") or anime.get("type") or "").strip() or None
            ep_count = _parse_int_optional(anime.get("episodeCount"))
            raw_eps = anime.get("episodes")
            episodes = [
                cand
                for ep in (raw_eps if isinstance(raw_eps, list) else [])
                if isinstance(ep, Mapping)
                and (
                    cand := _build_candidate_from_mapping(
                        ep,
                        fallback_anime_id=anime_id,
                        fallback_anime_title=anime_title,
                        fallback_anime_type=a_type,
                    )
                )
                is not None
            ]
            results.append(
                DanmakuAnimeSearchItem(
                    anime_id=anime_id,
                    anime_title=anime_title,
                    anime_type=a_type,
                    episode_count=ep_count or (len(episodes) if episodes else None),
                    episodes=episodes,
                )
            )
        return results

    def get_bangumi(self, anime_id: int) -> DanmakuBangumiResponse:
        """
        查询指定作品的完整分集目录（带 L1 缓存）。

        :param anime_id: 上游作品标识
        :return: 作品详情与有序分集列表
        """
        cfg = self._require_ready_config()
        cache_key = f"bangumi:{cfg.source_hash()}:{anime_id}"
        cached = self._meta_cache.get(cache_key)
        if isinstance(cached, Mapping):
            return DanmakuBangumiResponse.model_validate(cached)

        resp = self._call_upstream(cfg, "GET", f"/api/v2/bangumi/{anime_id}")
        if resp.status_code == 404:
            raise DanmakuError("未找到对应作品分集信息", status_code=404)
        if resp.status_code != 200:
            raise DanmakuError(f"获取弹幕分集目录失败（HTTP {resp.status_code}）", status_code=502)
        try:
            payload = json.loads(resp.text)
        except json.JSONDecodeError as err:
            raise DanmakuError("上游分集目录响应格式无效", status_code=502) from err

        bangumi = payload.get("bangumi") if isinstance(payload, Mapping) else None
        if not isinstance(bangumi, Mapping):
            raise DanmakuError("上游未返回有效的作品分集信息", status_code=404)
        anime_title = str(bangumi.get("animeTitle") or "").strip()
        anime_type = str(bangumi.get("typeDescription") or bangumi.get("type") or "").strip() or None
        episodes: list[DanmakuEpisodeCandidate] = []
        raw_eps = bangumi.get("episodes")
        if isinstance(raw_eps, list):
            for ep in raw_eps:
                if isinstance(ep, Mapping):
                    cand = _build_candidate_from_mapping(
                        ep,
                        fallback_anime_id=anime_id,
                        fallback_anime_title=anime_title,
                        fallback_anime_type=anime_type,
                    )
                    if cand:
                        episodes.append(cand)
        result = DanmakuBangumiResponse(
            anime_id=anime_id,
            anime_title=anime_title,
            anime_type=anime_type,
            episodes=episodes,
        )
        self._meta_cache.set(cache_key, result.model_dump(mode="json"), ttl=_META_CACHE_TTL_SECONDS)
        return result

    def get_comments(
        self,
        episode_id: int,
        *,
        ch_convert: Optional[int] = None,
        force_refresh: bool = False,
    ) -> DanmakuCommentsResult:
        """
        获取单集归一化紧凑弹幕载荷，结合单飞锁、L2 磁盘 gzip 缓存与 LRU 配额淘汰。

        :param episode_id: 上游弹幕分集标识
        :param ch_convert: 可选简繁转换覆盖参数（0/1/2）
        :param force_refresh: 为 True 时忽略本地磁盘缓存强制回源刷新
        :return: 含压缩字节流与强 ETag 的弹幕载荷结果
        """
        cfg = self._require_ready_config()
        effective_convert = cfg.ch_convert if ch_convert is None else max(0, min(2, int(ch_convert)))
        source_hash = cfg.source_hash(ch_convert=effective_convert)
        cache_path = (
            self._cache_dir()
            / source_hash
            / f"{episode_id}_r{1 if cfg.with_related else 0}_c{effective_convert}.json.gz"
        )
        ttl_seconds = cfg.cache_ttl_hours * 3600

        if not force_refresh:
            hit = self._read_l2_cache(cache_path, episode_id, ttl_seconds)
            if hit is not None:
                return hit

        sf_key = f"{source_hash}:{episode_id}:{int(cfg.with_related)}:{effective_convert}"
        with self._get_singleflight_lock(sf_key):
            if not force_refresh:
                hit = self._read_l2_cache(cache_path, episode_id, ttl_seconds)
                if hit is not None:
                    return hit
            return self._fetch_and_cache_comments(cfg, episode_id, effective_convert, cache_path)

    def _read_l2_cache(
        self,
        cache_path: Path,
        episode_id: int,
        ttl_seconds: int,
    ) -> Optional[DanmakuCommentsResult]:
        """
        读取并校验单集 L2 ``.json.gz`` 磁盘缓存，命中时刷新访问时间用于 LRU。

        :param cache_path: 磁盘缓存文件路径
        :param episode_id: 分集标识
        :param ttl_seconds: 允许的最大缓存寿命（秒）
        :return: 有效缓存结果；未命中或已过期时返回 None
        """
        if not cache_path.is_file():
            return None
        try:
            stat = cache_path.stat()
            now = self._clock()
            if now - stat.st_mtime > ttl_seconds:
                return None
            gz_bytes = cache_path.read_bytes()
            if not gz_bytes:
                return None
            try:
                os.utime(cache_path, (now, stat.st_mtime))
            except OSError:
                pass
            etag = f'"{hashlib.sha256(gz_bytes).hexdigest()[:16]}"'
            return DanmakuCommentsResult(
                episode_id=episode_id,
                gz_bytes=gz_bytes,
                etag=etag,
                cached=True,
                count=0,
                updated_at=int(stat.st_mtime),
            )
        except OSError:
            return None

    def _fetch_and_cache_comments(
        self,
        cfg: DanmakuRuntimeConfig,
        episode_id: int,
        ch_convert: int,
        cache_path: Path,
    ) -> DanmakuCommentsResult:
        """
        回源拉取单集弹幕、双格式解析归一化、原子落盘 gzip 缓存并执行 LRU 配额淘汰。

        :param cfg: 弹幕运行时配置
        :param episode_id: 分集标识
        :param ch_convert: 生效的简繁转换参数
        :param cache_path: L2 磁盘缓存目标路径
        :return: 弹幕结果（含 ``cached=False`` 的首次响应流与磁盘缓存一致的 ETag）
        """
        resp = self._call_upstream(
            cfg,
            "GET",
            f"/api/v2/comment/{episode_id}",
            query={
                "withRelated": "true" if cfg.with_related else "false",
                "chConvert": str(ch_convert),
            },
        )
        if resp.status_code == 404:
            raise DanmakuError("上游未找到该分集的弹幕数据", status_code=404)
        if resp.status_code != 200:
            raise DanmakuError(f"拉取上游弹幕失败（HTTP {resp.status_code}）", status_code=502)

        items = parse_upstream_comments(resp.content, resp.content_type)
        now = int(self._clock())
        cached_envelope = {
            "success": True,
            "data": {
                "schema_version": _SCHEMA_VERSION,
                "episode_id": episode_id,
                "count": len(items),
                "cached": True,
                "updated_at": now,
                "items": items,
            },
        }
        cached_json = json.dumps(cached_envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cached_gz = gzip.compress(cached_json, compresslevel=6)
        etag = f'"{hashlib.sha256(cached_gz).hexdigest()[:16]}"'

        self._write_l2_cache(cache_path, cached_gz, now, cfg.cache_max_mb)

        fresh_envelope = {
            "success": True,
            "data": {
                "schema_version": _SCHEMA_VERSION,
                "episode_id": episode_id,
                "count": len(items),
                "cached": False,
                "updated_at": now,
                "items": items,
            },
        }
        fresh_json = json.dumps(fresh_envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        fresh_gz = gzip.compress(fresh_json, compresslevel=6)
        return DanmakuCommentsResult(
            episode_id=episode_id,
            gz_bytes=fresh_gz,
            etag=etag,
            cached=False,
            count=len(items),
            updated_at=now,
        )

    def _write_l2_cache(self, cache_path: Path, gz_bytes: bytes, timestamp: int, max_mb: int) -> None:
        """
        原子写入 ``.json.gz`` 缓存文件并按 LRU 淘汰超出磁盘配额的旧文件。

        :param cache_path: 缓存文件目标路径
        :param gz_bytes: 已压缩字节内容
        :param timestamp: 缓存写入的 Unix 秒级时间戳
        :param max_mb: 允许占用的最大磁盘配额（MB）
        """
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp_path.write_bytes(gz_bytes)
            os.utime(tmp_path, (timestamp, timestamp))
            os.replace(tmp_path, cache_path)
        except OSError as err:
            logger.warning(f"写入弹幕磁盘缓存失败：{err}")
            return
        self._enforce_lru_quota(max_bytes=max_mb * 1024 * 1024, protected_path=cache_path)

    def _enforce_lru_quota(self, *, max_bytes: int, protected_path: Optional[Path] = None) -> None:
        """
        扫描弹幕磁盘缓存目录，超出配额时按最近访问时间（atime/mtime）从旧到新淘汰。

        :param max_bytes: 最大允许磁盘字节数
        :param protected_path: 本次刚刚写入的文件路径，优先保留不被淘汰
        """
        root = self._cache_dir()
        if not root.exists():
            return
        entries: list[tuple[float, int, Path]] = []
        total_bytes = 0
        for file_path in root.rglob("*.json.gz"):
            try:
                st = file_path.stat()
                total_bytes += st.st_size
                entries.append((max(st.st_atime, st.st_mtime), st.st_size, file_path))
            except OSError:
                continue
        if total_bytes <= max_bytes:
            return
        entries.sort(key=lambda item: item[0])
        for _, size, file_path in entries:
            if total_bytes <= max_bytes:
                break
            if protected_path is not None and file_path == protected_path:
                continue
            try:
                file_path.unlink(missing_ok=True)
                total_bytes -= size
            except OSError:
                continue

    def clear_cache(self) -> DanmakuCacheClearResult:
        """
        清空所有 L1 元数据缓存与 L2 磁盘弹幕压缩文件。

        :return: 清理的文件数量与释放字节数统计
        """
        self._meta_cache.clear()
        root = self._cache_dir()
        removed_files = 0
        freed_bytes = 0
        if root.exists():
            for file_path in list(root.rglob("*.json.gz")):
                try:
                    size = file_path.stat().st_size
                    file_path.unlink(missing_ok=True)
                    removed_files += 1
                    freed_bytes += size
                except OSError:
                    continue
        return DanmakuCacheClearResult(removed_files=removed_files, freed_bytes=freed_bytes)
