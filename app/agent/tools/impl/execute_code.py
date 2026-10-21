"""
在当前 Agent 会话的持久 Python 内核中处理多次只读工具结果。
"""

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.agent.code.authority import CURRENT_CELL
from app.agent.code.manager import get_code_session_manager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.settings import get_runtime_setting


class ExecuteCodeInput(BaseModel):
    """
    代码与显式重置状态；身份、工具集合和预算由宿主提供，不由模型选择。
    """

    code: str = Field(
        ...,
        min_length=1,
        max_length=200000,
        description="在持久化会话内核中执行的 Python 代码",
    )
    reset: bool = Field(
        False,
        description="执行本单元前是否丢弃此前所有 Python 变量和导入",
    )


class ExecuteCodeTool(MoviePilotTool):
    """
    与管理员命令执行使用相同主机权限边界；工具 RPC 仅开放安全只读能力。

    :cvar result_max_chars: 禁用基类再次截断，由代码内核自身管理输出配额和文件回退
    """

    name: str = Field(
        default="execute_code",
        description="供模型调用的稳定工具名称",
    )
    description: str = Field(
        default=(
            "需要循环、分页、筛选、关联或聚合三个及以上只读工具调用时运行 Python。"
            "单次调用或结果需要完整模型推理时使用普通工具调用。"
            "变量、导入和已加载数据在当前对话的多次调用间保留。"
            "通过 from moviepilot_tools import moviepilot_api, read_file, search_web 导入助手函数；"
            "仅当前模型请求已启用的助手可用，返回值为解析后的 JSON。"
            "先加载对应领域 Skill，使用其精确 moviepilot_api 操作标识与参数。"
            "API 助手仅允许安全读取，禁止写入、秘密读取及敏感性未知的读取。"
            "moviepilot_tools 还提供 json_parse、shell_quote 和 retry。"
            "每单元限制 300 秒、50 次工具调用、标准输出 50KB、标准错误 10KB，完整标准输出最多另存 5MB。"
            "只打印有用答案，过大输出会返回可分段读取的文件。"
            "异常保留变量；"
            "超时、取消、重置或 sys.exit 会丢失内核状态。"
            "这是在宿主机以管理员权限运行 Python，不是操作系统沙箱，无需逐次向用户确认。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    args_schema: type[BaseModel] = Field(
        default=ExecuteCodeInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )
    require_admin: bool = Field(
        default=True,
        description="是否仅允许系统管理员调用",
    )
    tags: list[str] = Field(
        default=[ToolTag.Admin, ToolTag.Command, ToolTag.Write],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    result_max_chars: ClassVar[int | None] = None

    def get_tool_message(self, **kwargs: Any) -> str:
        """
        不把完整程序或参数注入用户通知。
        """
        return '执行 Python 数据分析'

    def _get_run_timeout_seconds(self, **kwargs: Any) -> None:
        """
        内核拥有300秒截止时间及进程回收，外层通用工具定时器不抢先丢弃清理结果。
        """
        return None

    async def run(self, **kwargs: Any) -> str:  # type: ignore[override]
        """
        只有真实宿主工具链绑定的当前 cell 可执行，直接外部调用不能获得默认身份。
        """
        arguments = ExecuteCodeInput(**kwargs)
        authority = CURRENT_CELL.get()
        if authority is None or not authority.available() or authority.scope.user_id != str(self._user_id):
            return json.dumps({'success': False, 'error': '当前入口没有可用的 Python 会话身份。'}, ensure_ascii=False)
        root = Path(get_runtime_setting('CONFIG_PATH')) / 'agent' / 'runtime' / 'code'
        manager = get_code_session_manager(root)
        result = await manager.execute(authority, arguments.code, reset=arguments.reset, cwd=Path(get_runtime_setting('ROOT_PATH')))
        return json.dumps(result, ensure_ascii=False)
