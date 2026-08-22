"""内建远程命令清单。

命令词到业务实现的绑定认识全部业务域——站点、订阅、下载、整理、系统、消息与技能，
因此归组合根持有；命令中枢只负责合并三处来源、查表与执行，不认识任何业务链。

业务链在首次执行该命令时才物化。命令中枢是进程级单例，构造期就绑定实现会把七条业务链
连同它们的数据库登记、后台线程与队列一并拉起，而绝大多数命令在一个进程的生命周期里
一次也不会被敲。清单因此对业务实现型条目交出零参解析器，解析结果按条目缓存，同一命令
的后续执行复用同一个业务链实例。

条目有两种形状，执行语义不同：

- **业务实现型**带 ``provider``：实现自行声明参数个数，命令中枢按签名决定传入哪些上下文；
  进展与结果由实现自己发消息报告。
- **定时任务型**带 ``id`` 与 ``type``：实现不接收任何参数，开始与完成提示由命令中枢统一
  发出。任务标识同时是交给调用方的数据，启动实现由本模块按同一个标识构造。
"""

import threading
from typing import Callable, Dict, List

from app.application.messaging.gateway import CommandChain
from app.application.messaging.skill import SkillInteractionHandler
from app.application.orchestration.download import DownloadChain
from app.application.orchestration.message import MessageChain
from app.application.orchestration.site import SiteChain
from app.application.orchestration.subscribe import SubscribeChain
from app.application.orchestration.system import SystemChain
from app.application.orchestration.transfer import TransferChain
from app.scheduler import Scheduler


def _lazy(factory: Callable[[], Callable]) -> Callable[[], Callable]:
    """把业务实现工厂包装成只物化一次的零参解析器。

    :param factory: 构造业务链并取出其命令实现的工厂
    :return: 解析器；首次调用时物化业务链，其后返回同一个实现
    """
    lock = threading.Lock()
    holder: List[Callable] = []

    def resolve() -> Callable:
        if not holder:
            with lock:
                if not holder:
                    holder.append(factory())
        return holder[0]

    return resolve


def _scheduler_runner(job_id: str) -> Callable[[], None]:
    """构造启动指定定时任务的零参实现。

    :param job_id: 定时任务标识
    :return: 启动该定时任务的可调用对象
    """

    def run() -> None:
        Scheduler().start(job_id=job_id)

    return run


def builtin_commands() -> Dict[str, dict]:
    """交出内建命令表。

    :return: 命令词到命令表条目的映射，次序即渠道菜单的注册次序
    """
    return {
        "/cookiecloud": {
            "id": "cookiecloud",
            "type": "scheduler",
            "func": _scheduler_runner("cookiecloud"),
            "description": "同步站点",
            "category": "站点",
        },
        "/sites": {
            "provider": _lazy(lambda: SiteChain().remote_list),
            "description": "管理站点",
            "category": "站点",
            "data": {},
        },
        "/mediaserver_sync": {
            "id": "mediaserver_sync",
            "type": "scheduler",
            "func": _scheduler_runner("mediaserver_sync"),
            "description": "同步媒体服务器",
            "category": "管理",
        },
        "/subscribes": {
            "provider": _lazy(lambda: SubscribeChain().remote_list),
            "description": "管理订阅",
            "category": "订阅",
            "data": {},
        },
        "/downloading": {
            "provider": _lazy(lambda: DownloadChain().remote_downloading),
            "description": "正在下载",
            "category": "管理",
            "data": {},
        },
        "/transfer": {
            "id": "transfer",
            "type": "scheduler",
            "func": _scheduler_runner("transfer"),
            "description": "下载文件整理",
            "category": "管理",
        },
        "/redo": {
            "provider": _lazy(lambda: TransferChain().remote_transfer),
            "description": "手动整理",
            "data": {},
        },
        "/clear_cache": {
            "provider": _lazy(lambda: SystemChain().remote_clear_cache),
            "description": "清理缓存",
            "category": "管理",
            "data": {},
        },
        "/restart": {
            "provider": _lazy(lambda: SystemChain().restart),
            "description": "重启系统",
            "category": "管理",
            "data": {},
        },
        "/version": {
            "provider": _lazy(lambda: SystemChain().version),
            "description": "当前版本",
            "category": "管理",
            "data": {},
        },
        "/clear_session": {
            "provider": _lazy(lambda: MessageChain().remote_clear_session),
            "description": "清除会话",
            "category": "管理",
            "data": {},
        },
        "/stop_agent": {
            "provider": _lazy(lambda: MessageChain().remote_stop_agent),
            "description": "停止推理",
            "category": "管理",
            "data": {},
        },
        "/session_status": {
            "provider": _lazy(lambda: MessageChain().remote_session_status),
            "description": "会话状态",
            "category": "智能体",
            "data": {},
        },
        "/skills": {
            "provider": _lazy(
                lambda: SkillInteractionHandler(messenger=CommandChain()).remote_manage
            ),
            "description": "管理技能",
            "category": "智能体",
            "data": {},
        },
    }
