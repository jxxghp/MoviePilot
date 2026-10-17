"""
下载和整理历史的接口投影及批量操作结果。
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator as _field_validator

from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource


class DownloadHistory(OptionalMediaIdentityMixin, BaseModel):
    """
    下载历史记录。
    """
    id: int = Field(description="记录标识")
    path: Optional[str] = Field(default=None, description="保存路程")
    type: Optional[str] = Field(default=None, description="类型：电影、电视剧、音乐")
    title: Optional[str] = Field(default=None, description="标题")
    year: Optional[str] = Field(default=None, description="年份")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    seasons: Optional[str] = Field(default=None, description="季Sxx")
    episodes: Optional[str] = Field(default=None, description="集Exx")
    image: Optional[str] = Field(default=None, description="背景图")
    poster: Optional[str] = Field(default=None, description="海报")
    download_hash: Optional[str] = Field(default=None, description="下载器Hash")
    torrent_name: Optional[str] = Field(default=None, description="种子名称")
    torrent_description: Optional[str] = Field(default=None, description="种子描述")
    torrent_site: Optional[str] = Field(default=None, description="站点")
    userid: Optional[str] = Field(default=None, description="下载用户")
    username: Optional[str] = Field(default=None, description="下载用户名")
    channel: Optional[str] = Field(default=None, description="下载渠道")
    date: Optional[str] = Field(default=None, description="创建时间")
    note: Optional[JsonData] = Field(default=None, description="备注")
    media_category_id: Optional[str] = Field(default=None, description="实际媒体类别稳定标识")
    media_category: Optional[str] = Field(default=None, description="实际媒体类别兼容路径快照")
    classification_rule_id: Optional[str] = Field(default=None, description="命中的分类规则标识")
    classification_policy_revision: Optional[int] = Field(default=None, description="执行时分类策略版本")
    classification_source: Optional[str] = Field(default=None, description="最终分类来源")
    episode_group: Optional[str] = Field(default=None, description="自定义剧集组")

    model_config = ConfigDict(from_attributes=True)


class TransferHistory(OptionalMediaIdentityMixin, BaseModel):
    """
    文件整理历史记录。
    """
    id: int = Field(description="记录标识")
    transfer_task_id: Optional[str] = Field(
        default=None,
        exclude=True,
        description="durable 整理任务标识仅供宿主入口选择重试协议，不属于公开历史响应",
    )
    src_storage: Optional[str] = Field(default=None, description="源存储类型")
    dest_storage: Optional[str] = Field(default=None, description="目标存储类型")
    src_fileitem: Optional[JsonData] = Field(default=None, description="源文件项（含文件大小等运行时字段，与 to_dict 直出保持一致）")
    dest_fileitem: Optional[JsonData] = Field(default=None, description="目标文件项")
    src: Optional[str] = Field(default=None, description="源目录")
    dest: Optional[str] = Field(default=None, description="目的目录")
    mode: Optional[str] = Field(default=None, description="转移模式")
    type: Optional[str] = Field(default=None, description="类型：电影、电视剧、音乐")
    media_category_id: Optional[str] = Field(default=None, description="实际媒体类别稳定标识")
    category: Optional[str] = Field(default=None, description="实际媒体类别兼容路径快照")
    classification_rule_id: Optional[str] = Field(default=None, description="命中的分类规则标识")
    classification_policy_revision: Optional[int] = Field(default=None, description="执行时分类策略版本")
    classification_source: Optional[str] = Field(default=None, description="最终分类来源")
    title: Optional[str] = Field(default=None, description="标题")
    year: Optional[str] = Field(default=None, description="年份")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    audio_format: Optional[str] = Field(default=None, description="实际音频格式")
    audio_lossless: Optional[bool] = Field(default=None, description="是否无损音频")
    bit_depth: Optional[int] = Field(default=None, description="实际位深（bit）")
    sample_rate: Optional[int] = Field(default=None, description="实际采样率（Hz）")
    bitrate: Optional[int] = Field(default=None, description="实际码率（bps）")
    seasons: Optional[str] = Field(default=None, description="季Sxx")
    episodes: Optional[str] = Field(default=None, description="集Exx")
    image: Optional[str] = Field(default=None, description="海报")
    download_hash: Optional[str] = Field(default=None, description="下载器Hash")
    episode_group: Optional[str] = Field(default=None, description="自定义剧集组")
    status: bool = Field(default=True, description="状态 1-成功，0-失败")
    errmsg: Optional[str] = Field(default=None, description="失败原因")
    failure_stage: Optional[str] = Field(default=None, description="从失败原因和失败计数派生的用户可操作状态")
    recovery_action: Optional[str] = Field(default=None, description="当前失败状态下可执行的恢复操作")
    retry_count: Optional[int] = Field(default=None, description="已执行的重试次数")
    retry_exhausted: bool = Field(default=False, description="是否已耗尽重试次数")
    auto_paused: bool = Field(default=False, description="是否因反复失败而自动暂停")
    cleanup_status: Optional[str] = Field(default=None, description="下载器清理独立于媒体入库结果")
    cleanup_error: Optional[str] = Field(default=None, description="下载器清理失败的原因")
    date: Optional[str] = Field(default=None, description="日期")
    files: Optional[JsonData] = Field(default=None, description="文件清单")

    model_config = ConfigDict(from_attributes=True)

    # Pydantic 的动态装饰器类型会被存量 mypy 配置解析为 Any，公开 schema 仍需保留该校验。
    @_field_validator("errmsg", mode="before")  # type: ignore[misc]
    @classmethod
    def _sanitize_error_message(cls, value: object) -> Optional[str]:
        """
        历史接口只返回可理解的整理失败原因，数据库原文仍用于诊断。
        """
        if value is None or not str(value).strip():
            return None
        from app.runtime.errors import public_error_message

        return public_error_message(value, context="transfer")


class BatchTransferHistoryRedoRequest(BaseModel):
    """
    批量重新整理历史请求。
    """

    history_ids: list[int] = Field(default_factory=list, description="历史记录 ID 列表")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class TransferHistoryDiscardResult(BaseModel):  # type: ignore[misc]
    """
    损坏整理任务清理结果，标识保留供后续操作的历史记录。
    """

    history_id: int = Field(description="保留的整理历史记录 ID")


class TransferHistoryPage(BaseModel):
    """
    整理历史分页数据。
    """

    list: List[TransferHistory] = Field(default_factory=list, description="整理历史列表")
    total: int = Field(default=0, description="记录总数")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class TransferHistoryDeleteStep(BaseModel):  # type: ignore[misc]
    """
    整理历史删除流程中单个文件目标的执行结果。
    """

    status: Literal["not_requested", "deleted", "already_missing", "failed"] = Field(
        description="not_requested 未请求；deleted 已删除；already_missing 原目标已不存在；failed 删除或校验失败",
    )
    message: str = Field(default="", description="仅用于无法继续处理时给出可读原因，正常状态为空")


# Pydantic 动态类型无法由当前检查配置完整推断，保留既有类型检查豁免。
class TransferHistoryDeleteResult(BaseModel):  # type: ignore[misc]
    """
    整理历史删除及源/目标文件清理的分项结果。
    """

    source: TransferHistoryDeleteStep = Field(description="源文件步骤")
    destination: TransferHistoryDeleteStep = Field(description="目标文件步骤")
    history: Literal["deleted", "retained", "not_found"] = Field(
        description="历史记录步骤：deleted 已删除、retained 保留待重试、not_found 不存在",
    )
    message: str = Field(default="", description="面向请求方的概要消息；详细状态由三个步骤字段表达")

    @property
    def success(self) -> bool:
        """
        兼容应用层命令调用方，以历史记录是否删除作为整体成功条件。
        """
        return self.history == "deleted"

    model_config = ConfigDict(from_attributes=True)
