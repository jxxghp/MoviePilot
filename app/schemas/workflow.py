from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData
from app.schemas.context import Context, MediaInfo
from app.schemas.download import DownloadTask
from app.schemas.file import FileItem
from app.schemas.site import Site
from app.schemas.subscribe import Subscribe


class WorkflowExecutionConfig(BaseModel):
    """工作流调度器的执行参数。"""

    max_workers: Optional[int] = Field(default=None, ge=1, description="最大并发动作数")


class WorkflowNodeState(BaseModel):
    """工作流单个动作的持久化执行状态。"""

    state: Optional[str] = None
    attempt: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    message: Optional[str] = None


class WorkflowRuntimeState(BaseModel):
    """工作流调度器的实时进度摘要。"""

    progress: int = 0
    finished_actions: int = 0
    running_tasks: int = 0
    errors: dict[str, str] = Field(default_factory=dict)
    node_states: dict[str, str] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)


class WorkflowExecutionState(BaseModel):
    """可恢复的工作流结构化执行快照。"""

    version: int = 1
    nodes: dict[str, WorkflowNodeState] = Field(default_factory=dict)
    outputs: dict[str, JsonData] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    runtime: WorkflowRuntimeState = Field(default_factory=WorkflowRuntimeState)


class ActionPosition(BaseModel):
    """工作流画布中的动作坐标。"""

    x: float = 0
    y: float = 0


class ActionRetry(BaseModel):
    """动作失败后的重试策略。"""

    max_attempts: int = Field(default=1, ge=1)
    interval: float = Field(default=0, ge=0)
    backoff: float = Field(default=1, ge=1)


class Workflow(BaseModel):
    """
    工作流信息
    """
    id: Optional[int] = Field(default=None, description="工作流ID")
    name: Optional[str] = Field(default=None, description="工作流名称")
    description: Optional[str] = Field(default=None, description="工作流描述")
    timer: Optional[str] = Field(default=None, description="定时器")
    trigger_type: Optional[str] = Field(default='timer', description="触发类型：timer-定时触发 event-事件触发 manual-手动触发")
    event_type: Optional[str] = Field(default=None, description="事件类型（当trigger_type为event时使用）")
    event_conditions: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="事件条件（JSON格式，用于过滤事件）")
    state: Optional[str] = Field(default=None, description="状态")
    current_action: Optional[str] = Field(default=None, description="已执行动作")
    result: Optional[str] = Field(default=None, description="任务执行结果")
    run_count: Optional[int] = Field(default=0, description="已执行次数")
    actions: Optional[list["Action"]] = Field(default_factory=list, description="任务列表")
    flows: Optional[list["ActionFlow"]] = Field(default_factory=list, description="任务流")
    execution_config: Optional[WorkflowExecutionConfig] = Field(default_factory=WorkflowExecutionConfig, description="工作流执行配置")
    execution_state: Optional[WorkflowExecutionState] = Field(default_factory=WorkflowExecutionState, description="工作流结构化执行状态")
    add_time: Optional[str] = Field(default=None, description="创建时间")
    last_time: Optional[str] = Field(default=None, description="最后执行时间")

    model_config = ConfigDict(from_attributes=True)


class ActionParams(BaseModel):
    """
    动作基础参数
    """
    loop: Optional[bool] = Field(default=False, description="是否需要循环")
    loop_interval: Optional[int] = Field(default=0, description="循环间隔 (秒)")


class Action(BaseModel):
    """
    动作信息
    """
    id: Optional[str] = Field(default=None, description="动作ID")
    type: Optional[str] = Field(default=None, description="动作类型 (类名)")
    name: Optional[str] = Field(default=None, description="动作名称")
    description: Optional[str] = Field(default=None, description="动作描述")
    position: Optional[ActionPosition] = Field(default_factory=ActionPosition, description="位置")
    data: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="参数")
    inputs: Optional[List[str]] = Field(default_factory=list, description="动作输入声明")
    outputs: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="动作输出声明")
    join_policy: Optional[str] = Field(default=None, description="多上游节点汇合策略")
    fail_policy: Optional[str] = Field(default=None, description="动作失败后的工作流处理策略")
    branch_policy: Optional[str] = Field(default=None, description="多出边分支策略")
    concurrency_key: Optional[str] = Field(default=None, description="并发互斥键")
    timeout: Optional[int] = Field(default=None, description="动作执行超时时间（秒）")
    retry: Optional[ActionRetry] = Field(default=None, description="动作重试策略")


class ActionExecution(BaseModel):
    """
    动作执行情况
    """
    action: Optional[str] = Field(default=None, description="当前动作（名称）")
    result: Optional[bool] = Field(default=None, description="执行结果")
    message: Optional[str] = Field(default=None, description="执行消息")


