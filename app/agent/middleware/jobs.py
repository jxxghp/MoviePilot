import re
from collections.abc import Awaitable, Callable
from typing import Annotated, NotRequired, TypedDict

import yaml  # noqa
from anyio import Path as AsyncPath
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,  # noqa
    ResponseT,
)
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.agent.middleware.utils import append_to_system_message
from app.log import logger

# JOB.md 文件最大限制为 1MB
MAX_JOB_FILE_SIZE = 1 * 1024 * 1024
ACTIVE_JOB_STATUSES = ("pending", "in_progress")


class JobMetadata(TypedDict):
    """Job 元数据。"""

    path: str
    """JOB.md 文件路径。"""

    id: str
    """Job 标识符（目录名）。"""

    name: str
    """Job 名称。"""

    description: str
    """Job 描述。"""

    schedule: str
    """调度类型: once（一次性）/ recurring（重复性）。"""

    status: str
    """当前状态: pending / in_progress / completed / cancelled。"""

    last_run: str | None
    """上次执行时间。"""


class JobsState(AgentState):
    """jobs 中间件状态。"""

    jobs_metadata: NotRequired[Annotated[list[JobMetadata], PrivateStateAttr]]
    """已加载的 job 元数据列表，不传播给父 agent。"""


class JobsStateUpdate(TypedDict):
    """jobs 中间件状态更新项。"""

    jobs_metadata: list[JobMetadata]
    """待合并的 job 元数据列表。"""


def _parse_job_metadata(
    content: str,
    job_path: str,
    job_id: str,
) -> JobMetadata | None:
    """从 JOB.md 内容中解析 YAML 前言并验证元数据。"""
    if len(content) > MAX_JOB_FILE_SIZE:
        logger.warning(
            "Skipping %s: content too large (%d bytes)", job_path, len(content)
        )
        return None

    # 匹配 --- 分隔的 YAML 前言
    frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n"
    match = re.match(frontmatter_pattern, content, re.DOTALL)
    if not match:
        logger.warning("Skipping %s: no valid YAML frontmatter found", job_path)
        return None
    frontmatter_str = match.group(1)

    # 解析 YAML
    try:
        frontmatter_data = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML in %s: %s", job_path, e)
        return None

    if not isinstance(frontmatter_data, dict):
        logger.warning("Skipping %s: frontmatter is not a mapping", job_path)
        return None

    # Job 名称和描述
    name = str(frontmatter_data.get("name", "")).strip()
    description = str(frontmatter_data.get("description", "")).strip()
    if not name:
        logger.warning("Skipping %s: missing required 'name'", job_path)
        return None

    # 调度类型
    schedule = str(frontmatter_data.get("schedule", "once")).strip().lower()
    if schedule not in ("once", "recurring"):
        schedule = "once"

    # 状态
    status = str(frontmatter_data.get("status", "pending")).strip().lower()
    if status not in ("pending", "in_progress", "completed", "cancelled"):
        status = "pending"

    # 上次执行时间
    last_run = str(frontmatter_data.get("last_run", "")).strip() or None

    return JobMetadata(
        id=job_id,
        name=name,
        description=description,
        path=job_path,
        schedule=schedule,
        status=status,
        last_run=last_run,
    )


async def _alist_jobs(source_path: AsyncPath) -> list[JobMetadata]:
    """异步列出指定路径下的所有任务。

    扫描包含 JOB.md 的目录并解析其元数据，遇到非法 UTF-8 字节时以替换字符兜底。
    """
    jobs: list[JobMetadata] = []

    if not await source_path.exists():
        return []

    # 查找所有任务目录（包含 JOB.md 的目录）
    job_dirs: list[AsyncPath] = []
    async for path in source_path.iterdir():
        if await path.is_dir() and await (path / "JOB.md").is_file():
            job_dirs.append(path)

    if not job_dirs:
        return []

    # 显式按目录名排序，避免文件系统返回顺序不稳定时破坏提示词缓存命中。
    job_dirs.sort(key=lambda p: p.name.casefold())

    # 解析 JOB.md
    for job_path in job_dirs:
        job_md_path = job_path / "JOB.md"

        job_content = await job_md_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        # 解析元数据
        job_metadata = _parse_job_metadata(
            content=job_content,
            job_path=str(job_md_path),
            job_id=job_path.name,
        )
        if job_metadata:
            jobs.append(job_metadata)

    return jobs


