from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator as _field_validator

from app.runtime.errors import public_error_message as _public_error_message
from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource


class DownloadHistory(OptionalMediaIdentityMixin, BaseModel):
    """
    下载历史记录
    """

    # ID
    id: int
    # 保存路程
    path: Optional[str] = None
    # 类型：电影、电视剧、音乐
    type: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 媒体数据源
    media_source: Optional[MediaSource] = None
    # 数据源原生ID
    media_id: Optional[str] = None
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Optional[str] = None
    # 季Sxx
    seasons: Optional[str] = None
    # 集Exx
    episodes: Optional[str] = None
    # 背景图
    image: Optional[str] = None
    # 海报
    poster: Optional[str] = None
    # 下载器Hash
    download_hash: Optional[str] = None
    # 种子名称
    torrent_name: Optional[str] = None
    # 种子描述
    torrent_description: Optional[str] = None
    # 站点
    torrent_site: Optional[str] = None
    # 下载用户
    userid: Optional[str] = None
    # 下载用户名
    username: Optional[str] = None
    # 下载渠道
    channel: Optional[str] = None
    # 创建时间
    date: Optional[str] = None
    # 备注
    note: Optional[JsonData] = None
    # 实际媒体类别稳定标识
    media_category_id: Optional[str] = None
    # 实际媒体类别兼容路径快照
    media_category: Optional[str] = None
    # 命中的分类规则标识
    classification_rule_id: Optional[str] = None
    # 执行时分类策略版本
    classification_policy_revision: Optional[int] = None
    # 最终分类来源
    classification_source: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TransferHistory(OptionalMediaIdentityMixin, BaseModel):
    """
    文件整理历史记录
    """

    # ID
    id: int
    # durable 整理任务标识仅供宿主入口选择重试协议，不属于公开历史响应
    transfer_task_id: Optional[str] = Field(default=None, exclude=True)
    # 源存储类型
    src_storage: Optional[str] = None
    # 目标存储类型
    dest_storage: Optional[str] = None
    # 源文件项（含文件大小等运行时字段，与 to_dict 直出保持一致）
    src_fileitem: Optional[JsonData] = None
    # 目标文件项
    dest_fileitem: Optional[JsonData] = None
    # 源目录
    src: Optional[str] = None
    # 目的目录
    dest: Optional[str] = None
    # 转移模式
    mode: Optional[str] = None
    # 类型：电影、电视剧、音乐
    type: Optional[str] = None
    # 实际媒体类别稳定标识
    media_category_id: Optional[str] = None
    # 实际媒体类别兼容路径快照
    category: Optional[str] = None
    # 命中的分类规则标识
    classification_rule_id: Optional[str] = None
    # 执行时分类策略版本
    classification_policy_revision: Optional[int] = None
    # 最终分类来源
    classification_source: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 媒体数据源
    media_source: Optional[MediaSource] = None
    # 数据源原生ID
    media_id: Optional[str] = None
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Optional[str] = None
    # 专辑预期总曲目数
    total_tracks: Optional[int] = None
    # 实际音频格式
    audio_format: Optional[str] = None
    # 是否无损音频
    audio_lossless: Optional[bool] = None
    # 实际位深（bit）
    bit_depth: Optional[int] = None
    # 实际采样率（Hz）
    sample_rate: Optional[int] = None
    # 实际码率（bps）
    bitrate: Optional[int] = None
    # 季Sxx
    seasons: Optional[str] = None
    # 集Exx
    episodes: Optional[str] = None
    # 海报
    image: Optional[str] = None
    # 下载器Hash
    download_hash: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None
    # 状态 1-成功，0-失败
    status: bool = True
    # 失败原因
    errmsg: Optional[str] = None
    # 日期
    date: Optional[str] = None
    # 文件清单
    files: Optional[JsonData] = None

    model_config = ConfigDict(from_attributes=True)

    @_field_validator("errmsg", mode="before")
    @classmethod
    def _sanitize_error_message(cls, value: object) -> Optional[str]:
        """历史接口只返回可理解的整理失败原因，数据库原文仍用于诊断。"""
        if value is None or not str(value).strip():
            return None
        return _public_error_message(value, context="transfer")


class BatchTransferHistoryRedoRequest(BaseModel):
    """批量重新整理历史请求。"""

    history_ids: list[int] = Field(default_factory=list)


class TransferHistoryPage(BaseModel):
    """整理历史分页数据。"""

    list: List[TransferHistory] = Field(default_factory=list, description="整理历史列表")
    total: int = Field(default=0, description="记录总数")


class TransferHistoryDeleteStep(BaseModel):  # type: ignore[misc]
    """整理历史删除流程中单个文件目标的执行结果。"""

    # not_requested 未请求；deleted 已删除；already_missing 原目标已不存在；failed 删除或校验失败
    status: Literal["not_requested", "deleted", "already_missing", "failed"]
    # 仅用于无法继续处理时给出可读原因，正常状态为空
    message: str = ""


class TransferHistoryDeleteResult(BaseModel):  # type: ignore[misc]
    """整理历史删除及源/目标文件清理的分项结果。"""

    # 源文件步骤
    source: TransferHistoryDeleteStep
    # 目标文件步骤
    destination: TransferHistoryDeleteStep
    # 历史记录步骤：deleted 已删除、retained 保留待重试、not_found 不存在
    history: Literal["deleted", "retained", "not_found"]
    # 面向请求方的概要消息；详细状态由三个步骤字段表达
    message: str = ""

    @property
    def success(self) -> bool:
        """兼容应用层命令调用方，以历史记录是否删除作为整体成功条件。"""
        return self.history == "deleted"

    model_config = ConfigDict(from_attributes=True)
