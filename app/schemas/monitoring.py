"""
请求指标、性能快照和监控配置的数据结构。
"""

from datetime import datetime
from typing import List

from pydantic import BaseModel
from pydantic import Field as _Field


class RequestMetrics(BaseModel):
    """
    请求指标模型。
    """
    path: str = _Field(description="请求的 HTTP 路径")
    method: str = _Field(description="HTTP 请求方法")
    status_code: int = _Field(description="请求返回的 HTTP 状态码")
    response_time: float = _Field(description="请求响应耗时，单位为秒")
    timestamp: datetime = _Field(description="请求发生时间")
    client_ip: str = _Field(description="请求来源的客户端地址")
    user_agent: str = _Field(description="请求客户端的浏览器标识")


class PerformanceSnapshot(BaseModel):
    """
    性能快照模型。
    """
    timestamp: datetime = _Field(description="性能快照采集时间")
    cpu_usage: float = _Field(description="处理器使用率，单位为百分比")
    memory_usage: float = _Field(description="内存使用率，单位为百分比")
    active_requests: int = _Field(description="当前正在处理的请求数")
    request_rate: float = _Field(description="统计窗口内的每秒请求数")
    avg_response_time: float = _Field(description="平均响应耗时，单位为秒")
    error_rate: float = _Field(description="失败请求占总请求的比例，范围为 0 至 1")
    slow_requests: int = _Field(description="慢请求数量")


class EndpointStats(BaseModel):
    """
    端点统计模型。
    """
    endpoint: str = _Field(description="请求端点地址")
    count: int = _Field(description="端点请求次数")
    total_time: float = _Field(description="端点累计请求耗时，单位为秒")
    errors: int = _Field(description="端点错误请求次数")
    avg_time: float = _Field(description="端点平均请求耗时，单位为秒")


class ErrorRequest(BaseModel):
    """
    错误请求模型。
    """
    timestamp: str = _Field(description="错误发生时间的展示文本")
    method: str = _Field(description="HTTP 请求方法")
    path: str = _Field(description="发生错误的 HTTP 请求路径")
    status_code: int = _Field(description="请求返回的 HTTP 状态码")
    response_time: float = _Field(description="请求响应耗时，单位为秒")
    client_ip: str = _Field(description="请求来源的客户端地址")


class MonitoringOverview(BaseModel):
    """
    监控概览模型。
    """
    performance: PerformanceSnapshot = _Field(description="当前性能监控快照")
    top_endpoints: List[EndpointStats] = _Field(description="按请求统计排序的端点列表")
    recent_errors: List[ErrorRequest] = _Field(description="最近发生的错误请求")
    alerts: List[str] = _Field(description="当前监控告警列表")


class MonitoringConfig(BaseModel):
    """
    监控配置模型。
    """
    slow_request_threshold: float = _Field(default=1.0, description="慢请求耗时阈值，单位为秒，默认 1 秒")
    error_threshold: float = _Field(default=0.05, description="失败请求比例告警阈值，默认 0.05，即百分之五")
    cpu_threshold: float = _Field(default=80.0, description="处理器使用率告警阈值，单位为百分比，默认 80")
    memory_threshold: float = _Field(default=80.0, description="内存使用率告警阈值，单位为百分比，默认 80")
    max_history: int = _Field(default=1000, description="最多保留的监控记录数量")
    window_size: int = _Field(default=60, description="监控统计窗口，单位为秒，默认 60 秒")
