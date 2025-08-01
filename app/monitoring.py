import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Any

import psutil
from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import settings
from app.log import logger


@dataclass
class RequestMetrics:
    """
    请求指标数据类
    """
    path: str
    method: str
    status_code: int
    response_time: float
    timestamp: datetime
    client_ip: str
    user_agent: str


@dataclass
class PerformanceSnapshot:
    """
    性能快照数据类
    """
    timestamp: datetime
    cpu_usage: float
    memory_usage: float
    active_requests: int
    request_rate: float
    avg_response_time: float
    error_rate: float
    slow_requests: int


class FastAPIMonitor:
    """
    FastAPI性能监控器
    """

    def __init__(self, max_history: int = 1000, window_size: int = 60):
        self.max_history = max_history
        self.window_size = window_size  # 秒

        # 请求历史记录
        self.request_history: deque = deque(maxlen=max_history)

        # 实时统计
        self.active_requests = 0
        self.total_requests = 0
        self.error_requests = 0
        self.slow_requests = 0  # 响应时间超过1秒的请求

        # 时间窗口统计
        self.window_requests: deque = deque(maxlen=window_size)
        self.window_response_times: deque = deque(maxlen=window_size)

        # 线程锁
        self._lock = threading.Lock()

        # 性能阈值
        self.slow_request_threshold = 1.0  # 1秒
        self.error_threshold = 0.05  # 5%
        self.cpu_threshold = 80.0  # 80%
        self.memory_threshold = 80.0  # 80%

        # 告警状态
        self.alerts: List[str] = []

        logger.info("FastAPI性能监控器已初始化")

    def record_request(self, request: Request, response: Response, response_time: float):
        """
        记录请求指标
        """
        with self._lock:
            # 创建请求指标
            metrics = RequestMetrics(
                path=str(request.url.path),
                method=request.method,
                status_code=response.status_code,
                response_time=response_time,
                timestamp=datetime.now(),
                client_ip=request.client.host if request.client else "unknown",
                user_agent=request.headers.get("user-agent", "unknown")
            )

            # 添加到历史记录
            self.request_history.append(metrics)

            # 更新统计
            self.total_requests += 1
            if response.status_code >= 400:
                self.error_requests += 1
            if response_time > self.slow_request_threshold:
                self.slow_requests += 1

            # 添加到时间窗口
            self.window_requests.append(metrics)
            self.window_response_times.append(response_time)

    def start_request(self):
        """
        开始处理请求
        """
        with self._lock:
            self.active_requests += 1

    def end_request(self):
        """
        结束处理请求
        """
        with self._lock:
            self.active_requests = max(0, self.active_requests - 1)

    def get_performance_snapshot(self) -> PerformanceSnapshot:
        """
        获取性能快照
        """
        with self._lock:
            now = datetime.now()

            # 计算请求率（每分钟）
            recent_requests = [
                req for req in self.window_requests
                if now - req.timestamp < timedelta(seconds=self.window_size)
            ]
            request_rate = len(recent_requests) / (self.window_size / 60)

            # 计算平均响应时间
            recent_response_times = [
                rt for rt in self.window_response_times
                if len(self.window_response_times) > 0
            ]
            avg_response_time = sum(recent_response_times) / len(recent_response_times) if recent_response_times else 0

            # 计算错误率
            error_rate = self.error_requests / self.total_requests if self.total_requests > 0 else 0

            # 系统资源使用率
            cpu_usage = psutil.cpu_percent(interval=0.1)
            memory_usage = psutil.virtual_memory().percent

            return PerformanceSnapshot(
                timestamp=now,
                cpu_usage=cpu_usage,
                memory_usage=memory_usage,
                active_requests=self.active_requests,
                request_rate=request_rate,
                avg_response_time=avg_response_time,
                error_rate=error_rate,
                slow_requests=self.slow_requests
            )

    def get_top_endpoints(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取最活跃的端点
        """
        with self._lock:
            endpoint_stats = defaultdict(lambda: {
                'count': 0,
                'total_time': 0,
                'errors': 0,
                'avg_time': 0
            })

            for req in self.request_history:
                key = f"{req.method} {req.path}"
                endpoint_stats[key]['count'] += 1
                endpoint_stats[key]['total_time'] += req.response_time
                if req.status_code >= 400:
                    endpoint_stats[key]['errors'] += 1

            # 计算平均时间
            for stats in endpoint_stats.values():
                if stats['count'] > 0:
                    stats['avg_time'] = stats['total_time'] / stats['count']

            # 按请求数量排序
            sorted_endpoints = sorted(
                [{'endpoint': k, **v} for k, v in endpoint_stats.items()],
                key=lambda x: x['count'],
                reverse=True
            )

            return sorted_endpoints[:limit]

    def get_recent_errors(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        获取最近的错误请求
        """
        with self._lock:
            errors = [
                {
                    'timestamp': req.timestamp.isoformat(),
                    'method': req.method,
                    'path': req.path,
                    'status_code': req.status_code,
                    'response_time': req.response_time,
                    'client_ip': req.client_ip
                }
                for req in self.request_history
                if req.status_code >= 400
            ]
            return errors[-limit:]

    def check_alerts(self) -> List[str]:
        """
        检查告警条件
        """
        snapshot = self.get_performance_snapshot()
        alerts = []

        if snapshot.error_rate > self.error_threshold:
            alerts.append(f"错误率过高: {snapshot.error_rate:.2%}")

        if snapshot.cpu_usage > self.cpu_threshold:
            alerts.append(f"CPU使用率过高: {snapshot.cpu_usage:.1f}%")

        if snapshot.memory_usage > self.memory_threshold:
            alerts.append(f"内存使用率过高: {snapshot.memory_usage:.1f}%")

        if snapshot.avg_response_time > self.slow_request_threshold:
            alerts.append(f"平均响应时间过长: {snapshot.avg_response_time:.2f}s")

        if snapshot.request_rate > 1000:  # 每分钟1000请求
            alerts.append(f"请求率过高: {snapshot.request_rate:.0f} req/min")

        self.alerts = alerts
        return alerts


# 全局监控实例
monitor = FastAPIMonitor()


def setup_prometheus_metrics(app):
    """
    设置Prometheus指标
    """

    if not settings.PERFORMANCE_MONITOR_ENABLE:
        return

    # 创建Prometheus指标
    request_counter = Counter(
        "http_requests_total",
        "Total number of HTTP requests",
        ["method", "endpoint", "status"]
    )

    request_duration = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "endpoint"]
    )

    active_requests = Gauge(
        "http_active_requests",
        "Number of active HTTP requests"
    )

    # 自定义指标收集函数
    def custom_metrics(request: Request, response: Response, response_time: float):
        request_counter.labels(
            method=request.method,
            endpoint=request.url.path,
            status=response.status_code
        ).inc()

        request_duration.labels(
            method=request.method,
            endpoint=request.url.path
        ).observe(response_time)

        active_requests.set(monitor.active_requests)

    # 设置Prometheus监控
    Instrumentator().instrument(app).expose(app, include_in_schema=False, should_gzip=True)

    # 添加自定义指标
    @app.middleware("http")
    async def monitor_middleware(request: Request, call_next):
        start_time = time.time()

        # 开始请求
        monitor.start_request()

        try:
            response = await call_next(request)
            response_time = time.time() - start_time

            # 记录请求指标
            monitor.record_request(request, response, response_time)

            # 更新Prometheus指标
            custom_metrics(request, response, response_time)

            return response
        except Exception as e:
            response_time = time.time() - start_time
            logger.error(f"请求处理异常: {e}")

            # 创建错误响应
            response = Response(
                content=str(e),
                status_code=500,
                media_type="text/plain"
            )

            # 记录错误请求
            monitor.record_request(request, response, response_time)

            return response
        finally:
            # 结束请求
            monitor.end_request()

    logger.info("Prometheus指标监控已设置")


def get_metrics_response():
    """
    获取Prometheus指标响应
    """
    return PlainTextResponse(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
