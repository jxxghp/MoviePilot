from typing import Any, List

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from app import schemas
from app.core.security import verify_apitoken
from app.monitoring import monitor, get_metrics_response
from app.schemas.monitoring import (
    PerformanceSnapshot,
    EndpointStats,
    ErrorRequest,
    MonitoringOverview
)

router = APIRouter()


@router.get("/overview", summary="获取监控概览", response_model=schemas.MonitoringOverview)
def get_overview(_: str = Depends(verify_apitoken)) -> Any:
    """
    获取完整的监控概览信息
    """
    # 获取性能快照
    performance = monitor.get_performance_snapshot()

    # 获取最活跃端点
    top_endpoints = monitor.get_top_endpoints(limit=10)

    # 获取最近错误
    recent_errors = monitor.get_recent_errors(limit=20)

    # 检查告警
    alerts = monitor.check_alerts()

    return MonitoringOverview(
        performance=PerformanceSnapshot(
            timestamp=performance.timestamp,
            cpu_usage=performance.cpu_usage,
            memory_usage=performance.memory_usage,
            active_requests=performance.active_requests,
            request_rate=performance.request_rate,
            avg_response_time=performance.avg_response_time,
            error_rate=performance.error_rate,
            slow_requests=performance.slow_requests
        ),
        top_endpoints=[EndpointStats(**endpoint) for endpoint in top_endpoints],
        recent_errors=[ErrorRequest(**error) for error in recent_errors],
        alerts=alerts
    )


@router.get("/performance", summary="获取性能快照", response_model=schemas.PerformanceSnapshot)
def get_performance(_: str = Depends(verify_apitoken)) -> Any:
    """
    获取当前性能快照
    """
    snapshot = monitor.get_performance_snapshot()
    return PerformanceSnapshot(
        timestamp=snapshot.timestamp,
        cpu_usage=snapshot.cpu_usage,
        memory_usage=snapshot.memory_usage,
        active_requests=snapshot.active_requests,
        request_rate=snapshot.request_rate,
        avg_response_time=snapshot.avg_response_time,
        error_rate=snapshot.error_rate,
        slow_requests=snapshot.slow_requests
    )


@router.get("/endpoints", summary="获取端点统计", response_model=List[schemas.EndpointStats])
def get_endpoints(
        limit: int = Query(10, ge=1, le=50, description="返回的端点数量"),
        _: str = Depends(verify_apitoken)
) -> Any:
    """
    获取最活跃的API端点统计
    """
    endpoints = monitor.get_top_endpoints(limit=limit)
    return [EndpointStats(**endpoint) for endpoint in endpoints]


@router.get("/errors", summary="获取错误请求", response_model=List[schemas.ErrorRequest])
def get_errors(
        limit: int = Query(20, ge=1, le=100, description="返回的错误数量"),
        _: str = Depends(verify_apitoken)
) -> Any:
    """
    获取最近的错误请求记录
    """
    errors = monitor.get_recent_errors(limit=limit)
    return [ErrorRequest(**error) for error in errors]


@router.get("/alerts", summary="获取告警信息", response_model=List[str])
def get_alerts(_: str = Depends(verify_apitoken)) -> Any:
    """
    获取当前告警信息
    """
    return monitor.check_alerts()


@router.get("/metrics", summary="Prometheus指标")
def get_prometheus_metrics(_: str = Depends(verify_apitoken)) -> Any:
    """
    获取Prometheus格式的监控指标
    """
    return get_metrics_response()


