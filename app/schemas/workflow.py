from typing import Optional, List

from pydantic import BaseModel, Field

from app.schemas.context import Context, MediaInfo
from app.schemas.download import DownloadTask
from app.schemas.file import FileItem
from app.schemas.site import Site
from app.schemas.subscribe import Subscribe


class Workflow(BaseModel):
    """
    工作流信息
    """
    id: Optional[int] = Field(None, description="工作流ID")
    name: Optional[str] = Field(None, description="工作流名称")
    description: Optional[str] = Field(None, description="工作流描述")
    timer: Optional[str] = Field(None, description="定时器")
    state: Optional[str] = Field(None, description="状态")
    current_action: Optional[str] = Field(None, description="已执行动作")
    result: Optional[str] = Field(None, description="任务执行结果")
    run_count: Optional[int] = Field(0, description="已执行次数")
    actions: Optional[list] = Field([], description="任务列表")
    flows: Optional[list] = Field([], description="任务流")
    add_time: Optional[str] = Field(None, description="创建时间")
    last_time: Optional[str] = Field(None, description="最后执行时间")

    class Config:
        orm_mode = True


class ActionParams(BaseModel):
    """
    动作基础参数
    """
    loop: Optional[bool] = Field(False, description="是否需要循环")
    loop_interval: Optional[int] = Field(0, description="循环间隔 (秒)")


class Action(BaseModel):
    """
    动作信息
    """
    id: Optional[str] = Field(None, description="动作ID")
    type: Optional[str] = Field(None, description="动作类型 (类名)")
    name: Optional[str] = Field(None, description="动作名称")
    description: Optional[str] = Field(None, description="动作描述")
    position: Optional[dict] = Field({}, description="位置")
    data: Optional[dict] = Field({}, description="参数")


class ActionExecution(BaseModel):
    """
    动作执行情况
    """
    action: Optional[str] = Field(None, description="当前动作（名称）")
    result: Optional[bool] = Field(None, description="执行结果")
    message: Optional[str] = Field(None, description="执行消息")


class ActionContext(BaseModel):
    """
    动作基础上下文，各动作通用数据
    """
    content: Optional[str] = Field(None, description="文本类内容")
    torrents: Optional[List[Context]] = Field([], description="资源列表")
    medias: Optional[List[MediaInfo]] = Field([], description="媒体列表")
    fileitems: Optional[List[FileItem]] = Field([], description="文件列表")
    downloads: Optional[List[DownloadTask]] = Field([], description="下载任务列表")
    sites: Optional[List[Site]] = Field([], description="站点列表")
    subscribes: Optional[List[Subscribe]] = Field([], description="订阅列表")
    execute_history: Optional[List[ActionExecution]] = Field([], description="执行历史")
    progress: Optional[int] = Field(0, description="执行进度（%）")


class ActionFlow(BaseModel):
    """
    工作流流程
    """
    id: Optional[str] = Field(None, description="流程ID")
    source: Optional[str] = Field(None, description="源动作")
    target: Optional[str] = Field(None, description="目标动作")
    animated: Optional[bool] = Field(True, description="是否动画流程")
