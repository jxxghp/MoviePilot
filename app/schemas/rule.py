"""
资源过滤规则、规则组及调整请求的数据结构。
"""

from typing import Optional

from pydantic import BaseModel
from pydantic import Field as _Field


class CustomRule(BaseModel):
    """
    自定义规则项。
    """

    id: Optional[str] = _Field(default=None, description="规则ID")
    name: Optional[str] = _Field(default=None, description="名称")
    include: Optional[str] = _Field(default=None, description="包含")
    exclude: Optional[str] = _Field(default=None, description="排除")
    size_range: Optional[str] = _Field(default=None, description="单集平均文件大小范围，单位为 MiB；a-b、>a、<a 均包含边界，空值不限制")
    seeders: Optional[str] = _Field(default=None, description="最少做种人数")
    publish_time: Optional[str] = _Field(default=None, description="距离发布经过的分钟数；单值为最小等待时间，区间表示允许的时间范围")


class FilterRuleGroup(BaseModel):
    """
    过滤规则组。
    """

    name: Optional[str] = _Field(default=None, description="名称")
    rule_string: Optional[str] = _Field(default=None, description="规则串")
    media_type: Optional[str] = _Field(default=None, description="适用媒体类型 None-全部 电影/电视剧/音乐")
    category: Optional[str] = _Field(default=None, description="适用媒体类别 None-全部 对应二级分类")


class CustomFilterRuleCreateRequest(BaseModel):
    """
    新增自定义过滤规则请求。
    """

    rule_id: str = _Field(description="新建过滤规则的唯一标识")
    name: str = _Field(description="显示名称")
    include: Optional[str] = _Field(default=None, description="包含")
    exclude: Optional[str] = _Field(default=None, description="排除")
    size_range: Optional[str] = _Field(default=None, description="单集平均文件大小范围，单位为 MiB；a-b、>a、<a 均包含边界，空值不限制")
    seeders: Optional[str] = _Field(default=None, description="允许的最少做种人数")
    publish_time: Optional[str] = _Field(default=None, description="距离发布经过的分钟数；单值为最小等待时间，区间表示允许的时间范围")


class CustomFilterRuleUpdateRequest(BaseModel):
    """
    更新自定义过滤规则请求。
    """

    new_rule_id: Optional[str] = _Field(default=None, description="修改后的规则标识")
    name: Optional[str] = _Field(default=None, description="显示名称")
    include: Optional[str] = _Field(default=None, description="包含")
    exclude: Optional[str] = _Field(default=None, description="排除")
    size_range: Optional[str] = _Field(default=None, description="单集平均文件大小范围，单位为 MiB；a-b、>a、<a 均包含边界，空值不限制")
    seeders: Optional[str] = _Field(default=None, description="允许的最少做种人数")
    publish_time: Optional[str] = _Field(default=None, description="距离发布经过的分钟数；单值为最小等待时间，区间表示允许的时间范围")


class CustomFilterRuleReorderRequest(BaseModel):
    """
    调整自定义过滤规则顺序请求。
    """

    rule_ids: list[str] = _Field(description="调整后的规则标识顺序")
    expected_rule_ids: Optional[list[str]] = _Field(default=None, description="调整前预期的规则标识顺序")


class FilterRuleGroupCreateRequest(BaseModel):
    """
    新增过滤规则组请求。
    """

    name: str = _Field(description="显示名称")
    rule_string: str = _Field(description="规则串")
    media_type: Optional[str] = _Field(default=None, description="适用媒体类型")
    category: Optional[str] = _Field(default=None, description="媒体分类")


class FilterRuleGroupUpdateRequest(BaseModel):
    """
    更新过滤规则组请求。
    """

    new_name: Optional[str] = _Field(default=None, description="修改后的名称")
    rule_string: Optional[str] = _Field(default=None, description="规则串")
    media_type: Optional[str] = _Field(default=None, description="适用媒体类型")
    category: Optional[str] = _Field(default=None, description="媒体分类")


class FilterRuleGroupReorderRequest(BaseModel):
    """
    调整过滤规则组顺序请求。
    """

    group_names: list[str] = _Field(description="调整后的规则组顺序")
    expected_group_names: Optional[list[str]] = _Field(default=None, description="调整前预期的规则组顺序，用于检测并发修改")
