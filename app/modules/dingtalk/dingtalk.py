"""
钉钉自定义机器人 Webhook 客户端。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from functools import partial
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger
from app.schemas.delivery import DeliveryPartPlanner, DeliveryPartSender, DeliveryReceipt, DeliveryStatus


class DingTalk:
    """
    通过钉钉自定义机器人 Webhook 发送 Markdown 通知。
    """

    def __init__(
        self,
        DINGTALK_WEBHOOK: Optional[str] = None,
        DINGTALK_SECRET: Optional[str] = None,
        **kwargs: object,
    ) -> None:
        """
        保存 Webhook 与可选加签密钥，网络请求延迟到实际发送时执行。
        """
        self._webhook = (DINGTALK_WEBHOOK or "").strip()
        self._secret = (DINGTALK_SECRET or "").strip()
        self._req = RequestUtils(content_type="application/json", timeout=30)

    def get_state(self) -> bool:
        """
        检查 Webhook 是否为可发送请求的 HTTP(S) 地址。
        """
        if not self._webhook:
            return False
        parsed = urlsplit(self._webhook)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def build_request_url(self, timestamp: Optional[int] = None) -> str:
        """
        按钉钉加签协议追加时间戳与签名。

        :param timestamp: Unix 时间戳，单位毫秒；省略时使用当前时间
        :return: 已加签请求地址；未配置密钥时返回原 Webhook
        """
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
        """
        将通知字段转换为钉钉 Markdown 标题和正文。

        :param title: 原始标题；首行用作消息标题，空标题使用 MoviePilot 通知
        :param text: 可选正文
        :param image: 可选图片地址
        :param link: 可选详情链接
        :return: 消息标题与 Markdown 正文二元组
        """
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
        """
        向机器人所在群发送 Markdown 消息并校验业务返回码。

        请求结束后关闭响应；该入口不执行接收人定向发送。

        :param title: 消息标题，可与正文择一提供
        :param text: 消息正文
        :param image: 可选图片地址
        :param userid: 兼容统一通知接口的接收人字段，群机器人当前忽略
        :param link: 可选详情链接
        :return: HTTP 和钉钉业务状态均成功时返回 True；配置、输入或响应失败时返回 False
        """
        if not title and not text:
            logger.warning("钉钉通知标题和内容不能同时为空")
            return False
        if not self.get_state():
            logger.error("钉钉自定义机器人 Webhook 配置不完整")
            return False

        markdown = self.build_markdown(title, text, image, link)
        status, _ = self._post_markdown(*markdown)
        return status == "confirmed"

    def send_tracked_chunks(
        self, title: str | None, texts: list[str | None], image: str | None, link: str | None, *,
        receipt: DeliveryReceipt, part_sender: DeliveryPartSender, plan: DeliveryPartPlanner,
    ) -> None:
        """
        在提交钉钉消息前固定群 Webhook 和全部 Markdown 分段。

        :param title: 消息标题
        :param texts: 有序正文分段
        :param image: 可选封面地址
        :param link: 可选详情链接
        :param receipt: 记录平台确认的回调
        :param part_sender: 每段提交前核对进度的回调
        :param plan: 首次提交前登记分段和目标的回调
        """
        target = "webhook:" + hashlib.sha256(self._webhook.encode()).hexdigest()
        chunks = [self.build_markdown(title, text, image, link) for text in texts if title or text]
        if not chunks:
            receipt(target, 0, "skipped", "empty_content")
            return
        rendered = [json.dumps(chunk, ensure_ascii=False) for chunk in chunks]
        plan(tuple((target, index, content) for index, content in enumerate(rendered)))
        confirmed = True
        for index, (content, markdown) in enumerate(zip(rendered, chunks)):
            status, code = part_sender(target, index, content, confirmed, partial(self._post_markdown, *markdown))
            receipt(target, index, status, code)
            confirmed = confirmed and status == "confirmed"

    def _post_markdown(self, title: str, text: str) -> tuple[DeliveryStatus, str | None]:
        """
        提交单段 Markdown，区分业务拒绝与结果未知。

        :param title: 渲染后的平台标题
        :param text: 渲染后的 Markdown 正文
        :return: 平台实际确认状态及脱敏错误类别
        """
        if not self.get_state():
            return "permanent_failure", "invalid_webhook"
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
        }
        response = self._req.post_res(url=self.build_request_url(), json=payload, allow_redirects=False)
        if response is None:
            logger.error("钉钉自定义机器人请求失败")
            return "unknown", "response_unconfirmed"
        try:
            if response.status_code == 429:
                return "retryable_failure", "rate_limited"
            if response.status_code != 200:
                logger.error(f"钉钉自定义机器人返回 HTTP {response.status_code}")
                return "unknown", "response_unconfirmed"
            try:
                result = response.json()
            except ValueError:
                logger.error("钉钉自定义机器人返回了无法解析的响应")
                return "unknown", "response_unconfirmed"
            if not isinstance(result, dict) or type(result.get("errcode")) is not int:
                return "unknown", "response_unconfirmed"
            if result["errcode"] == 0:
                return "confirmed", None
            logger.error("钉钉自定义机器人返回错误码 %s", result["errcode"])
            return "permanent_failure", "platform_rejected"
        finally:
            response.close()
