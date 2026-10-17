"""
资源过滤规则、规则组及调整请求的数据结构。
"""

from typing import Optional

from pydantic import BaseModel, Field


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class CustomRule(BaseModel):  # type: ignore[misc]
    """
    自定义规则项。
    """

    id: Optional[str] = Field(default=None, description="规则ID")
    name: Optional[str] = Field(default=None, description="名称")
    include: Optional[str] = Field(default=None, description="包含")
    exclude: Optional[str] = Field(default=None, description="排除")
    size_range: Optional[str] = Field(default=None, description="大小范围（MB）")
    seeders: Optional[str] = Field(default=None, description="最少做种人数")
    publish_time: Optional[str] = Field(default=None, description="发布时间")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class FilterRuleGroup(BaseModel):  # type: ignore[misc]
    """
    过滤规则组。
    """

    name: Optional[str] = Field(default=None, description="名称")
    rule_string: Optional[str] = Field(default=None, description="规则串")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型 None-全部 电影/电视剧/音乐")
    category: Optional[str] = Field(default=None, description="适用媒体类别 None-全部 对应二级分类")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class CustomFilterRuleCreateRequest(BaseModel):  # type: ignore[misc]
    """
    新增自定义过滤规则请求。
    """

    rule_id: str = Field(description="新建过滤规则的唯一标识")
    name: str = Field(description="显示名称")
    include: Optional[str] = Field(default=None, description="包含")
    exclude: Optional[str] = Field(default=None, description="排除")
    size_range: Optional[str] = Field(default=None, description="大小范围（MB）")
    seeders: Optional[str] = Field(default=None, description="允许的最少做种人数")
    publish_time: Optional[str] = Field(default=None, description="发布时间")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class CustomFilterRuleUpdateRequest(BaseModel):  # type: ignore[misc]
    """
    更新自定义过滤规则请求。
    """

    new_rule_id: Optional[str] = Field(default=None, description="修改后的规则标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    include: Optional[str] = Field(default=None, description="包含")
    exclude: Optional[str] = Field(default=None, description="排除")
    size_range: Optional[str] = Field(default=None, description="大小范围（MB）")
    seeders: Optional[str] = Field(default=None, description="允许的最少做种人数")
    publish_time: Optional[str] = Field(default=None, description="发布时间")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class CustomFilterRuleReorderRequest(BaseModel):  # type: ignore[misc]
    """
    调整自定义过滤规则顺序请求。
    """

    rule_ids: list[str] = Field(description="调整后的规则标识顺序")
    expected_rule_ids: Optional[list[str]] = Field(default=None, description="调整前预期的规则标识顺序")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class FilterRuleGroupCreateRequest(BaseModel):  # type: ignore[misc]
    """
    新增过滤规则组请求。
    """

    name: str = Field(description="显示名称")
    rule_string: str = Field(description="规则串")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    category: Optional[str] = Field(default=None, description="媒体分类")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class FilterRuleGroupUpdateRequest(BaseModel):  # type: ignore[misc]
    """
    更新过滤规则组请求。
    """

    new_name: Optional[str] = Field(default=None, description="修改后的名称")
    rule_string: Optional[str] = Field(default=None, description="规则串")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    category: Optional[str] = Field(default=None, description="媒体分类")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class FilterRuleGroupReorderRequest(BaseModel):  # type: ignore[misc]
    """
    调整过滤规则组顺序请求。
    """

    group_names: list[str] = Field(description="调整后的规则组顺序")
    expected_group_names: Optional[list[str]] = Field(default=None, description="调整前预期的规则组顺序，用于检测并发修改")
