"""宿主工具输入、结果与异常的递归脱敏摘要。"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from itertools import islice
from typing import Any

from pydantic import BaseModel


REDACTED_VALUE = "***"
_MAX_DEPTH = 8
_MAX_ITEMS = 100
_MAX_TEXT_CHARS = 16 * 1024
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "api_token",
    "authorization",
    "client_secret",
    "cookie",
    "passkey",
    "passwd",
    "password",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "token",
}
_ACRONYM_BOUNDARY_PATTERN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_CASE_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_COMPACT_SECRET_SUFFIXES = (
    "apikey",
    "authorization",
    "cookie",
    "passkey",
    "passwd",
    "password",
    "privatekey",
    "pwd",
    "secret",
    "secretaccesskey",
    "secretkey",
    "token",
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"x-api-key|api[_-]?key|api[_-]?token)\s*[:=]\s*)[^\r\n]+"
)
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
_PRIVATE_KEY_OPEN_PATTERN = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*",
    re.DOTALL,
)


def _normalize_key(key: Any) -> str:
    """把结构化字段名转换为可比较的 snake_case。"""
    try:
        text = str(key)
    except Exception:
        return ""
    text = _ACRONYM_BOUNDARY_PATTERN.sub("_", text.strip())
    text = _CAMEL_CASE_BOUNDARY_PATTERN.sub("_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _is_secret_key(key: Any) -> bool:
    """判断字段是否承载凭据原值，避免误伤 token_count 等统计字段。"""
    normalized = _normalize_key(key)
    if normalized in _SECRET_KEYS:
        return True
    compact = normalized.replace("_", "")
    return compact.endswith(_COMPACT_SECRET_SUFFIXES)


def _advance_quote_context(
    value: str,
    start: int,
    end: int,
    active_quote: str,
) -> str:
    """线性跟踪文本片段内尚未闭合的单双引号。"""
    cursor = start
    while cursor < end:
        char = value[cursor]
        if char == "\\":
            cursor += 2
            continue
        if active_quote:
            if char == active_quote:
                active_quote = ""
        elif char in ("\"", "'"):
            active_quote = char
        cursor += 1
    return active_quote


def _is_assignment_key_start(char: str) -> bool:
    """判断字符是否可作为 ASCII 赋值字段名的起点。"""
    return char == "_" or char.isascii() and char.isalnum()


def _is_assignment_key_char(char: str) -> bool:
    """判断字符是否属于常见日志或配置字段名。"""
    return _is_assignment_key_start(char) or char in (".", "-")


def _quote_wrapper_at(value: str, start: int) -> tuple[str, int, int]:
    """识别引号包装，并返回引号、反斜杠数量和已扫描位置。"""
    quote_at = start
    while quote_at < len(value) and value[quote_at] == "\\":
        quote_at += 1
    if quote_at >= len(value) or value[quote_at] not in ("\"", "'"):
        return "", quote_at - start, quote_at
    return value[quote_at], quote_at - start, quote_at + 1


def _quote_wrapper_end(
    value: str,
    content_start: int,
    quote: str,
    slash_count: int,
) -> int:
    """查找同层引号结束位置，更深层转义引号视为值内容。"""
    cursor = content_start
    while cursor < len(value):
        if value[cursor] == "\\":
            slash_start = cursor
            while cursor < len(value) and value[cursor] == "\\":
                cursor += 1
            if (
                cursor < len(value)
                and value[cursor] == quote
                and cursor - slash_start >= slash_count
                and (cursor - slash_start - slash_count)
                % (2 * (slash_count + 1))
                == 0
            ):
                return cursor + 1
            if cursor < len(value):
                cursor += 1
            continue
        if slash_count == 0 and value[cursor] == quote:
            return cursor + 1
        cursor += 1
    return len(value)


def _find_assignment_header(
    value: str,
    search_from: int,
) -> tuple[int, str, int] | None:
    """单向查找下一个赋值头，返回起点、字段名和值起点。"""
    cursor = search_from
    while cursor < len(value):
        match_start = cursor
        if match_start > 0 and value[match_start - 1].isascii() and value[
            match_start - 1
        ].isalnum():
            cursor += 1
            continue

        quote, slash_count, key_start = _quote_wrapper_at(value, cursor)
        if not quote:
            if slash_count:
                cursor = key_start
                continue
            key_start = cursor
        if key_start >= len(value) or not _is_assignment_key_start(
            value[key_start]
        ):
            cursor += 1
            continue

        key_end = key_start + 1
        while key_end < len(value) and _is_assignment_key_char(value[key_end]):
            key_end += 1

        header_end = key_end
        if quote:
            closing_quote_at = header_end
            while (
                closing_quote_at < len(value)
                and value[closing_quote_at] == "\\"
            ):
                closing_quote_at += 1
            if (
                closing_quote_at >= len(value)
                or value[closing_quote_at] != quote
                or closing_quote_at - header_end != slash_count
            ):
                # 引号不是字段名的一部分时，从引号内首字符重试一次。
                cursor = key_start
                continue
            header_end = closing_quote_at + 1
        while header_end < len(value) and value[header_end].isspace():
            header_end += 1
        if header_end >= len(value) or value[header_end] not in (":", "="):
            cursor = key_end
            continue
        header_end += 1
        while header_end < len(value) and value[header_end].isspace():
            header_end += 1
        return match_start, value[key_start:key_end], header_end
    return None


def _assignment_value_end(
    value: str,
    match_start: int,
    value_start: int,
    active_quote: str,
) -> int:
    """返回凭据值的安全消费边界，并保留外层文本结构。"""
    if value_start >= len(value):
        return value_start

    quote, slash_count, content_start = _quote_wrapper_at(value, value_start)
    if quote and slash_count == 0 and quote == active_quote:
        return value_start
    if quote:
        return _quote_wrapper_end(
            value,
            content_start,
            quote,
            slash_count,
        )

    delimiters = ",;}\r\n"
    if match_start > 0 and value[match_start - 1] in "?&#":
        delimiters += "&#"

    value_end = value_start
    while value_end < len(value) and value[value_end] not in delimiters:
        value_end += 1
    if value[value_start:value_end][-1:] in ("\"", "'"):
        value_end -= 1
    return value_end


def _sanitize_assignments(value: str) -> str:
    """按结构化字段的同一判敏规则清理文本赋值。"""
    fragments = []
    emitted_until = 0
    search_from = 0
    context_from = 0
    active_quote = ""
    while header := _find_assignment_header(value, search_from):
        match_start, key, value_start = header
        active_quote = _advance_quote_context(
            value,
            context_from,
            match_start,
            active_quote,
        )
        if _is_secret_key(key):
            fragments.append(value[emitted_until:match_start])
            fragments.append(value[match_start:value_start])
            fragments.append(REDACTED_VALUE)
            emitted_until = _assignment_value_end(
                value,
                match_start,
                value_start,
                active_quote,
            )
            search_from = emitted_until
            active_quote = _advance_quote_context(
                value,
                match_start,
                emitted_until,
                active_quote,
            )
            context_from = emitted_until
            continue

        # 只消费字段头，值内部的 URL query 或嵌套诊断仍会继续进入扫描。
        search_from = value_start
        active_quote = _advance_quote_context(
            value,
            match_start,
            search_from,
            active_quote,
        )
        context_from = search_from

    if not fragments:
        return value
    fragments.append(value[emitted_until:])
    return "".join(fragments)


def _is_uri_scheme_char(char: str) -> bool:
    """判断字符是否属于 RFC 3986 scheme 的 ASCII 字符集。"""
    return char.isascii() and char.isalnum() or char in ("+", "-", ".")


def _starts_uri_scheme(value: str, start: int) -> bool:
    """判断指定位置是否以完整的 URI scheme 开始。"""
    if (
        start >= len(value)
        or not value[start].isascii()
        or not value[start].isalpha()
    ):
        return False
    scheme_end = start + 1
    while scheme_end < len(value) and _is_uri_scheme_char(value[scheme_end]):
        scheme_end += 1
    return value.startswith("://", scheme_end)


def _sanitize_uri_userinfo(value: str, *, truncated: bool = False) -> str:
    """清理 URI authority 中 `@` 前的 userinfo 凭据。"""
    fragments = []
    emitted_until = 0
    search_from = 0
    while (scheme_end := value.find("://", search_from)) >= 0:
        scheme_start = scheme_end
        while scheme_start > 0 and _is_uri_scheme_char(value[scheme_start - 1]):
            scheme_start -= 1
        if (
            scheme_start == scheme_end
            or not value[scheme_start].isascii()
            or not value[scheme_start].isalpha()
        ):
            search_from = scheme_end + 3
            continue

        authority_start = scheme_end + 3
        authority_end = authority_start
        seen_userinfo = False
        while authority_end < len(value):
            char = value[authority_end]
            if char.isspace() or char in ("/", "?", "#"):
                break
            if char == "@":
                seen_userinfo = True
            elif char in (",", ";", "|") and (
                seen_userinfo or _starts_uri_scheme(value, authority_end + 1)
            ):
                break
            authority_end += 1
        if truncated and authority_end == len(value):
            fragments.append(value[emitted_until:authority_start])
            fragments.append(REDACTED_VALUE)
            emitted_until = authority_end
            search_from = authority_end
            continue

        userinfo_end = value.rfind("@", authority_start, authority_end)
        if userinfo_end < 0:
            search_from = max(authority_end, authority_start)
            continue

        fragments.append(value[emitted_until:authority_start])
        fragments.append(f"{REDACTED_VALUE}@")
        emitted_until = userinfo_end + 1
        search_from = max(authority_end, emitted_until)

    if not fragments:
        return value
    fragments.append(value[emitted_until:])
    return "".join(fragments)


def _sanitize_text(value: str) -> str:
    """清理非结构化文本中的常见凭据表达。"""
    truncated = len(value) > _MAX_TEXT_CHARS
    bounded_value = value[:_MAX_TEXT_CHARS] if truncated else value
    sanitized = _PRIVATE_KEY_PATTERN.sub(REDACTED_VALUE, bounded_value)
    sanitized = _PRIVATE_KEY_OPEN_PATTERN.sub(REDACTED_VALUE, sanitized)
    sanitized = _SENSITIVE_HEADER_PATTERN.sub(r"\1***", sanitized)
    sanitized = _BEARER_PATTERN.sub(r"\1***", sanitized)
    sanitized = _sanitize_uri_userinfo(sanitized, truncated=truncated)
    sanitized = _sanitize_assignments(sanitized)
    sanitized = _OPENAI_KEY_PATTERN.sub(REDACTED_VALUE, sanitized)
    return f"{sanitized}<truncated>" if truncated else sanitized


def stable_type_name(value: Any) -> str:
    """绕过自定义 metaclass 协议，返回仅用于诊断的稳定类型名。"""
    try:
        name = type.__getattribute__(type(value), "__name__")
    except Exception:
        return "unknown"
    return name if type(name) is str and name else "unknown"


def _unavailable(value: Any) -> str:
    """在对象协议异常时返回不含对象文本的稳定占位。"""
    return f"<unavailable:{stable_type_name(value)}>"


def sanitize_for_host(
    value: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """递归清理宿主日志/回执使用的数据，不修改调用方原对象。"""
    try:
        if _depth >= _MAX_DEPTH:
            return "<max-depth>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            truncated = len(value) > _MAX_TEXT_CHARS
            if not truncated and value.strip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                else:
                    sanitized_json = sanitize_for_host(
                        parsed,
                        _depth=_depth + 1,
                        _seen=_seen,
                    )
                    return json.dumps(sanitized_json, ensure_ascii=False, default=str)
            return _sanitize_text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<bytes:{len(value)}>"

        seen = _seen if _seen is not None else set()
        track_identity = (
            isinstance(value, (BaseModel, Mapping, Sequence, set, frozenset))
            or is_dataclass(value)
        )
        value_id = id(value)
        if track_identity:
            if value_id in seen:
                return "<cycle>"
            seen.add(value_id)

        try:
            if isinstance(value, BaseModel):
                sanitized = {}
                model_fields = getattr(type(value), "model_fields", {})
                for index, field_name in enumerate(model_fields):
                    if index >= _MAX_ITEMS:
                        sanitized["<truncated>"] = "more fields"
                        break
                    try:
                        item = getattr(value, field_name)
                    except Exception:
                        item = _unavailable(value)
                    sanitized[field_name] = (
                        REDACTED_VALUE
                        if _is_secret_key(field_name)
                        else sanitize_for_host(
                            item,
                            _depth=_depth + 1,
                            _seen=seen,
                        )
                    )
                return sanitized

            if is_dataclass(value) and not isinstance(value, type):
                sanitized = {}
                for index, field_info in enumerate(fields(value)):
                    if index >= _MAX_ITEMS:
                        sanitized["<truncated>"] = "more fields"
                        break
                    try:
                        item = getattr(value, field_info.name)
                    except Exception:
                        item = _unavailable(value)
                    sanitized[field_info.name] = (
                        REDACTED_VALUE
                        if _is_secret_key(field_info.name)
                        else sanitize_for_host(
                            item,
                            _depth=_depth + 1,
                            _seen=seen,
                        )
                    )
                return sanitized

            if isinstance(value, Mapping):
                sanitized = {}
                items = list(islice(iter(value.items()), _MAX_ITEMS + 1))
                for key, item in items[:_MAX_ITEMS]:
                    try:
                        output_key = _sanitize_text(str(key))
                    except Exception:
                        output_key = f"<key:{stable_type_name(key)}>"
                    sanitized[output_key] = (
                        REDACTED_VALUE
                        if _is_secret_key(key)
                        else sanitize_for_host(
                            item,
                            _depth=_depth + 1,
                            _seen=seen,
                        )
                    )
                if len(items) > _MAX_ITEMS:
                    sanitized["<truncated>"] = "more items"
                return sanitized

            if isinstance(value, (set, frozenset)) or (
                isinstance(value, Sequence)
                and not isinstance(value, (str, bytes, bytearray))
            ):
                items = list(islice(iter(value), _MAX_ITEMS + 1))
                sanitized_items = [
                    sanitize_for_host(
                        item,
                        _depth=_depth + 1,
                        _seen=seen,
                    )
                    for item in items[:_MAX_ITEMS]
                ]
                if len(items) > _MAX_ITEMS:
                    sanitized_items.append("<more items>")
                return sanitized_items

            try:
                text = str(value)
            except Exception:
                return _unavailable(value)
            return _sanitize_text(text)
        finally:
            if track_identity:
                seen.discard(value_id)
    except Exception:
        return _unavailable(value)


def _bounded_summary(value: Any, *, max_chars: int) -> str:
    """把脱敏结构转换为不超过指定长度的稳定文本。"""
    sanitized = sanitize_for_host(value)
    if isinstance(sanitized, str):
        text = sanitized
    else:
        # 保留调用方字段顺序，避免排序后由大型低价值字段挤掉前部诊断信息。
        try:
            text = json.dumps(sanitized, ensure_ascii=False, default=str)
        except Exception:
            text = _unavailable(sanitized)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    suffix = "..."
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return f"{text[:max_chars - len(suffix)]}{suffix}"


def summarize_input(value: Any, *, max_chars: int = 500) -> str:
    """生成工具输入的 secret-safe 有界摘要。"""
    return _bounded_summary(value, max_chars=max_chars)


def summarize_result(value: Any, *, max_chars: int = 500) -> str:
    """生成工具结果的 secret-safe 有界摘要。"""
    return _bounded_summary(value, max_chars=max_chars)


def summarize_error(error: BaseException, *, max_chars: int = 500) -> str:
    """生成不回显异常凭据的类型化摘要。"""
    error_text = sanitize_for_host(error)
    return _bounded_summary(
        f"{stable_type_name(error)}: {error_text}",
        max_chars=max_chars,
    )


__all__ = [
    "REDACTED_VALUE",
    "sanitize_for_host",
    "stable_type_name",
    "summarize_error",
    "summarize_input",
    "summarize_result",
]
