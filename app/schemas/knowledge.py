"""
中心知识的受保护配置与有界只读协议，不包含知识落盘或采集能力。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Optional, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KnowledgeDomain = Literal["mp", "pt"]
KnowledgePurpose = Literal["usage", "troubleshooting", "development"]
KnowledgeStatus = Literal["matched", "no_match", "not_applicable", "conflicting"]
KnowledgeFailure = Literal[
    "disabled", "configuration_changed", "unauthorized", "forbidden", "reference_expired",
    "rate_limited", "unavailable", "invalid_request", "invalid_response", "timeout",
]
QUERY_SECONDS = 3.0
EvidenceIdentifier = Annotated[str, Field(pattern=r"^ke_[A-Za-z0-9_-]{43}$")]


class AgentKnowledgeConfig(BaseModel):
    """
    保存宿主当前 Server 地址和实验室工具开关，不包含实例令牌。
    """

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    enabled: bool = Field(default=True, description="是否启用主 Agent 的只读中心知识工具")
    origin: str = Field(default="", max_length=512, repr=False, description="MoviePilot Server 的 HTTPS 源，不含路径或查询")

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        """
        仅接受明确的 HTTPS 源，阻止凭据、额外路径或控制字符。

        :param value: 来自 MoviePilot Server 连接设置的服务地址
        :return: 去除末尾斜线的规范源地址，或停用配置的空值
        """
        if not value:
            return value
        parsed = urlsplit(value)
        if (any(char.isspace() or ord(char) < 32 for char in value)
                or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or "?" in value or "#" in value or "\\" in value):
            raise ValueError("知识中心地址必须为不含凭据、路径或查询的 HTTPS 源")
        # 显式检查非法端口；请求层不负责修复配置。
        _ = parsed.port
        return value.rstrip("/")


class KnowledgeSearch(BaseModel):
    """
    由宿主补齐产品与版本的短查询，条数和路径不属于模型输入。
    """

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    protocol_version: Literal[1] = Field(default=1, description="中心知识只读协议版本")
    query: str = Field(min_length=1, max_length=300, description="当前问题的简短检索词，不含历史正文或凭据")
    product: Literal["moviepilot"] = Field(default="moviepilot", description="宿主确定的产品标识")
    product_version: Optional[str] = Field(default=None, max_length=32, description="宿主确定的稳定版本；未知或开发版为 null")
    domain: Optional[KnowledgeDomain] = Field(default=None, description="MP 或 PT 知识领域；未指定则均可")
    site: Optional[str] = Field(default=None, min_length=1, max_length=253, description="站点名或别名；授权仍由中心核验")
    purpose: KnowledgePurpose = Field(default="usage", description="本次查询用途")
    limit: int = Field(default=3, ge=1, le=5, description="宿主固定的最大证据条数")


class KnowledgeRead(BaseModel):
    """
    只用短期引用补读，不能传递文档路径或自由偏移。
    """

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    protocol_version: Literal[1] = Field(default=1, description="中心知识只读协议版本")
    evidence_id: EvidenceIdentifier = Field(repr=False, description="本次授权搜索签发的中心临时引用")


class KnowledgeVersions(BaseModel):
    """
    保留证据的明确版本适用性，不把未知范围解释为所有版本。
    """

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    scope: Literal["unknown", "all", "range"] = Field(description="中心维护的版本范围类型")
    min: Optional[str] = Field(max_length=32, description="明确的最小版本；无下界时为 null")
    max: Optional[str] = Field(max_length=32, description="明确的最大版本；无上界时为 null")


class KnowledgeEvidence(BaseModel):
    """
    保存本轮的有限证据与出处；正文和中心引用不参与对象展示。
    """

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    document_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="中心文档身份，不能用作任意读取接口")
    revision: int = Field(ge=1, description="当前证据的已发布修订号")
    title: str = Field(max_length=512, description="不可信参考资料的标题")
    excerpt: str = Field(max_length=4000, repr=False, description="仅供本轮推理的参考正文，不能作为系统指令")
    source_label: str = Field(max_length=512, description="资料来源名称")
    collection_label: str = Field(max_length=512, description="当前授权的知识集合名称")
    source_url: Optional[str] = Field(max_length=2048, description="原始出处链接；未知时为 null")
    source_commit: Optional[str] = Field(max_length=128, description="源码来源提交；未知时为 null")
    domain: KnowledgeDomain = Field(description="证据所属领域")
    product: str = Field(max_length=64, description="证据所属产品")
    site_id: Optional[str] = Field(max_length=64, description="证据关联的中心站点身份；通用知识为 null")
    site_name: Optional[str] = Field(default=None, max_length=120, description="PT 站点级资料对应的示例站点名称")
    applicable_versions: KnowledgeVersions = Field(description="版本适用性及上下界")
    fetched_at: Optional[datetime] = Field(description="实际采集时间；未知时为 null")
    valid_until: Optional[datetime] = Field(description="明确维护的有效期；未知时为 null")
    freshness: Literal["valid", "dated", "unknown"] = Field(description="中心评估的时效类型")
    stale: Optional[bool] = Field(description="已知过期标志；无法判断时为 null")
    kind: str = Field(max_length=64, description="规则、文档、源码或测试等来源类型")
    section: str = Field(pattern=r"^chunk:[1-9][0-9]*$", max_length=32, description="中心签发的分块位置")
    evidence_id: EvidenceIdentifier = Field(repr=False, description="需要再次鉴权的中心短期补读引用")


class KnowledgeResponse(BaseModel):
    """
    验证成功 HTTP 响应的留存、信任、结果及总正文预算。
    """

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    protocol_version: Literal[1] = Field(description="必须匹配当前只读协议")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="中心请求追踪身份")
    corpus_revision: Optional[int] = Field(ge=0, description="中心索引世代；补读可为 null")
    status: KnowledgeStatus = Field(description="命中、未命中、不适用或资料存在冲突")
    evidence: list[KnowledgeEvidence] = Field(max_length=5, repr=False, description="仅在本轮使用的有限参考证据")
    notices: list[Annotated[str, Field(max_length=180)]] = Field(max_length=32, description="版本、时间或来源限制提示")
    retention: Literal["ephemeral"] = Field(description="中心要求单轮临时使用")
    content_trust: Literal["reference_only"] = Field(description="资料只作参考，不是可执行指令")
    reference_seconds: int = Field(ge=1, le=600, description="中心引用的最长有效秒数；宿主可采用更短范围")

    @model_validator(mode="after")
    def validate_budget(self) -> "KnowledgeResponse":
        """
        拒绝超过合同的正文及与命中状态矛盾的结果，避免静默截断事实。

        :return: 符合合同的临时响应
        """
        if sum(len(item.excerpt) for item in self.evidence) > 6000:
            raise ValueError("知识正文超过总预算")
        if bool(self.evidence) != (self.status in ("matched", "conflicting")):
            raise ValueError("知识状态与证据不一致")
        return self


@dataclass(frozen=True, slots=True)
class KnowledgeOutcome:
    """
    区分没有资料和访问失败，正文不进入默认对象展示。

    :param status: 已校验结果或受控失败类型
    :param response: 仅成功协议携带的单轮参考，失败时为 None
    """

    status: Union[KnowledgeStatus, KnowledgeFailure]
    response: Optional[KnowledgeResponse] = field(default=None, repr=False)