def filter_active_jobs(jobs_metadata: list[JobMetadata]) -> list[JobMetadata]:
    """筛选需要参与心跳检查的活跃任务。

    这里严格以任务状态为准，只保留 `pending` / `in_progress`。
    `recurring` 任务执行完成后按约定应回写为 `pending`，因此无需再额外放宽
    到 `completed`，避免已结束任务被重复注入后台心跳。
    """
    return [
        job for job in jobs_metadata if job.get("status") in ACTIVE_JOB_STATUSES
    ]


async def load_jobs_metadata(source_paths: list[str]) -> list[JobMetadata]:
    """按顺序加载多个 jobs 目录下的任务元数据。"""
    all_jobs: list[JobMetadata] = []
    for source_path_str in source_paths:
        source_path = AsyncPath(source_path_str)
        if not await source_path.exists():
            await source_path.mkdir(parents=True, exist_ok=True)
            continue
        source_jobs = await _alist_jobs(source_path)
        all_jobs.extend(source_jobs)
    return all_jobs


JOBS_SYSTEM_PROMPT = """
<jobs_system>
You have a scheduled jobs system for user-requested delayed or recurring work.

**Jobs Location:** `{jobs_location}`

**Current Jobs:**

{jobs_list}

Rules:
- For new delayed, recurring, reminder, or monitoring work, use the dedicated
  `create_agent_task`, `query_agent_tasks`, `update_agent_task`, `run_agent_task`,
  and `delete_agent_task` tools. These tools use integer task IDs. Do not create
  or edit JOB.md files for new tasks.
- Use `query_schedulers` and `run_scheduler` only for MoviePilot system, plugin,
  or workflow runtime services; never pass their string job IDs to Agent task tools.
- Do not create tasks for immediate one-time work or work already handled by MoviePilot schedulers.
- Entries listed above are legacy JOB.md tasks. Read their files only when a heartbeat asks you to execute them.
- During heartbeat checks, act only on `pending` or `in_progress` jobs, update status/last_run/logs, and leave recurring jobs `pending` after each run.
</jobs_system>
"""


class JobsMiddleware(AgentMiddleware[JobsState, ContextT, ResponseT]):  # noqa
    """加载并向系统提示词注入 Agent Jobs 的中间件。

    扫描 jobs 目录下的 JOB.md 文件，解析元数据并注入到系统提示词中，
    使智能体了解当前的长期任务及其状态。
    """

    state_schema = JobsState

    def __init__(self, *, sources: list[str]) -> None:
        """初始化 Jobs 中间件。"""
        self.sources = sources
        self.system_prompt_template = JOBS_SYSTEM_PROMPT

    @staticmethod
    def _format_jobs_list(jobs: list[JobMetadata]) -> str:
        """格式化任务元数据列表用于系统提示词。"""
        if not jobs:
            return "(No active legacy JOB.md tasks. Use create_agent_task for new scheduled work.)"

        lines = []
        for job in jobs:
            status_emoji = {
                "pending": "⏳",
                "in_progress": "🔄",
                "completed": "✅",
                "cancelled": "❌",
            }.get(job["status"], "❓")

            schedule_label = (
                "recurring (重复)"
                if job["schedule"] == "recurring"
                else "once (一次性)"
            )
            desc_line = (
                f"- {status_emoji} **{job['id']}**: {job['name']}"
                f" [{schedule_label}] - {job['description']}"
            )
            if job.get("last_run"):
                desc_line += f" (上次执行: {job['last_run']})"
            lines.append(desc_line)
            lines.append(f"  -> Read `{job['path']}` for full details")

        return "\n".join(lines)

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """将任务文档注入模型请求的系统消息中。"""
        jobs_metadata = request.state.get("jobs_metadata", [])  # noqa

        # 仅注入真正活跃的任务，避免把已完成任务继续塞进心跳上下文。
        active_jobs = filter_active_jobs(jobs_metadata)

        jobs_list = self._format_jobs_list(active_jobs)
        jobs_location = self.sources[0] if self.sources else ""

        jobs_section = self.system_prompt_template.format(
            jobs_location=jobs_location,
            jobs_list=jobs_list,
        )

        new_system_message = append_to_system_message(
            request.system_message, jobs_section
        )

        return request.override(system_message=new_system_message)

    async def abefore_agent(  # noqa
        self, state: JobsState, runtime: Runtime, config: RunnableConfig
    ) -> JobsStateUpdate | None:
        """在 Agent 执行前异步加载任务元数据。

        """
        return JobsStateUpdate(
            jobs_metadata=await load_jobs_metadata(self.sources)
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """在模型调用时注入任务文档。"""
        modified_request = self.modify_request(request)
        return await handler(modified_request)


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "JobMetadata",
    "JobsMiddleware",
    "filter_active_jobs",
    "load_jobs_metadata",
]
