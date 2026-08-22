"""定时作业的声明类型。

调度引擎按声明登记作业：一条作业声明对应一份运行状态登记，
并可展开为零到多条调度器触发（仅手动执行的作业没有触发）。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class ScheduledTrigger:
    """
    一条定时作业触发登记。

    :param trigger: 触发类型或已构建的触发器对象
    :param options: 透传给调度器的触发参数
    :param suffix: 同一作业登记多条触发时附加到调度器任务 id 的后缀
    :param name: 调度器任务显示名，缺省时沿用作业名
    :param replace_existing: 同 id 任务是否覆盖登记
    """

    trigger: Any
    options: Dict[str, Any] = field(default_factory=dict)
    suffix: str = ""
    name: Optional[str] = None
    replace_existing: bool = False


@dataclass(frozen=True)
class ScheduledJob:
    """
    一条定时作业声明。

    :param id: 作业标识，同时是运行状态登记键
    :param name: 作业显示名
    :param func: 作业执行体
    :param kwargs: 调用执行体时透传的关键字参数
    :param provider_name: 作业提供方显示名，缺省由引擎按系统作业展示
    :param manual: 是否只允许手动执行
    :param triggers: 作业展开的调度器触发登记
    """

    id: str
    name: str
    func: Callable[..., Any]
    kwargs: Optional[Dict[str, Any]] = None
    provider_name: Optional[str] = None
    manual: bool = False
    triggers: Tuple[ScheduledTrigger, ...] = ()
