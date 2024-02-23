from typing import Optional

from pydantic import BaseModel


class Statistic(BaseModel):
    # 电影
    movie_count: Optional[int] = 0
    # 电视剧数量
    tv_count: Optional[int] = 0
    # 集数量
    episode_count: Optional[int] = 0
    # 用户数量
    user_count: Optional[int] = 0


class Storage(BaseModel):
    # 总存储空间
    total_storage: Optional[float] = 0
    # 已使用空间
    used_storage: Optional[float] = 0


class ProcessInfo(BaseModel):
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


class ScheduleInfo(BaseModel):
    # ID
    id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 提供者
    provider: Optional[str] = None
    # 状态
    status: Optional[str] = None
    # 下次执行时间
    next_run: Optional[str] = None
