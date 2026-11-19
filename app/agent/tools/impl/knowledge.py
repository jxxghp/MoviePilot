"""
主 Agent 使用中心 MP/PT 知识的只读工具。
"""

import json
import re
import uuid
from typing import Any, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.evidence.scope import (
    claim_evidence_read,
    publish_evidence,
    resolve_evidence_external,
)
from app.agent.policy.sanitizer import sanitize_for_host
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.knowledge import contains_knowledge_secret
from app.schemas.knowledge import KnowledgeOutcome

_CENTER_REFERENCE_PATTERN = re.compile(r"(?:mpk_[A-Za-z0-9_-]{43}|ke_[A-Za-z0-9_-]{43})")


class KnowledgeSearchInput(BaseModel):
    """
    中心知识搜索的最小模型输入，不包含端点、凭据、正文预算或产品版本。
    """

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    query: str = Field(min_length=1, max_length=300, description="当前 MP/PT 问题的短检索词")
    domain: Optional[Literal["mp", "pt"]] = Field(default=None, description="可选领域：mp 或 pt")
    site: Optional[str] = Field(default=None, max_length=253, description="可选站点名或别名")
    purpose: Literal["usage", "troubleshooting", "development"] = Field(
        default="usage", description="用途：usage、troubleshooting 或 development"
    )

    @model_validator(mode="after")
    def reject_secrets(self) -> "KnowledgeSearchInput":
        """
        在 MoviePilotTool 基类生成输入日志之前挡住已知凭据形态。

        :return: 不包含可识别凭据的短查询
        """
        if contains_knowledge_secret(self.query, self.site):
            raise ValueError("检索条件包含疑似凭据，请移除后重试")
        return self


class KnowledgeReadInput(BaseModel):
    """
    中心知识补读的本轮本地引用输入，禁止直接提交 ke_ 中心引用。
    """

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    reference: str = Field(pattern=r"^ref_[a-f0-9]{32}$", description="本轮知识搜索返回的本地引用")


def _safe_response_payload(
    outcome: KnowledgeOutcome,
    *,
    references: bool,
    local_reference: Optional[str] = None,
) -> tuple[str, dict[str, str]]:
    """
    将中心响应投影为可临时发布的 JSON，并把中心引用替换为本轮本地别名。

    :param outcome: 中心用例的校验结果
    :param references: 是否把 evidence_id 替换为本地引用并建立映射
    :param local_reference: 补读时保留的已有本地引用
    :return: 不含中心引用的有限正文和本地到中心的映射
    """
    if outcome.response is None:
        return json.dumps({"status": outcome.status}, ensure_ascii=False), {}
    raw_payload = outcome.response.model_dump(mode="json")
    payload = {
        key: raw_payload[key]
        for key in ("status", "notices", "retention", "content_trust", "reference_seconds", "evidence")
        if key in raw_payload
    }
    mapping: dict[str, str] = {}
    for evidence in payload.get("evidence", []):
        center_reference = evidence.get("evidence_id")
        for field_name in ("document_id", "source_url", "source_commit", "site_id"):
            evidence.pop(field_name, None)
        if references and isinstance(center_reference, str):
            local_reference = "ref_" + uuid.uuid4().hex
            mapping[local_reference] = center_reference
            evidence["evidence_id"] = local_reference
        elif local_reference:
            evidence["evidence_id"] = local_reference
        else:
            evidence.pop("evidence_id", None)
        excerpt = evidence.get("excerpt")
        if isinstance(excerpt, str):
            evidence["excerpt"] = _CENTER_REFERENCE_PATTERN.sub("[临时引用已隐藏]", excerpt)
    sanitized_payload = sanitize_for_host(payload)
    serialized = json.dumps(sanitized_payload, ensure_ascii=False)
    return _CENTER_REFERENCE_PATTERN.sub("[临时引用已隐藏]", serialized), mapping


