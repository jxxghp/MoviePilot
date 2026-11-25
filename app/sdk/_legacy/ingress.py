"""
旧插件显式 HTTP 回环的兼容门面；不创建平台证明，不参与内置 SDK 的入站选路。
"""

from app.application.messaging.ingress import (
    async_forward_message_to_host,
    forward_message_to_host,
    submit_message_to_host,
)

__all__ = ["async_forward_message_to_host", "forward_message_to_host", "submit_message_to_host"]
