"""
工作流动作、调度状态、执行上下文及分享接口的数据结构。
"""

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData
from app.schemas.context import Context, MediaInfo
from app.schemas.download import DownloadTask
from app.schemas.file import FileItem
from app.schemas.site import Site
from app.schemas.subscribe import Subscribe


class WorkflowExecutionConfig(BaseModel):
    """
    工作流调度器的执行参数。
    """

    max_workers: Optional[int] = Field(default=None, ge=1, description="最大并发动作数")


class WorkflowNodeState(BaseModel):
    """
    工作流单个动作的持久化执行状态。
    """

    state: Optional[str] = Field(default=None, description="当前业务状态")
    attempt: int = Field(default=0, description="当前节点的执行尝试次数")
    started_at: Optional[str] = Field(default=None, description="开始时间")
    finished_at: Optional[str] = Field(default=None, description="结束时间")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")


class WorkflowRuntimeState(BaseModel):
    """
    工作流调度器的实时进度摘要。
    """

    progress: int = Field(default=0, description="当前完成百分比")
    finished_actions: int = Field(default=0, description="已完成的工作流动作数量")
    running_tasks: int = Field(default=0, description="当前运行中的工作流任务数量")
    errors: dict[str, str] = Field(default_factory=dict, description="按动作标识组织的执行错误")
    node_states: dict[str, str] = Field(default_factory=dict, description="按动作标识组织的运行状态")
    attempts: dict[str, int] = Field(default_factory=dict, description="按动作标识记录的执行尝试次数")


class WorkflowExecutionState(BaseModel):
    """
    可恢复的工作流结构化执行快照。
    """

    version: int = Field(default=1, description="执行快照的数据结构版本")
    nodes: dict[str, WorkflowNodeState] = Field(default_factory=dict, description="按动作标识组织的持久化执行快照")
    outputs: dict[str, JsonData] = Field(default_factory=dict, description="按动作标识保存的输出数据")
    errors: dict[str, str] = Field(default_factory=dict, description="按动作标识保存的执行错误")
    runtime: WorkflowRuntimeState = Field(default_factory=WorkflowRuntimeState, description="工作流运行进度摘要")


class ActionPosition(BaseModel):
    """
    工作流画布中的动作坐标。
    """

    x: float = Field(default=0, description="工作流画布横坐标")
    y: float = Field(default=0, description="工作流画布纵坐标")


class ActionRetry(BaseModel):
    """
    动作失败后的重试策略。
    """

    max_attempts: int = Field(default=1, ge=1, description="最大执行次数，包含首次执行")
    interval: float = Field(default=0, ge=0, description="首次重试的等待时间，单位为秒")
    backoff: float = Field(default=1, ge=1, description="每次重试的等待时间增长倍数")


class Workflow(BaseModel):
    """
    工作流信息。
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
    execution_config: Optional[WorkflowExecutionConfig] = Field(
        default_factory=WorkflowExecutionConfig,
        description="工作流执行配置",
    )
    execution_state: Optional[WorkflowExecutionState] = Field(
        default_factory=WorkflowExecutionState,
        description="工作流结构化执行状态",
    )
    add_time: Optional[str] = Field(default=None, description="创建时间")
    last_time: Optional[str] = Field(default=None, description="最后执行时间")

    model_config = ConfigDict(from_attributes=True)


class ActionParams(BaseModel):
    """
    动作基础参数。
    """
    loop: Optional[bool] = Field(default=False, description="是否需要循环")
    loop_interval: Optional[int] = Field(default=0, description="循环间隔 (秒)")


class Action(BaseModel):
    """
    动作信息。
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
    动作执行情况。
    """
    action: Optional[str] = Field(default=None, description="当前动作（名称）")
    result: Optional[bool] = Field(default=None, description="执行结果")
    message: Optional[str] = Field(default=None, description="执行消息")


class ActionContext(BaseModel):
    """
    动作基础上下文，各动作通用数据。
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
    工作流流程。
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
    工作流分享信息。
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
    """
    单个插件声明的工作流动作组。
    """

    plugin_id: str = Field(description="插件标识")
    plugin_name: str = Field(description="插件显示名称")
    actions: list[dict[str, JsonData]] = Field(default_factory=list, description="可用动作列表")


class ActionContractField(BaseModel):
    """
    动作契约中的单个输入或输出字段。
    """

    name: str = Field(description="显示名称")
    label: str = Field(description="显示标签")
    kind: str = Field(default="scalar", description="输入或输出的数据形态，默认 scalar 表示单值")
    merge: Optional[str] = Field(default=None, description="多个上游输入的合并策略")
    identity: Optional[str] = Field(default=None, description="合并集合时用于识别同一条目的字段")


class ActionContract(BaseModel):
    """
    工作流动作对编辑器公开的输入输出契约。
    """

    inputs: list[ActionContractField] = Field(default_factory=list, description="动作输入声明")
    outputs: list[ActionContractField] = Field(default_factory=list, description="动作输出声明")
    condition_fields: list[ActionContractField] = Field(default_factory=list, description="可用于流转条件的字段定义")
    concurrency_key: Optional[str] = Field(default=None, description="并发互斥键")


class WorkflowActionDefinition(BaseModel):
    """
    可用于工作流编辑器的动作定义。
    """

    type: str = Field(description="类型标识")
    name: str = Field(description="显示名称")
    description: Optional[str] = Field(default=None, description="描述")
    contract: ActionContract = Field(default_factory=ActionContract, description="动作输入输出及并发约束")
    data: dict[str, JsonData] = Field(default_factory=dict, description="返回的结构化数据")


class NameValueOption(BaseModel):
    """
    前端选项的显示文本和值。
    """

    title: str = Field(description="前端显示的选项文本")
    value: str = Field(description="选中后提交的选项值")
