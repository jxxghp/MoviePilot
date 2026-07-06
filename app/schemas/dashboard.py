from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.helper.locale import LocaleHelper


class Statistic(BaseModel):
    """媒体库数量统计。"""

    # 电影
    movie_count: Optional[int] = 0
    # 电视剧数量
    tv_count: Optional[int] = 0
    # 集数量
    episode_count: Optional[int] = 0
    # 用户数量
    user_count: Optional[int] = 0
    # 本月新增电影数量
    movie_count_month: Optional[int] = 0
    # 本月新增电视剧数量
    tv_count_month: Optional[int] = 0
    # 本月新增剧集数量
    episode_count_month: Optional[int] = 0


class Storage(BaseModel):
    """仪表板存储空间统计。"""

    # 总存储空间
    total_storage: Optional[float] = 0.0
    # 已使用空间
    used_storage: Optional[float] = 0.0


class ProcessInfo(BaseModel):
    """仪表板进程运行信息。"""

    # 进程ID
    pid: Optional[int] = 0
    # 进程名称
    name: Optional[str] = None
    # 进程状态
    status: Optional[str] = None
    # 进程占用CPU
    cpu: Optional[float] = 0.0
    # 进程占用内存 MB
    memory: Optional[float] = 0.0
    # 进程创建时间
    create_time: Optional[float] = 0.0
    # 进程运行时间 秒
    run_time: Optional[float] = 0.0


class DownloaderInfo(BaseModel):
    """仪表板下载器汇总信息。"""

    # 下载速度
    download_speed: Optional[float] = 0.0
    # 上传速度
    upload_speed: Optional[float] = 0.0
    # 下载量
    download_size: Optional[float] = 0.0
    # 上传量
    upload_size: Optional[float] = 0.0
    # 剩余空间
    free_space: Optional[float] = 0.0


class DashboardMemoryInfo(BaseModel):
    """仪表板应用进程与系统内存统计。"""

    # 总内存字节数
    total: int = 0
    # 当前 MoviePilot 进程使用内存字节数
    used: int = 0
    # 缓存与缓冲区占用字节数
    cached: int = 0
    # 可用内存字节数
    available: int = 0
    # 当前 MoviePilot 进程使用内存占总内存百分比
    usage: float = 0.0


class ScheduleProgress(BaseModel):
    """后台服务执行进度信息。"""

    # ID
    id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 多语言名称
    name_i18n: Optional[str] = None
    # 提供者
    provider: Optional[str] = None
    # 多语言提供者
    provider_i18n: Optional[str] = None
    # 是否正在执行
    enable: Optional[bool] = False
    # 当前完成百分比
    value: Optional[float] = 0.0
    # 当前进度文本
    text: Optional[str] = None
    # 多语言进度文本
    text_i18n: Optional[str] = None
    # 执行状态 waiting/running/success/failed
    status: Optional[str] = None
    # 最近一次执行是否成功
    success: Optional[bool] = None
    # 最近一次开始时间
    started_at: Optional[str] = None
    # 最近一次结束时间
    finished_at: Optional[str] = None
    # 最近一次错误信息
    error: Optional[str] = None
    # 多语言错误信息
    error_i18n: Optional[str] = None
    # 扩展数据
    data: Optional[dict] = Field(default_factory=dict)

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
    """仪表板后台服务信息。"""

    # ID
    id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 多语言名称
    name_i18n: Optional[str] = None
    # 提供者
    provider: Optional[str] = None
    # 多语言提供者
    provider_i18n: Optional[str] = None
    # 状态
    status: Optional[str] = None
    # 多语言状态
    status_i18n: Optional[str] = None
    # 下次执行时间
    next_run: Optional[str] = None
    # 多语言下次执行时间
    next_run_i18n: Optional[str] = None
    # 当前完成百分比
    progress: Optional[float] = 0.0
    # 进度文本
    progress_text: Optional[str] = None
    # 多语言进度文本
    progress_text_i18n: Optional[str] = None
    # 是否正在更新进度
    progress_enable: Optional[bool] = False
    # 进度详情
    progress_detail: Optional[ScheduleProgress] = None

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
    """仪表板系统摘要信息。"""

    # 主机名称
    hostname: str
    # 操作系统名称
    operating_system: str
    # MoviePilot 运行时间，单位秒
    runtime: int
    # MoviePilot 后端版本
    version: str
