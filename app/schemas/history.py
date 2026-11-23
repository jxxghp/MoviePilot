"""
下载和整理历史的接口投影及批量操作结果。
"""

from pathlib import Path as _Path
from pathlib import PurePosixPath as _PurePosixPath
from typing import List, Literal, Optional
from typing import Self as _Self
from unicodedata import category as _category

from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator as _field_validator
from pydantic import model_validator as _model_validator

from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin, RequiredMediaIdentityMixin
from app.schemas.types import MediaSource


class TransferRegistrationAudit(BaseModel):
    """
    外部整理登记时由服务器固定的操作者和文件核验说明。
    """

    actor_id: int = Field(ge=1, description="登记管理员的完整用户主键，身份以此字段为准")
    actor_name: str = Field(
        min_length=1, max_length=128,
        description="管理员名称显示快照，最多 128 字符；超长时取前 127 字符加省略号，空名称显示 #用户ID",
    )
    registered_at: str = Field(max_length=40, description="服务器生成的 UTC ISO 登记时间")
    verification: Literal["matching_sizes", "native_hardlink", "destination_only"] = Field(
        description="两端大小匹配、本地硬链接或仅目标存在；均不代表内容校验和",
    )
    source_state: Literal["present", "absent"] = Field(description="核验时源文件存在或确认不存在")

    model_config = ConfigDict(extra="forbid", frozen=True)


class TransferHistoryRegisterRequest(RequiredMediaIdentityMixin, BaseModel):
    """
    管理员对已完成的外部整理提交路径和媒体声明，不允许伪造文件快照或审计。
    """

    src_storage: str = Field(min_length=1, max_length=64, description="源存储配置标识")
    dest_storage: str = Field(min_length=1, max_length=64, description="目标存储配置标识")
    src: str = Field(min_length=1, max_length=4096, description="整理前源文件的绝对路径")
    dest: str = Field(min_length=1, max_length=4096, description="整理后目标文件的绝对路径")
    mode: Literal["copy", "link", "move"] = Field(description="已完成的复制、本地硬链接或移动")
    type: Literal["电影", "电视剧"] = Field(description="管理员确认的电影或电视剧类型")
    title: str = Field(min_length=1, max_length=512, description="管理员确认的媒体标题")
    year: Optional[str] = Field(default=None, pattern=r"^\d{4}$", description="四位发行年份")
    media_source: MediaSource = Field(description="管理员确认的媒体数据源")
    media_id: str = Field(min_length=1, max_length=128, description="来源中的规范原生媒体标识")
    seasons: Optional[str] = Field(
        default=None, max_length=32, pattern=r"^S\d{2,4}(?:-S\d{2,4})?$", description="季号或季范围，例如 S01",
    )
    episodes: Optional[str] = Field(
        default=None, max_length=32, pattern=r"^E(?:0[1-9]|[1-9]\d{1,3})(?:-E(?:0[1-9]|[1-9]\d{1,3}))?$",
        description="集号或集范围，例如 E01 或 E01-E12",
    )
    download_hash: Optional[str] = Field(default=None, max_length=128, description="已知下载任务标识，仅作关联声明")

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @_field_validator("*", mode="before")
    @classmethod
    def _reject_control_characters(cls, value: object) -> object:
        """
        在去除首尾空白之前拒绝 Unicode 控制字符，避免异常地址被静默改写。
        """
        if isinstance(value, str) and any(_category(char) == "Cc" for char in value):
            raise ValueError("登记字段不能包含控制字符")
        return value

    @_model_validator(mode="after")
    def _normalize_addresses(self) -> _Self:
        """
        只规范地址拼写，不解析符号链接；拒绝相对路径、父跳转和同地址登记。
        """
        for field in ("src", "dest"):
            value = getattr(self, field)
            storage = getattr(self, f"{field}_storage")
            path = _Path(value) if storage == "local" else _PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("登记路径必须为不含父目录跳转的绝对路径")
            object.__setattr__(self, field, path.as_posix())
        if self.src_storage == self.dest_storage and self.src == self.dest:
            raise ValueError("源文件与目标文件不能是同一地址")
        if self.type == "电影" and (self.seasons or self.episodes):
            raise ValueError("电影登记不能包含季集信息")
        return self


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
    origin: Optional[Literal["external"]] = Field(default=None, description="external 为外部登记；旧行未记录来源")
    registration: Optional[TransferRegistrationAudit] = Field(default=None, description="外部登记审计与核验说明")
    transfer_task_id: Optional[str] = Field(
        default=None,
        exclude=True,
        description="durable 整理任务标识仅供宿主入口选择重试协议，不属于公开历史响应",
    )
    transfer_settlement_revision: Optional[int] = Field(
        default=None, exclude=True, description="宿主 AI 接管使用的结算版本，不属于公开历史响应",
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

    @_field_validator("errmsg", mode="before")
    @classmethod
    def _sanitize_error_message(cls, value: object) -> Optional[str]:
        """
        历史接口只返回可理解的整理失败原因，数据库原文仍用于诊断。
        """
        if value is None or not str(value).strip():
            return None
        from app.runtime.errors import public_error_message

        return public_error_message(value, context="transfer")


class TransferHistoryRegisterResult(BaseModel):
    """
    已提交的登记回执，重复请求返回原历史及 created=false。
    """

    history: TransferHistory = Field(description="已提交或先前登记的历史")
    created: bool = Field(description="此次请求是否新建历史记录")


class BatchTransferHistoryRedoRequest(BaseModel):
    """
    批量重新整理历史请求。
    """

    history_ids: list[int] = Field(default_factory=list, description="历史记录 ID 列表")


class TransferHistoryDiscardResult(BaseModel):
    """
    损坏整理任务清理结果，标识保留供后续操作的历史记录。
    """

    history_id: int = Field(description="保留的整理历史记录 ID")


class TransferHistoryReplanRequest(RequiredMediaIdentityMixin, BaseModel):
    """
    为零文件副作用的识别失败提交经核实的媒体身份和季集修正。
    """

    task_id: str = Field(min_length=1, description="接管上下文中的原始持久任务标识，必须与历史绑定一致")
    settlement_revision: int = Field(ge=1, description="接管上下文中的原始结算版本，拒绝分析期间已变化的任务")
    media_source: MediaSource = Field(description="经媒体查询核实的身份来源")
    media_id: str = Field(
        min_length=1,
        description="经媒体查询核实并与 media_source 原样配对的来源原生 ID，重新规划时直接按该身份识别",
    )
    media_type: Literal["movie", "tv"] = Field(description="经核实的电影或电视剧类型，须与该来源原生 ID 对应")
    season: Optional[int] = Field(default=None, ge=0, description="修正后的季号，0 为特别篇；为空时保留原值")
    episode: Optional[int] = Field(default=None, ge=1, description="修正后的单集集号；为空时保留原集数范围")

    model_config = ConfigDict(extra="forbid")


class TransferHistoryPage(BaseModel):
    """
    整理历史分页数据。
    """

    list: List[TransferHistory] = Field(default_factory=list, description="整理历史列表")
    total: int = Field(default=0, description="记录总数")


class TransferHistoryDeleteStep(BaseModel):
    """
    整理历史删除流程中单个文件目标的执行结果。
    """

    status: Literal["not_requested", "deleted", "already_missing", "failed"] = Field(
        description="not_requested 未请求；deleted 已删除；already_missing 原目标已不存在；failed 删除或校验失败",
    )
    message: str = Field(default="", description="仅用于无法继续处理时给出可读原因，正常状态为空")


class TransferHistoryDeleteResult(BaseModel):
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
