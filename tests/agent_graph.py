"""
通过真实图资源 owner 为 Agent 行为测试装配已发布图。
"""

from datetime import datetime
from typing import Any, Optional

from app.agent.graph import CompiledAgentBundle, GraphResources
from app.agent.orchestrator import MoviePilotAgent


def publish_graph(
    agent: MoviePilotAgent,
    *,
    graph: Optional[Any] = None,
    models: tuple[Any, ...] = (),
    middlewares: tuple[object, ...] = (),
) -> CompiledAgentBundle:
    """
    为无需真实构图的回归装配资源完整的缓存图，不模拟生命周期实现。

    :param agent: 被测会话执行对象
    :param graph: 供执行的测试图；None 时使用占位对象
    :param models: 需观察客户端释放的模型
    :param middlewares: 需观察关闭及未收敛重试的控制器
    :return: 已发布图，供断言资源归属和缓存身份
    """
    resources = GraphResources()
    for model in models:
        resources.own_model(model)
    resources.own_middlewares(middlewares)
    bundle = CompiledAgentBundle(
        signature=("test",), agent=graph if graph is not None else object(),
        streaming=False, created_at=datetime.now(), resources=resources,
    )
    agent._graphs.publish(bundle)
    return bundle
