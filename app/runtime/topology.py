"""MoviePilot 进程拓扑约束。"""

from typing import Optional


class UnsupportedProcessTopologyError(RuntimeError):
    """启动配置会复制当前只能单实例运行的控制面。"""


def process_topology_issue(*, workers: int, safe_mode: bool) -> Optional[str]:
    """
    返回当前进程拓扑不可运行的原因。

    :param workers: API worker 进程数
    :param safe_mode: 是否只启动安全模式数据面
    :return: 支持时返回 None，否则返回可直接展示的错误说明
    """
    if workers < 1:
        return "API_WORKERS 必须大于等于 1"
    if workers == 1 or safe_mode:
        return None
    return (
        "MoviePilot V3 全功能模式仅支持 API_WORKERS=1；"
        f"当前配置为 {workers}，每个 worker 都会重复启动插件、调度器、监控器和工作流。"
        "请将 API_WORKERS 改为 1 后重启。故障排查可以临时启用 "
        "MOVIEPILOT_SAFE_MODE=true，但安全模式不是全功能扩容方案。"
    )


def validate_process_topology(*, workers: int, safe_mode: bool) -> None:
    """
    在启动任何持久化或后台副作用前校验进程拓扑。

    :param workers: API worker 进程数
    :param safe_mode: 是否只启动安全模式数据面
    :raises UnsupportedProcessTopologyError: 当前拓扑会复制全功能控制面
    """
    issue = process_topology_issue(workers=workers, safe_mode=safe_mode)
    if issue:
        raise UnsupportedProcessTopologyError(issue)
