"""
通过实验室开关控制、每次重新读取配置的中心知识只读用例与技术端口。
"""

import asyncio
import hashlib
import re
from collections.abc import Callable
from typing import Optional, Protocol, Union

from pydantic import ValidationError

from app.schemas.knowledge import (
    QUERY_SECONDS,
    AgentKnowledgeConfig,
    KnowledgeDomain,
    KnowledgeOutcome,
    KnowledgePurpose,
    KnowledgeRead,
    KnowledgeSearch,
)

# 只拦截已知高风险形态，不声称识别任意秘密；工具仍须提炼短问题而非转发会话。
_SENSITIVE_QUERY = re.compile(
    r"mpk_[A-Za-z0-9_-]{43}|ke_[A-Za-z0-9_-]{43}|(?:sk-|ghp_)[A-Za-z0-9_-]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?i:cookie\s*:\s*\S+|authorization\s*:\s*\S+|"
    r"(?:passkey|password|api_key|access_token|token)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_%+/-]{8,})"
)


def contains_knowledge_secret(value: str, site: Optional[str] = None) -> bool:
    """
    在工具参数进入基类日志之前拒绝已知令牌、Cookie 和凭据形态。

    :param value: 模型提供的短问题
    :param site: 模型提供的可选站点名或别名
    :return: 发现已知凭据形态时为 True
    """
    return bool(_SENSITIVE_QUERY.search(value + "\n" + (site or "")))


class KnowledgePort(Protocol):
    """
    宿主注入的有界只读技术端口，不提供采集、列表或写知识能力。
    """

    async def query(
        self, config: AgentKnowledgeConfig, request: Union[KnowledgeSearch, KnowledgeRead],
    ) -> KnowledgeOutcome:
        """
        以当前配置完成一次检索或补读，不缓存正文或回显传输异常。

        :param config: 当前 MoviePilot Server 地址与本地工具开关
        :param request: 宿主已验证的短查询或短期补读身份
        :return: 受控的成功响应或失败状态
        """
        ...


class KnowledgeService:
    """
    管理当前开关、Server 配置、宿主版本和单次请求预算，不保留响应正文。
    """

    def __init__(
        self, config: Callable[[], AgentKnowledgeConfig], port: KnowledgePort, version: Callable[[], Optional[str]],
    ) -> None:
        """
        注入快照配置、外部技术端口和宿主版本读取器。

        :param config: 由宿主组合根提供的 Server 地址与本地开关快照读取器
        :param port: 由组合根提供的只读中心适配器
        :param version: 返回宿主当前版本，不能取自模型参数
        """
        self._config = config
        self._port = port
        self._version = version

    def current_config(self) -> Optional[AgentKnowledgeConfig]:
        """
        每次读取并验证当前 Server 地址和本地开关，损坏配置保持关闭。

        :return: 已启用配置；不可用时为 None
        """
        try:
            value = AgentKnowledgeConfig.model_validate(self._config())
        except (ValueError, TypeError):
            return None
        return value if value.enabled and value.origin else None

    def signature(self) -> str:
        """
        给工具目录提供不暴露 Server 地址的变更身份。

        :return: 当前完整配置摘要；未启用时为 disabled
        """
        config = self.current_config()
        return hashlib.sha256(config.model_dump_json().encode()).hexdigest() if config else "disabled"

    async def _query(self, request: Union[KnowledgeSearch, KnowledgeRead]) -> KnowledgeOutcome:
        """
        在请求前后核对配置，停用或 Server 地址变化后的迟到正文不再交给调用方。

        :param request: 仅包含当前问题及已验证范围的协议输入
        :return: 当前配置下的结果；请求中配置变化时丢弃响应
        """
        config = self.current_config()
        if config is None:
            return KnowledgeOutcome("disabled")
        try:
            async with asyncio.timeout(QUERY_SECONDS):
                result = await self._port.query(config, request)
        except TimeoutError:
            result = KnowledgeOutcome("timeout")
        if config != self.current_config():
            return KnowledgeOutcome("configuration_changed")
        return result

    async def search(
        self, query: str, *, domain: Optional[KnowledgeDomain] = None,
        site: Optional[str] = None, purpose: KnowledgePurpose = "usage",
    ) -> KnowledgeOutcome:
        """
        只发送当前短问题，以宿主确定的产品、稳定版本和条数检索。

        :param query: 当前问题提取的短检索词，不是整段会话
        :param domain: MP 或 PT 领域过滤
        :param site: 可选站点范围，不替代中心授权
        :param purpose: 本轮查询用途
        :return: 有限证据或明确失败状态
        """
        if contains_knowledge_secret(query, site):
            return KnowledgeOutcome("invalid_request")
        version = self._version()
        stable = re.fullmatch(r"v?(\d+\.\d+\.\d+)", version or "")
        try:
            request = KnowledgeSearch(query=query.strip(), domain=domain, site=site, purpose=purpose,
                                      product_version=stable.group(1) if stable else None, limit=3)
        except ValidationError:
            return KnowledgeOutcome("invalid_request")
        return await self._query(request)

    async def read(self, evidence_id: str) -> KnowledgeOutcome:
        """
        每次补读重新使用当前凭据，中心再次检查授权与发布状态。

        :param evidence_id: 由本轮宿主管理的中心短期引用
        :return: 有限上下文或受控失败状态
        """
        try:
            request = KnowledgeRead(evidence_id=evidence_id)
        except ValidationError:
            return KnowledgeOutcome("invalid_request")
        return await self._query(request)
