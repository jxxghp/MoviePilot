from typing import Optional

from pydantic import BaseModel


class SysConfigBase(BaseModel):
    # 媒体统计
    mediaStatistic: Optional[bool] = True
    # 后台任务
    scheduler: Optional[bool] = False
    # 实时速率
    speed: Optional[bool] = False
    # 存储空间
    storage: Optional[bool] = True
    # 最近入库
    weeklyOverview: Optional[bool] = False
    # CPU
    cpu: Optional[bool] = False
    # 内存
    memory: Optional[bool] = False
    # 我的媒体库
    library: Optional[bool] = True
    # 继续观看
    playing: Optional[bool] = True
    # 最近添加
    latest: Optional[bool] = True


class SysConfigInDBBase(SysConfigBase):
    id: Optional[int] = None
    uid: Optional[int] = None


class SysConfig(SysConfigInDBBase):
    uid: int
