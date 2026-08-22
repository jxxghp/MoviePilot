"""钉钉自定义机器人 Webhook 客户端。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger


class DingTalk:
    """通过钉钉自定义机器人 Webhook 发送 Markdown 通知。"""

    def __init__(
        self,
        DINGTALK_WEBHOOK: Optional[str] = None,
        DINGTALK_SECRET: Optional[str] = None,
        **kwargs,
    ) -> None:
        """保存 Webhook 与可选加签密钥，网络请求延迟到实际发送时执行。"""
        self._webhook = (DINGTALK_WEBHOOK or "").strip()
        self._secret = (DINGTALK_SECRET or "").strip()
        self._req = RequestUtils(content_type="application/json", timeout=30)

    def get_state(self) -> bool:
        """检查 Webhook 是否为可发送请求的 HTTP(S) 地址。"""
        if not self._webhook:
            return False
        parsed = urlsplit(self._webhook)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def build_request_url(self, timestamp: Optional[int] = None) -> str:
        """按钉钉加签协议向 Webhook 查询参数追加时间戳与签名。"""
        if not self._secret:
            return self._webhook
        timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
        string_to_sign = f"{timestamp}\n{self._secret}".encode("utf-8")
        digest = hmac.new(
            self._secret.encode("utf-8"),
            string_to_sign,
            digestmod=hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("utf-8")
        parsed = urlsplit(self._webhook)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update({"timestamp": str(timestamp), "sign": signature})
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )

    @staticmethod
    def build_markdown(
        title: Optional[str],
        text: Optional[str] = None,
        image: Optional[str] = None,
        link: Optional[str] = None,
    ) -> tuple[str, str]:
        """将 MoviePilot 通知字段转换为钉钉 Markdown 标题与正文。"""
        raw_title = str(title or "").strip()
        title_lines = raw_title.splitlines()
        markdown_title = title_lines[0].strip() if title_lines else ""
        markdown_title = markdown_title or "MoviePilot 通知"

        content_parts = []
        if raw_title:
            content_parts.append(f"### {raw_title}")
        if text:
            content_parts.append(str(text).strip())
        if image:
            content_parts.append(f"![图片]({image})")
        if link:
            content_parts.append(f"[查看详情]({link})")
        if not content_parts:
            content_parts.append(markdown_title)
        return markdown_title, "\n\n".join(part for part in content_parts if part)

    def send_msg(
        self,
        title: Optional[str],
        text: Optional[str] = None,
        image: Optional[str] = None,
        userid: Optional[str] = None,
        link: Optional[str] = None,
    ) -> bool:
        """向机器人所在群发送一条 Markdown 消息并校验钉钉业务返回码。"""
        if not title and not text:
            logger.warning("钉钉通知标题和内容不能同时为空")
            return False
        if not self.get_state():
            logger.error("钉钉自定义机器人 Webhook 配置不完整")
            return False

        markdown_title, markdown_text = self.build_markdown(
            title=title,
            text=text,
            image=image,
            link=link,
        )
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": markdown_title,
                "text": markdown_text,
            },
        }
        response = self._req.post_res(url=self.build_request_url(), json=payload)
        if response is None:
            logger.error("钉钉自定义机器人请求失败")
            return False
        try:
            if response.status_code != 200:
                logger.error(f"钉钉自定义机器人返回 HTTP {response.status_code}")
                return False
            try:
                result = response.json()
            except ValueError:
                logger.error("钉钉自定义机器人返回了无法解析的响应")
                return False
            if not isinstance(result, dict):
                logger.error("钉钉自定义机器人返回了非对象响应")
                return False
            if result.get("errcode") == 0:
                return True
            logger.error(
                "钉钉自定义机器人返回错误："
                f"{result.get('errcode')}-{result.get('errmsg') or '未知错误'}"
            )
            return False
        finally:
            response.close()
