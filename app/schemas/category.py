from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, RootModel


class CategoryRule(BaseModel):
    """
    分类规则详情
    """
    # 内容类型
    genre_ids: Optional[str] = None
    # 语种
    original_language: Optional[str] = None
    # 国家或地区（电视剧）
    origin_country: Optional[str] = None
    # 国家或地区（电影）
    production_countries: Optional[str] = None
    # 发行年份
    release_year: Optional[str] = None
    # 允许接收其他动态字段
    model_config = ConfigDict(extra='allow')


class CategoryConfig(BaseModel):
    """
    分类策略配置
    """
    # 电影分类策略
    movie: Optional[Dict[str, Optional[CategoryRule]]] = {}
    # 电视剧分类策略
    tv: Optional[Dict[str, Optional[CategoryRule]]] = {}


class MediaCategoryMap(RootModel[Dict[str, list[str]]]):
    """媒体类型与自动分类名称列表的映射。"""


class RouteDiagnosticWarning(BaseModel):
    """不阻断路由决策的结构化警告。"""

    code: str
    message: str
    related_indices: list[int] = Field(default_factory=list)


class CategoryConditionDecision(BaseModel):
    """单个分类条件的求值结果。"""

    field: str
    expected: Any = None
    actual: Any = None
    matched: bool = False
    message: str = ""


class CategoryRuleDecision(BaseModel):
    """单条分类规则的求值结果。"""

    index: int
    category: str
    matched: bool = False
    selected: bool = False
    reachable: bool = True
    conditions: list[CategoryConditionDecision] = Field(default_factory=list)


class CategoryRouteDecision(BaseModel):
    """分类规则求值与最终类别来源。"""

    automatic_category: str = ""
    provided_category: str = ""
    selected_category: str = ""
    source: Literal["automatic", "provided", "none"] = "none"
    rules: list[CategoryRuleDecision] = Field(default_factory=list)
    warnings: list[RouteDiagnosticWarning] = Field(default_factory=list)
