"""
永久种子黑名单的受限持久化结构与管理接口契约。
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BlacklistRule(BaseModel):
    """
    一条手动规则，非空条件按与匹配，不接受正则表达式。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    keyword: str | None = Field(default=None, max_length=256, description="标题或副标题包含的字面关键词，不区分大小写")
    site_id: int | None = Field(default=None, strict=True, ge=1, description="精确匹配的已配置站点 ID")
    uploader: str | None = Field(default=None, max_length=128, description="精确匹配的发布者名称，不区分大小写")

    @field_validator("keyword", "uploader", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        """
        去除首尾空白并折叠大小写，使重复规则具有相同表示。
        """
        return value.strip().casefold() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_condition(self) -> Self:
        """
        拒绝没有条件的规则，避免意外禁止全部资源。
        """
        if self.keyword is None and self.site_id is None and self.uploader is None:
            raise ValueError("至少填写关键词、站点或发布者之一")
        return self


class BlacklistIdentity(BaseModel):
    """
    脱敏资源身份，只有摘要和标准哈希可用于资源匹配。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = Field(default=1, description="安全身份结构版本")
    digests: tuple[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")], ...] = Field(
        default=(), max_length=32, description="站点与具体种子 ID 的 SHA256 摘要，最多 32 个")
    hashes: tuple[Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")], ...] = Field(
        default=(), max_length=32, description="规范化 v1、v2 及下载器截断哈希，最多 32 个")
    site_id: int | None = Field(default=None, strict=True, ge=1, description="可选的来源站点 ID，仅供展示")
    uploader: str | None = Field(default=None, max_length=128, description="可选的规范化发布者名称，仅供展示")

    @field_validator("digests", "hashes")
    @classmethod
    def canonical_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """
        排序和去重已经验证的安全别名。
        """
        return tuple(sorted(set(value)))


class BlacklistEntry(BaseModel):
    """
    服务器生成 ID 与时间的永久规则或具体资源限制。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-f0-9]{32}$", description="服务器生成的黑名单条目标识")
    created_at: datetime = Field(description="服务器生成的创建时间")
    kind: Literal["manual", "resource"] = Field(description="手动条件规则或具体资源")
    rule: BlacklistRule | None = Field(default=None, description="手动规则条件")
    identity: BlacklistIdentity | None = Field(default=None, description="具体资源的脱敏身份")

    @model_validator(mode="after")
    def require_matching_payload(self) -> Self:
        """
        校验条目类型与数据一致，资源条目必须至少有一个可匹配身份。
        """
        if self.kind == "manual":
            if self.rule is None or self.identity is not None:
                raise ValueError("手动规则数据无效")
        elif self.rule is not None or self.identity is None or not (self.identity.digests or self.identity.hashes):
            raise ValueError("资源黑名单缺少安全身份")
        if self.created_at.tzinfo is None:
            raise ValueError("创建时间必须带时区")
        return self


class BlacklistConfig(BaseModel):
    """
    版本化且不可变的配置快照，损坏配置必须显式报错。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = Field(default=1, description="黑名单配置结构版本")
    entries: tuple[BlacklistEntry, ...] = Field(default=(), max_length=1000, description="最多 1000 条永久黑名单")

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        """
        拒绝重复条目标识，避免删除时出现歧义。
        """
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("黑名单条目标识重复")
        return self


class BlacklistPage(BaseModel):
    """
    同一快照生成的黑名单分页结果。
    """

    items: tuple[BlacklistEntry, ...] = Field(description="当前页条目")
    total: int = Field(ge=0, description="快照中的条目总数")
    page: int = Field(ge=1, description="从 1 开始的页码")
    count: int = Field(ge=1, le=100, description="每页条数")


class BlacklistAddResult(BaseModel):
    """
    新增或幂等合并后的条目与创建状态。
    """

    entry: BlacklistEntry = Field(description="持久化后的完整条目")
    created: bool = Field(description="本次是否新建，重复或合并时为否")


class BlacklistRemoveResult(BaseModel):
    """
    幂等删除结果，不涉及下载任务、文件或历史。
    """

    removed: bool = Field(description="本次是否实际移除了条目")
