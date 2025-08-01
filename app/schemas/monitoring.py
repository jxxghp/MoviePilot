from datetime import datetime
from typing import List

from pydantic import BaseModel


class RequestMetrics(BaseModel):
    """
    请求指标模型
    """
    path: str
    method: str
    status_code: int
    response_time: float
    timestamp: datetime
    client_ip: str
    user_agent: str


class PerformanceSnapshot(BaseModel):
    """
    性能快照模型
    """
    timestamp: datetime
    cpu_usage: float
    memory_usage: float
    active_requests: int
    request_rate: float
    avg_response_time: float
    error_rate: float
    slow_requests: int


class EndpointStats(BaseModel):
    """
    端点统计模型
    """
    endpoint: str
    count: int
    total_time: float
    errors: int
    avg_time: float


class ErrorRequest(BaseModel):
    """
    错误请求模型
    """
    timestamp: str
    method: str
    path: str
    status_code: int
    response_time: float
    client_ip: str


class MonitoringOverview(BaseModel):
    """
    监控概览模型
    """
    performance: PerformanceSnapshot
    top_endpoints: List[EndpointStats]
    recent_errors: List[ErrorRequest]
    alerts: List[str]


class MonitoringConfig(BaseModel):
    """
    监控配置模型
    """
    slow_request_threshold: float = 1.0
    error_threshold: float = 0.05
    cpu_threshold: float = 80.0
    memory_threshold: float = 80.0
    max_history: int = 1000
    window_size: int = 60
