"""
搜索 API 输出模型。
"""

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from app.schemas.common import JsonData
from app.schemas.context import Context as _Context
from app.schemas.context import SubtitleInfo
from app.schemas.context import TorrentInfo as TorrentInfo


class SearchSourcePage(BaseModel):
    """
    单页 SSE 的来源结果；客户端持有页号并回传 source，原始页摘要由后端缓存。
    """

    source: str = Field(description="不透明的来源标识，客户端不解析")
    site_name: Optional[str] = Field(default=None, description="站点名称")
    page: int = Field(description="本次请求的页号（从 0 开始）")
    can_continue: bool = Field(description="是否允许继续请求（成功后翻页、失败后重试）；不保证下一页一定有资源")
    error: Optional[str] = Field(default=None, description="本页请求失败的原因")


class SearchDiagnosticSite(BaseModel):
    """
    本次请求实际调度的来源事实，不包含请求地址和原始错误。
    """

    site_id: Optional[int] = Field(default=None, description="站点主键；插件来源为 None")
    site_name: str = Field(description="站点名称；插件使用插件资源源，缺失名称使用未知站点")
    attempted: bool = Field(description="是否实际发起过该来源请求，冷却跳过时为 False")
    outcome: Literal["success", "empty", "error", "cooldown", "unknown"] = Field(description="来源结果；unknown 表示未获得可信观察")
    raw_count: int = Field(default=0, ge=0, description="来源报告的原始资源数，同站点本轮请求累加")


class SearchDiagnosticCounts(BaseModel):
    """
    当前页处理的候选与最终保留数量。
    """

    candidates: int = Field(default=0, ge=0, description="来源返回并交给本次过滤匹配的候选数")
    retained: int = Field(default=0, ge=0, description="完成过滤、匹配与去重后的结果数")


class SearchDiagnosticRejection(BaseModel):
    """
    有实际过滤证据的资源拒绝计数，未知原因不推测归属。
    """

    code: str = Field(description="稳定原因代码；客户端应兼容未知代码")
    count: int = Field(ge=0, description="该阶段排除的候选数，不含规则不适用警告")


class SearchDiagnosticRuleGroup(BaseModel):
    """
    所选规则组在本次媒体上下文中的适用性。
    """

    name: str = Field(description="所选规则组名称")
    status: Literal["effective", "skipped"] = Field(description="effective 表示适用；skipped 不代表资源被拒绝")
    reason: Optional[Literal["rule_group_not_applicable", "rule_group_missing"]] = Field(default=None, description="跳过原因；适用时为 None")
    media_type: Optional[str] = Field(default=None, description="规则限定的媒体类型；None 表示不限")
    categories: list[str] = Field(default_factory=list, description="规则限定的分类名称或兼容目录；空列表表示不限")


class SearchDiagnosticWarning(BaseModel):
    """
    配置适用性或来源请求警告，不计入资源拒绝数。
    """

    code: str = Field(description="稳定警告代码；客户端应兼容未知代码")
    rule_group: Optional[str] = Field(default=None, description="相关规则组名称；来源警告为 None")
    rule_ids: list[str] = Field(default_factory=list, exclude_if=lambda value: not value,
                               description="规则配置错误涉及的完整 ID；保留旧 ID 空白，没有相关 ID 时省略")


class SearchDiagnostics(BaseModel):
    """
    与一次搜索结果绑定的诊断快照；页级事实不能推断全站结果。
    """

    version: Literal[1] = Field(default=1, description="诊断契约版本")
    search_id: str = Field(description="本轮搜索的不透明标识；单页续页沿用来源标识中的编号")
    scope: Literal["page", "search"] = Field(description="page 仅描述当前页请求；search 表示完整搜索请求")
    searched_sites: list[SearchDiagnosticSite] = Field(default_factory=list, description="本次实际调度的来源，不包含未搜索站点")
    stage_counts: SearchDiagnosticCounts = Field(description="当前处理范围的候选数与保留数")
    rejection_reasons: list[SearchDiagnosticRejection] = Field(default_factory=list, description="有证据的资源拒绝原因与数量")
    rule_groups: list[SearchDiagnosticRuleGroup] = Field(default_factory=list, description="所选规则组的适用性及作用范围")
    warnings: list[SearchDiagnosticWarning] = Field(default_factory=list, description="规则适用性和来源状态警告，不含原始错误和凭据")


class SearchLastContextData(BaseModel):
    """
    上一次搜索的请求参数与结果。
    """

    params: dict[str, JsonData] = Field(default_factory=dict, description="请求参数")
    results: list[Union[_Context, SubtitleInfo]] = Field(default_factory=list, description="查询结果列表")
    diagnostics: Optional[SearchDiagnostics] = Field(default=None, description="当前用户最近完整搜索的诊断；未覆盖入口或未搜索时为 None")


class SearchRecommendStatusData(BaseModel):
    """
    AI 搜索结果推荐任务状态。
    """

    status: Literal["disabled", "idle", "running", "completed", "error"] = Field(
        description="推荐任务状态：disabled 未启用、idle 空闲、running 运行中、completed 已完成、error 失败",
    )
    results: list[int] = Field(default_factory=list, description="推荐结果在搜索列表中的索引")