class ActionContext(BaseModel):
    """
    动作基础上下文，各动作通用数据
    """
    content: Optional[str] = Field(default=None, description="文本类内容")
    torrents: Optional[List[Context]] = Field(default_factory=list, description="资源列表")
    medias: Optional[List[MediaInfo]] = Field(default_factory=list, description="媒体列表")
    fileitems: Optional[List[FileItem]] = Field(default_factory=list, description="文件列表")
    downloads: Optional[List[DownloadTask]] = Field(default_factory=list, description="下载任务列表")
    sites: Optional[List[Site]] = Field(default_factory=list, description="站点列表")
    subscribes: Optional[List[Subscribe]] = Field(default_factory=list, description="订阅列表")
    workflow_context: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="工作流全局上下文")
    node_outputs: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="节点输出数据")
    runtime_state: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="运行期状态")
    artifacts: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="大对象引用与产物数据")
    execute_history: Optional[List[ActionExecution]] = Field(default_factory=list, description="执行历史")
    progress: Optional[int] = Field(default=0, description="执行进度（%）")


class ActionResult(BaseModel):
    """
    动作执行结果。
    """
    success: Optional[bool] = Field(default=True, description="动作是否执行成功")
    message: Optional[str] = Field(default=None, description="动作执行消息")
    context: Optional[ActionContext] = Field(default=None, description="动作执行后的上下文")
    # 动作内部可暂存待序列化对象；API 输出前由工作流序列化器转为 JsonData。
    outputs: Optional[dict[str, Any]] = Field(default_factory=dict, description="当前节点显式输出")
    next_policy: Optional[str] = Field(default=None, description="动作完成后的调度策略")
    attempts: Optional[int] = Field(default=1, description="动作实际尝试次数")


class ActionFlow(BaseModel):
    """
    工作流流程
    """
    id: Optional[str] = Field(default=None, description="流程ID")
    source: Optional[str] = Field(default=None, description="源动作")
    target: Optional[str] = Field(default=None, description="目标动作")
    animated: Optional[bool] = Field(default=True, description="是否动画流程")
    data: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="流程扩展配置")
    condition: Optional[str] = Field(default=None, description="流转条件表达式")
    join_policy: Optional[str] = Field(default=None, description="目标节点汇合策略")
    branch_policy: Optional[str] = Field(default=None, description="源节点分支策略")


class WorkflowShare(BaseModel):
    """
    工作流分享信息
    """
    id: Optional[int] = Field(default=None, description="分享ID")
    share_title: Optional[str] = Field(default=None, description="分享标题")
    share_comment: Optional[str] = Field(default=None, description="分享说明")
    share_user: Optional[str] = Field(default=None, description="分享人")
    share_uid: Optional[str] = Field(default=None, description="分享人唯一ID")
    name: Optional[str] = Field(default=None, description="工作流名称")
    description: Optional[str] = Field(default=None, description="工作流描述")
    timer: Optional[str] = Field(default=None, description="定时器")
    trigger_type: Optional[str] = Field(default=None, description="触发类型")
    event_type: Optional[str] = Field(default=None, description="事件类型")
    event_conditions: Optional[str] = Field(default=None, description="事件条件")
    actions: Optional[str] = Field(default=None, description="任务列表(JSON字符串)")
    flows: Optional[str] = Field(default=None, description="任务流(JSON字符串)")
    context: Optional[str] = Field(default=None, description="执行上下文(JSON字符串)")
    date: Optional[str] = Field(default=None, description="分享时间")
    count: Optional[int] = Field(default=0, description="复用人次")

    model_config = ConfigDict(from_attributes=True)


class PluginWorkflowActionGroup(BaseModel):
    """单个插件声明的工作流动作组。"""

    plugin_id: str
    plugin_name: str
    actions: list[dict[str, JsonData]] = Field(default_factory=list)


class ActionContractField(BaseModel):
    """动作契约中的单个输入或输出字段。"""

    name: str
    label: str
    kind: str = "scalar"
    merge: Optional[str] = None
    identity: Optional[str] = None


class ActionContract(BaseModel):
    """工作流动作对编辑器公开的输入输出契约。"""

    inputs: list[ActionContractField] = Field(default_factory=list)
    outputs: list[ActionContractField] = Field(default_factory=list)
    condition_fields: list[ActionContractField] = Field(default_factory=list)
    concurrency_key: Optional[str] = None


class WorkflowActionDefinition(BaseModel):
    """可用于工作流编辑器的动作定义。"""

    type: str
    name: str
    description: Optional[str] = None
    contract: ActionContract = Field(default_factory=ActionContract)
    data: dict[str, JsonData] = Field(default_factory=dict)


class NameValueOption(BaseModel):
    """前端选项的显示文本和值。"""

    title: str
    value: str
