"""
订阅日历日期窗口使用的精简事件结构。
"""

from typing import Optional

from pydantic import BaseModel, Field


class CalendarSubscription(BaseModel):
    """
    日历使用的订阅摘要，仅保留卡片展示与入库状态所需字段。
    """

    id: Optional[int] = Field(default=None, description="订阅标识")
    name: Optional[str] = Field(default=None, description="订阅名称")
    type: Optional[str] = Field(default=None, description="电影或电视剧")
    year: Optional[str] = Field(default=None, description="发行年份")
    season: Optional[int] = Field(default=None, description="订阅季号")
    poster: Optional[str] = Field(default=None, description="海报地址")
    total_episode: Optional[int] = Field(default=None, description="总集数")
    lack_episode: Optional[int] = Field(default=None, description="缺失集数")
    completed_episode: Optional[int] = Field(default=None, description="已入库集数")
    note: Optional[list[int]] = Field(default=None, description="已入库集号")
    best_version: Optional[int] = Field(default=None, description="是否洗版")
    episode_priority: Optional[dict[str, int]] = Field(default=None, description="洗版剧集优先级")
    last_update: Optional[str] = Field(default=None, description="最近入库更新时间")


class CalendarEpisode(BaseModel):
    """
    日历展示所需的单集摘要，不携带简介与演职人员等详情。
    """

    episode_number: int = Field(description="集号")
    name: Optional[str] = Field(default=None, description="本集标题")


class CalendarDate(BaseModel):
    """
    单个订阅在同一播出日期的事件摘要。
    """

    date: str = Field(description="播出或上映日期，格式为 YYYY-MM-DD")
    episodes: list[CalendarEpisode] = Field(default_factory=list, description="当天播出的分集，电影为空")
    runtime: Optional[int] = Field(default=None, description="单集或电影时长，单位分钟")


class SubscribeCalendarEvent(CalendarDate):
    """
    关联订阅与其播出日期的可展示日历项。
    """

    subscribe: CalendarSubscription = Field(description="当前用户可见的订阅摘要及入库进度")


class SubscribeCalendarPage(BaseModel):
    """
    按日期排序、限定日期窗口和条目数的订阅日历结果。
    """

    items: list[SubscribeCalendarEvent] = Field(default_factory=list, description="当前页日历条目")
    total: int = Field(description="当前日期窗口的日历条目总数")