@router.get("/dashboard", summary="监控仪表板", response_class=HTMLResponse)
def get_dashboard(_: str = Depends(verify_apitoken)) -> Any:
    """
    获取实时监控仪表板HTML页面
    """
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>MoviePilot 性能监控仪表板</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0;
                padding: 20px;
                background-color: #f5f5f5;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
            }
            .header {
                text-align: center;
                margin-bottom: 30px;
                color: #333;
            }
            .metrics-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }
            .metric-card {
                background: white;
                padding: 20px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                text-align: center;
            }
            .metric-value {
                font-size: 2em;
                font-weight: bold;
                color: #2196F3;
            }
            .metric-label {
                color: #666;
                margin-top: 5px;
            }
            .chart-container {
                background: white;
                padding: 20px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                margin-bottom: 20px;
            }
            .alerts {
                background: #fff3cd;
                border: 1px solid #ffeaa7;
                border-radius: 5px;
                padding: 15px;
                margin-bottom: 20px;
            }
            .alert-item {
                color: #856404;
                margin: 5px 0;
            }
            .refresh-btn {
                background: #2196F3;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                cursor: pointer;
                margin-bottom: 20px;
            }
            .refresh-btn:hover {
                background: #1976D2;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎬 MoviePilot 性能监控仪表板</h1>
                <button class="refresh-btn" onclick="refreshData()">刷新数据</button>
            </div>
            
            <div id="alerts" class="alerts" style="display: none;">
                <h3>⚠️ 告警信息</h3>
                <div id="alerts-list"></div>
            </div>
            
            <div class="metrics-grid">
                <div class="metric-card">
                    <div class="metric-value" id="cpu-usage">--</div>
                    <div class="metric-label">CPU使用率 (%)</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value" id="memory-usage">--</div>
                    <div class="metric-label">内存使用率 (%)</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value" id="active-requests">--</div>
                    <div class="metric-label">活跃请求数</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value" id="request-rate">--</div>
                    <div class="metric-label">请求率 (req/min)</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value" id="avg-response-time">--</div>
                    <div class="metric-label">平均响应时间 (s)</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value" id="error-rate">--</div>
                    <div class="metric-label">错误率 (%)</div>
                </div>
            </div>
            
            <div class="chart-container">
                <h3>📊 性能趋势</h3>
                <canvas id="performanceChart" width="400" height="200"></canvas>
            </div>
            
            <div class="chart-container">
                <h3>🔥 最活跃端点</h3>
                <canvas id="endpointsChart" width="400" height="200"></canvas>
            </div>
        </div>
        
        <script>
            let performanceChart, endpointsChart;
            let performanceData = {
                labels: [],
                cpu: [],
                memory: [],
                requests: []
            };
            
            // 初始化图表
            function initCharts() {
                const ctx1 = document.getElementById('performanceChart').getContext('2d');
                performanceChart = new Chart(ctx1, {
                    type: 'line',
                    data: {
                        labels: performanceData.labels,
                        datasets: [{
                            label: 'CPU使用率 (%)',
                            data: performanceData.cpu,
                            borderColor: '#2196F3',
                            backgroundColor: 'rgba(33, 150, 243, 0.1)',
                            tension: 0.4
                        }, {
                            label: '内存使用率 (%)',
                            data: performanceData.memory,
                            borderColor: '#4CAF50',
                            backgroundColor: 'rgba(76, 175, 80, 0.1)',
                            tension: 0.4
                        }, {
                            label: '活跃请求数',
                            data: performanceData.requests,
                            borderColor: '#FF9800',
                            backgroundColor: 'rgba(255, 152, 0, 0.1)',
                            tension: 0.4
                        }]
                    },
                    options: {
                        responsive: true,
                        scales: {
                            y: {
                                beginAtZero: true
                            }
                        }
                    }
                });
                
                const ctx2 = document.getElementById('endpointsChart').getContext('2d');
                endpointsChart = new Chart(ctx2, {
                    type: 'bar',
                    data: {
                        labels: [],
                        datasets: [{
                            label: '请求数',
                            data: [],
                            backgroundColor: 'rgba(33, 150, 243, 0.8)'
                        }]
                    },
                    options: {
                        responsive: true,
                        scales: {
                            y: {
                                beginAtZero: true
                            }
                        }
                    }
                });
            }
            
            // 更新性能数据
            function updatePerformanceData(data) {
                const now = new Date().toLocaleTimeString();
                
                performanceData.labels.push(now);
                performanceData.cpu.push(data.performance.cpu_usage);
                performanceData.memory.push(data.performance.memory_usage);
                performanceData.requests.push(data.performance.active_requests);
                
                // 保持最近20个数据点
                if (performanceData.labels.length > 20) {
                    performanceData.labels.shift();
                    performanceData.cpu.shift();
                    performanceData.memory.shift();
                    performanceData.requests.shift();
                }
                
                // 更新图表
                performanceChart.data.labels = performanceData.labels;
                performanceChart.data.datasets[0].data = performanceData.cpu;
                performanceChart.data.datasets[1].data = performanceData.memory;
                performanceChart.data.datasets[2].data = performanceData.requests;
                performanceChart.update();
                
                // 更新端点图表
                const endpointLabels = data.top_endpoints.map(e => e.endpoint.substring(0, 20));
                const endpointData = data.top_endpoints.map(e => e.count);
                
                endpointsChart.data.labels = endpointLabels;
                endpointsChart.data.datasets[0].data = endpointData;
                endpointsChart.update();
            }
            
            // 更新指标显示
            function updateMetrics(data) {
                document.getElementById('cpu-usage').textContent = data.performance.cpu_usage.toFixed(1);
                document.getElementById('memory-usage').textContent = data.performance.memory_usage.toFixed(1);
                document.getElementById('active-requests').textContent = data.performance.active_requests;
                document.getElementById('request-rate').textContent = data.performance.request_rate.toFixed(0);
                document.getElementById('avg-response-time').textContent = data.performance.avg_response_time.toFixed(3);
                document.getElementById('error-rate').textContent = (data.performance.error_rate * 100).toFixed(2);
            }
            
            // 更新告警
            function updateAlerts(alerts) {
                const alertsDiv = document.getElementById('alerts');
                const alertsList = document.getElementById('alerts-list');
                
                if (alerts.length > 0) {
                    alertsDiv.style.display = 'block';
                    alertsList.innerHTML = alerts.map(alert => 
                        `<div class="alert-item">⚠️ ${alert}</div>`
                    ).join('');
                } else {
                    alertsDiv.style.display = 'none';
                }
            }
            
            // 获取URL中的token参数
            function getTokenFromUrl() {
                const urlParams = new URLSearchParams(window.location.search);
                return urlParams.get('token');
            }
            
            // 刷新数据
            async function refreshData() {
                try {
                    const token = getTokenFromUrl();
                    if (!token) {
                        console.error('未找到token参数');
                        return;
                    }
                    
                    const response = await fetch(`/api/v1/monitoring/overview?token=${token}`);
                    
                    if (response.ok) {
                        const data = await response.json();
                        updateMetrics(data);
                        updatePerformanceData(data);
                        updateAlerts(data.alerts);
                    }
                } catch (error) {
                    console.error('获取监控数据失败:', error);
                }
            }
            
            // 页面加载完成后初始化
            document.addEventListener('DOMContentLoaded', function() {
                initCharts();
                refreshData();
                
                // 每5秒自动刷新
                setInterval(refreshData, 5000);
            });
        </script>
    </body>
    </html>
    """)
