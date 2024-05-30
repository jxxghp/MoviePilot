
from pydantic import BaseModel


class Statistic(BaseModel):
    # 电影
    movie_count: int | None = 0
    # 电视剧数量
    tv_count: int | None = 0
    # 集数量
    episode_count: int | None = 0
    # 用户数量
    user_count: int | None = 0


class Storage(BaseModel):
    # 总存储空间
    total_storage: float | None = 0
    # 已使用空间
    used_storage: float | None = 0


class ProcessInfo(BaseModel):
    # 进程ID
    pid: int | None = 0
    # 进程名称
    name: str | None = None
    # 进程状态
    status: str | None = None
    # 进程占用CPU
    cpu: float | None = 0.0
    # 进程占用内存 MB
    memory: float | None = 0.0
    # 进程创建时间
    create_time: float | None = 0.0
    # 进程运行时间 秒
    run_time: float | None = 0.0


class DownloaderInfo(BaseModel):
    # 下载速度
    download_speed: float | None = 0.0
    # 上传速度
    upload_speed: float | None = 0.0
    # 下载量
    download_size: float | None = 0.0
    # 上传量
    upload_size: float | None = 0.0
    # 剩余空间
    free_space: float | None = 0.0


class ScheduleInfo(BaseModel):
    # ID
    id: str | None = None
    # 名称
    name: str | None = None
    # 提供者
    provider: str | None = None
    # 状态
    status: str | None = None
    # 下次执行时间
    next_run: str | None = None
