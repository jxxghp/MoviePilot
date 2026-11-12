"""
索引模块内部的下载配方与页面确认协议解释，不发起下载请求。
"""

import base64
import hashlib
import hmac
import json
import os
from typing import Any, Optional
from urllib.parse import urlencode, urljoin, urlparse, urlsplit

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from lxml import html

from app.runtime.log import logger

# 下载确认协议按站点会话语言返回简体或繁体提示。
_FIRST_DOWNLOAD_MARKERS = tuple(
    marker.encode("utf-8") for marker in ("下载种子文件", "下載種子檔案")
)


def is_download_confirmation(content: bytes) -> bool:
    """
    识别尚未解析成种子内容的首次下载确认响应。

    :param content: 原始响应字节
    :return: 命中支持的确认提示时为 True
    """
    return any(marker in content for marker in _FIRST_DOWNLOAD_MARKERS)


def parse_download_confirmation(text: str, url: str) -> Optional[tuple[str, dict[str, Any]]]:
    """
    解析下载确认表单，仅向当前下载端点提交有效控件。

    :param text: 下载确认页面文本
    :param url: 当前下载端点的最终地址
    :return: 安全的提交地址及表单字段；没有可用表单时为 None
    """
    try:
        forms = html.fromstring(text).forms
    except (ValueError, html.etree.ParserError):
        return None
    for form in forms:
        action = form.get("action", "").strip()
        target = url if action in ("", "?") else urljoin(url, action)
        origin, destination = urlsplit(url), urlsplit(target)
        if (destination[:2] != origin[:2] or destination.path != origin.path
                or form.get("method", "post").lower() != "post"):
            continue
        data = dict(form.form_values())
        # form_values 不包含用户点击的提交控件，确认协议可能依赖该字段。
        submits = form.xpath(
            './/input[@name and not(@disabled) and translate(@type, "SUBMIT", "submit")="submit"]'
            ' | .//button[@name and not(@disabled) and (not(@type) or @type="submit")]'
        )
        if submits:
            data[submits[0].get("name")] = submits[0].get("value", "")
        if data:
            return target, data
    return None


def _normalize_download_url(url: str, base_url: Optional[str]) -> str:
    """
    按旧配方的可信 API 来源规范化绝对或相对下载结果。
    """
    if not url or not base_url:
        return url
    base_parts = urlparse(base_url)
    if not base_parts.scheme or not base_parts.netloc:
        return url
    url_parts = urlparse(url)
    if not url_parts.netloc:
        return urljoin(f"{base_url.rstrip('/')}/", url)
    return url_parts._replace(scheme=base_parts.scheme, netloc=base_parts.netloc).geturl()


def parse_download_response(request_params: dict[str, Any], status_code: Optional[int],
                            payload: object, response_text: Optional[str]) -> Optional[str]:
    """
    解释未声明专用协议的旧下载配方，兼容既有缓存中的字段提取规则。

    :param request_params: 适配器生成的旧配方，包含结果字段和可选成功标志
    :param status_code: 原始 HTTP 状态；连接失败或 HTTP 错误保留空结果语义
    :param payload: 已解析的 JSON；无效结构不产生下载链接
    :param response_text: 不指定结果字段的旧配方使用响应正文
    :return: 解析的下载地址；接口失败或结果缺失时为 None
    """
    if status_code is None or status_code >= 400:
        return None
    if not request_params.get("result"):
        return response_text
    data: Any = payload
    success_key = request_params.get("success")
    if success_key and (not isinstance(data, dict) or not data.get(success_key)):
        return None
    for key in str(request_params["result"]).split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
        if not data:
            return None
    result_path = request_params.get("result_path")
    result_query_param = request_params.get("result_query_param")
    if result_path and result_query_param:
        if not isinstance(result_query_param, str):
            return None
        result_url = urljoin(
            f"{str(request_params.get('result_base_url')).rstrip('/')}/",
            str(result_path).lstrip("/"),
        )
        return f"{result_url}?{urlencode({result_query_param: data})}"
    if not isinstance(data, str):
        return None
    base_url = request_params.get("result_base_url")
    url = _normalize_download_url(data, base_url if isinstance(base_url, str) else None)
    logger.info("已获取到站点临时下载地址")
    return url


def encrypt_site_payload(data_str: str, key: bytes) -> str:
    """
    生成 NexusPHP 兼容的带 IV、MAC 与 Base64 包装的 AES-CBC 载荷。

    :param data_str: 待加密的站点协议文本
    :param key: 协议双方约定的 AES 密钥
    :return: 保留既有字段与编码口径的加密载荷
    """
    iv = os.urandom(16)
    iv_base64 = base64.b64encode(iv)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(data_str.encode(), AES.block_size))
    ciphertext_base64 = base64.b64encode(ciphertext)
    mac = hmac.new(key, msg=iv_base64 + ciphertext_base64, digestmod=hashlib.sha256).hexdigest()
    payload = json.dumps({"iv": iv_base64.decode(), "value": ciphertext_base64.decode(), "mac": mac, "tag": ""})
    return base64.b64encode(payload.encode()).decode()
