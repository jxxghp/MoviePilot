"""
仪表盘中的媒体统计、资源用量和任务进度数据结构。
"""

from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.runtime.localization import LocaleHelper
from app.schemas.common import JsonData


class Statistic(BaseModel):
    """
    媒体库数量统计。
    """

    movie_count: Optional[int] = Field(default=0, description="电影数量")
    tv_count: Optional[int] = Field(default=0, description="电视剧数量")
    episode_count: Optional[int] = Field(default=0, description="集数量")
    music_count: Optional[int] = Field(default=0, description="音乐数量")
    user_count: Optional[int] = Field(default=0, description="用户数量")
    movie_count_month: Optional[int] = Field(default=0, description="本月新增电影数量")
    tv_count_month: Optional[int] = Field(default=0, description="本月新增电视剧数量")
    episode_count_month: Optional[int] = Field(default=0, description="本月新增剧集数量")
    music_count_month: Optional[int] = Field(default=0, description="本月新增音乐数量")


class Storage(BaseModel):
    """
    仪表板存储空间统计。
    """

    total_storage: Optional[float] = Field(default=0.0, description="存储总容量，单位为字节")
    used_storage: Optional[float] = Field(default=0.0, description="已使用存储容量，单位为字节")


class ProcessInfo(BaseModel):
    """
    仪表板进程运行信息。
    """

    pid: Optional[int] = Field(default=0, description="进程ID")
    name: Optional[str] = Field(default=None, description="进程名称")
    status: Optional[str] = Field(default=None, description="进程状态")
    cpu: Optional[float] = Field(default=0.0, description="进程占用CPU")
    memory: Optional[float] = Field(default=0.0, description="进程常驻内存，单位为 MiB（1024 的平方字节）")
    create_time: Optional[float] = Field(default=0.0, description="进程创建的 Unix 时间戳，单位为秒；未采集时为 0")
    run_time: Optional[float] = Field(default=0.0, description="运行时长的秒数部分；当前采集使用 timedelta.seconds，不包含完整天数")


class DownloaderInfo(BaseModel):
    """
    仪表板下载器汇总信息。
    """

    download_speed: Optional[float] = Field(default=0.0, description="当前总下载速度，单位为字节每秒")
    upload_speed: Optional[float] = Field(default=0.0, description="当前总上传速度，单位为字节每秒")
    download_size: Optional[float] = Field(default=0.0, description="下载器返回的累计下载量，单位为字节，统计周期取决于下载器")
    upload_size: Optional[float] = Field(default=0.0, description="下载器返回的累计上传量，单位为字节，统计周期取决于下载器")
    free_space: Optional[float] = Field(default=0.0, description="下载器所在存储的可用空间，单位为字节")


class DashboardMemoryInfo(BaseModel):
    """
    仪表板应用进程与系统内存统计。
    """

    total: int = Field(default=0, description="总内存字节数")
    used: int = Field(default=0, description="当前 MoviePilot 进程使用内存字节数")
    cached: int = Field(default=0, description="缓存与缓冲区占用字节数")
    available: int = Field(default=0, description="可用内存字节数")
    usage: float = Field(default=0.0, description="当前 MoviePilot 进程使用内存占总内存百分比")


class ScheduleProgress(BaseModel):
    """
    后台服务执行进度信息。
    """
    id: Optional[str] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="名称")
    name_i18n: Optional[str] = Field(default=None, description="多语言名称")
    provider: Optional[str] = Field(default=None, description="提供者")
    provider_i18n: Optional[str] = Field(default=None, description="多语言提供者")
    execution_group: str = Field(default="default", description="调度执行组：agent为自主Agent独立线程池，default为普通后台任务；交互轮次使用另外保留的容量")
    enable: Optional[bool] = Field(default=False, description="是否正在执行")
    value: Optional[float] = Field(default=0.0, description="当前完成百分比")
    text: Optional[str] = Field(default=None, description="当前进度文本")
    text_i18n: Optional[str] = Field(default=None, description="多语言进度文本")
    status: Optional[str] = Field(default=None, description="执行状态：waiting 等待、running 运行中、success 成功、failed 失败")
    success: Optional[bool] = Field(default=None, description="最近一次执行是否成功")
    started_at: Optional[str] = Field(default=None, description="最近一次开始时间")
    finished_at: Optional[str] = Field(default=None, description="最近一次结束时间")
    error: Optional[str] = Field(default=None, description="最近一次错误信息")
    error_i18n: Optional[str] = Field(default=None, description="多语言错误信息")
    data: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="扩展数据")

    @model_validator(mode="after")
    def fill_i18n_fields(self) -> "ScheduleProgress":
        """
        自动补充后台服务进度的多语言展示字段。
        """
        locale = LocaleHelper.get_current_locale()
        if self.name and self.name_i18n is None:
            self.name_i18n = LocaleHelper.translate_text(self.name, locale=locale)
        if self.provider and self.provider_i18n is None:
            self.provider_i18n = LocaleHelper.translate_text(self.provider, locale=locale)
        if self.text and self.text_i18n is None:
            self.text_i18n = LocaleHelper.translate_text(self.text, locale=locale)
        if self.error and self.error_i18n is None:
            self.error_i18n = LocaleHelper.translate_text(self.error, locale=locale)
        return self


class ScheduleInfo(BaseModel):
    """
    仪表板后台服务信息。
    """
    id: Optional[str] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="名称")
    name_i18n: Optional[str] = Field(default=None, description="多语言名称")
    provider: Optional[str] = Field(default=None, description="提供者")
    provider_i18n: Optional[str] = Field(default=None, description="多语言提供者")
    execution_group: str = Field(default="default", description="调度执行组")
    status: Optional[str] = Field(default=None, description="调度器提供的服务运行状态展示文本，与最近一次执行结果分开")
    status_i18n: Optional[str] = Field(default=None, description="多语言状态")
    next_run: Optional[str] = Field(default=None, description="下次执行时间")
    next_run_i18n: Optional[str] = Field(default=None, description="多语言下次执行时间")
    progress: Optional[float] = Field(default=0.0, description="当前完成百分比")
    progress_text: Optional[str] = Field(default=None, description="进度文本")
    progress_text_i18n: Optional[str] = Field(default=None, description="多语言进度文本")
    progress_enable: Optional[bool] = Field(default=False, description="是否正在更新进度")
    progress_detail: Optional[ScheduleProgress] = Field(default=None, description="进度详情")

    @model_validator(mode="after")
    def fill_i18n_fields(self) -> "ScheduleInfo":
        """
        自动补充后台服务列表的多语言展示字段。
        """
        locale = LocaleHelper.get_current_locale()
        if self.name and self.name_i18n is None:
            self.name_i18n = LocaleHelper.translate_text(self.name, locale=locale)
        if self.provider and self.provider_i18n is None:
            self.provider_i18n = LocaleHelper.translate_text(self.provider, locale=locale)
        if self.status and self.status_i18n is None:
            self.status_i18n = LocaleHelper.translate_text(self.status, locale=locale)
        if self.next_run and self.next_run_i18n is None:
            self.next_run_i18n = LocaleHelper.translate_text(self.next_run, locale=locale)
        if self.progress_text and self.progress_text_i18n is None:
            self.progress_text_i18n = LocaleHelper.translate_text(self.progress_text, locale=locale)
        return self


class DashboardSystemInfo(BaseModel):
    """
    仪表板系统摘要信息。
    """

    hostname: str = Field(description="主机名称")
    operating_system: str = Field(description="操作系统名称")
    runtime: int = Field(description="MoviePilot 运行时间，单位秒")
    version: str = Field(description="MoviePilot 后端版本")
