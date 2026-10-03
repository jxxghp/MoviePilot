"""在当前 Agent 会话的持久 Python 内核中处理多次只读工具结果。"""

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.agent.code.authority import CURRENT_CELL
from app.agent.code.manager import get_code_session_manager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.settings import get_runtime_setting


class ExecuteCodeInput(BaseModel):  # type: ignore[misc]
    """代码与显式重置状态；身份、工具集合和预算由宿主提供，不由模型选择。"""

    code: str = Field(..., min_length=1, max_length=200_000, description='Python code to execute in the persistent session kernel.')
    reset: bool = Field(False, description='Discard all previous Python variables and imports before running this cell.')


class ExecuteCodeTool(MoviePilotTool):
    """与管理员命令执行使用相同主机权限边界；工具 RPC 仅开放安全只读能力。"""

    name: str = 'execute_code'
    description: str = (
        'Run Python for 3+ read-only tool calls with loops, paging, filtering, joins or aggregation. '
        'Use ordinary tool calls for one call or results requiring full model reasoning. '
        'Variables, imports and loaded data persist across calls in this conversation. '
        'Import enabled helpers with `from moviepilot_tools import moviepilot_api, read_file, search_web`; '
        'only helpers enabled in the current model request are available. Helpers return parsed JSON. '
        'Load the relevant domain Skill first and use its exact moviepilot_api operation IDs and arguments. '
        'The API helper permits only safe read operations, never writes, secret or unknown-sensitivity reads. '
        'json_parse, shell_quote and retry are also available from moviepilot_tools. '
        'Limits: 300 seconds, 50 tool calls per cell, stdout 50KB, stderr 10KB, full stdout spill up to 5MB. '
        'Print only the useful answer; oversized output returns a file to read in slices. '
        'Exceptions preserve variables; timeout, cancellation, reset or sys.exit loses kernel state. '
        'This is administrator Python on the host, not an OS sandbox. No per-call user confirmation is needed.'
    )
    args_schema: type[BaseModel] = ExecuteCodeInput
    require_admin: bool = True
    tags: list[str] = [ToolTag.Admin, ToolTag.Command, ToolTag.Write]
    result_max_chars: ClassVar[int | None] = None

    def get_tool_message(self, **kwargs: Any) -> str:
        """不把完整程序或参数注入用户通知。"""
        return '执行 Python 数据分析'

    def _get_run_timeout_seconds(self, **kwargs: Any) -> None:
        """内核拥有300秒截止时间及进程回收，外层通用工具定时器不抢先丢弃清理结果。"""
        return None

    async def run(self, **kwargs: Any) -> str:
        """只有真实宿主工具链绑定的当前 cell 可执行，直接外部调用不能获得默认身份。"""
        arguments = ExecuteCodeInput(**kwargs)
        authority = CURRENT_CELL.get()
        if authority is None or not authority.available() or authority.scope.user_id != str(self._user_id):
            return json.dumps({'success': False, 'error': '当前入口没有可用的 Python 会话身份。'}, ensure_ascii=False)
        root = Path(get_runtime_setting('CONFIG_PATH')) / 'agent' / 'runtime' / 'code'
        manager = get_code_session_manager(root)
        result = await manager.execute(authority, arguments.code, reset=arguments.reset, cwd=Path(get_runtime_setting('ROOT_PATH')))
        return json.dumps(result, ensure_ascii=False)
