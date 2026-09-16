from typing import Optional

from pydantic import BaseModel


class CustomRule(BaseModel):
    """
    自定义规则项
    """

    # 规则ID
    id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 大小范围（MB）
    size_range: Optional[str] = None
    # 最少做种人数
    seeders: Optional[str] = None
    # 发布时间
    publish_time: Optional[str] = None


class FilterRuleGroup(BaseModel):
    """
    过滤规则组
    """

    # 名称
    name: Optional[str] = None
    # 规则串
    rule_string: Optional[str] = None
    # 适用媒体类型 None-全部 电影/电视剧/音乐
    media_type: Optional[str] = None
    # 适用媒体类别 None-全部 对应二级分类
    category: Optional[str] = None


class CustomFilterRuleCreateRequest(BaseModel):  # type: ignore[misc]
    """新增自定义过滤规则请求。"""

    rule_id: str
    name: str
    include: Optional[str] = None
    exclude: Optional[str] = None
    size_range: Optional[str] = None
    seeders: Optional[str] = None
    publish_time: Optional[str] = None


class CustomFilterRuleUpdateRequest(BaseModel):  # type: ignore[misc]
    """更新自定义过滤规则请求。"""

    new_rule_id: Optional[str] = None
    name: Optional[str] = None
    include: Optional[str] = None
    exclude: Optional[str] = None
    size_range: Optional[str] = None
    seeders: Optional[str] = None
    publish_time: Optional[str] = None


class CustomFilterRuleReorderRequest(BaseModel):  # type: ignore[misc]
    """调整自定义过滤规则顺序请求。"""

    rule_ids: list[str]
    expected_rule_ids: Optional[list[str]] = None


class FilterRuleGroupCreateRequest(BaseModel):  # type: ignore[misc]
    """新增过滤规则组请求。"""

    name: str
    rule_string: str
    media_type: Optional[str] = None
    category: Optional[str] = None


class FilterRuleGroupUpdateRequest(BaseModel):  # type: ignore[misc]
    """更新过滤规则组请求。"""

    new_name: Optional[str] = None
    rule_string: Optional[str] = None
    media_type: Optional[str] = None
    category: Optional[str] = None


class FilterRuleGroupReorderRequest(BaseModel):  # type: ignore[misc]
    """调整过滤规则组顺序请求。"""

    group_names: list[str]
    expected_group_names: Optional[list[str]] = None
