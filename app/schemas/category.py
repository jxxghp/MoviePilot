from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, RootModel


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