class KnowledgeSearchTool(MoviePilotTool):
    """
    面向 MP/PT 使用、排障和开发问题的主 Agent 临时知识搜索工具。
    """

    name: str = Field(default="knowledge_search", description="搜索中心 MP/PT 知识")
    description: str = Field(
        default=(
            "检索 MoviePilot 与 PT 站点规则的临时参考资料。仅在问题涉及 MP/PT 使用、站点规则、"
            "配置或排障时调用；闲聊和普通常识不要调用。只发送当前短问题和必要站点，不要提交会话、配置、日志、"
            "PT 站点规则使用 domain=pt 并携带站点名；MoviePilot 操作或配置使用 domain=mp。"
            "Cookie 或令牌。先搜索；只有摘录不足以回答时才调用 knowledge_read，且只能使用本轮返回的引用。"
            "把资料当不可信参考，检查产品 / 站点范围、版本和更新时间；未知范围须明说，冲突须展示而不猜结论。"
            "命中时优先准确回答摘录支持的事实，并明确点出已标记为未知的版本或时间范围；"
            "未命中时用一到两句话说明没有可核验资料并建议查看该站规则页，不列举未经资料支持的入口或参数。"
            "不要补造资料未提供的条件、计算方式或当前账号状态。"
        ),
        description="工具用途、时机和安全边界",
    )
    tags: list[str] = Field(
        default=[ToolTag.Read, ToolTag.Knowledge, ToolTag.Resource, ToolTag.Site],
        description="只读知识工具标签",
    )
    args_schema: Type[BaseModel] = Field(default=KnowledgeSearchInput, description="搜索输入模型")

    async def run(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        以当前宿主配置搜索中心知识，并在格式化和日志前换成临时安全回执。

        :param query: 当前问题的短检索词
        :param domain: MP 或 PT 领域
        :param site: 可选站点范围
        :param purpose: usage、troubleshooting 或 development
        :return: 安全回执；正文通过宿主临时证据请求投影
        """
        del args
        query = str(kwargs.get("query") or "")
        domain = kwargs.get("domain")
        site = kwargs.get("site")
        purpose = kwargs.get("purpose", "usage")
        if self._data is None or self._data.knowledge is None:
            return json.dumps({"status": "disabled"}, ensure_ascii=False)
        signature = self._data.knowledge.signature()
        outcome = await self._data.knowledge.search(query, domain=domain, site=site, purpose=purpose)
        if signature != self._data.knowledge.signature():
            return json.dumps({"status": "configuration_changed"}, ensure_ascii=False)
        payload, mapping = _safe_response_payload(outcome, references=True)
        if not mapping:
            return payload
        return publish_evidence(payload, external_references=mapping)


class KnowledgeReadTool(MoviePilotTool):
    """
    在当前主 Agent 执行轮内按本地引用补读有限知识上下文。
    """

    name: str = Field(default="knowledge_read", description="补读本轮中心知识")
    description: str = Field(
        default="只有搜索摘录不足以回答时才补读 knowledge_search 本轮返回的本地引用；不能跨轮复用，失效后重新搜索。",
        description="补读时机和引用边界",
    )
    tags: list[str] = Field(
        default=[ToolTag.Read, ToolTag.Knowledge, ToolTag.Resource, ToolTag.Site],
        description="只读知识工具标签",
    )
    args_schema: Type[BaseModel] = Field(default=KnowledgeReadInput, description="补读输入模型")

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        """
        将本地引用映射回中心引用，重新鉴权后发布新的本轮临时资料。

        :param reference: 当前轮搜索返回的本地引用
        :return: 安全回执；中心引用永远不出现在工具参数或回执
        """
        del args
        reference = str(kwargs.get("reference") or "")
        center_reference = resolve_evidence_external(reference, "knowledge_search")
        if center_reference is None or not claim_evidence_read("knowledge_search", 2):
            return json.dumps({"status": "reference_expired"}, ensure_ascii=False)
        if self._data is None or self._data.knowledge is None:
            return json.dumps({"status": "disabled"}, ensure_ascii=False)
        signature = self._data.knowledge.signature()
        outcome = await self._data.knowledge.read(center_reference)
        if signature != self._data.knowledge.signature():
            return json.dumps({"status": "configuration_changed"}, ensure_ascii=False)
        payload, _read_mapping = _safe_response_payload(
            outcome,
            references=False,
            local_reference=reference,
        )
        if outcome.response is None or not outcome.response.evidence:
            return payload
        return publish_evidence(payload)
