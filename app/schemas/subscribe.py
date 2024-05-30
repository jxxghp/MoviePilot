
from pydantic import BaseModel


class Subscribe(BaseModel):
    id: int | None = None
    # 订阅名称
    name: str | None = None
    # 订阅年份
    year: str | None = None
    # 订阅类型 电影/电视剧
    type: str | None = None
    # 搜索关键字
    keyword: str | None = None
    tmdbid: int | None = None
    doubanid: str | None = None
    bangumiid: int | None = None
    # 季号
    season: int | None = None
    # 海报
    poster: str | None = None
    # 背景图
    backdrop: str | None = None
    # 评分
    vote: int | None = 0
    # 描述
    description: str | None = None
    # 过滤规则
    filter: str | None = None
    # 包含
    include: str | None = None
    # 排除
    exclude: str | None = None
    # 质量
    quality: str | None = None
    # 分辨率
    resolution: str | None = None
    # 特效
    effect: str | None = None
    # 总集数
    total_episode: int | None = 0
    # 开始集数
    start_episode: int | None = 0
    # 缺失集数
    lack_episode: int | None = 0
    # 附加信息
    note: str | None = None
    # 状态：N-新建， R-订阅中
    state: str | None = None
    # 最后更新时间
    last_update: str | None = None
    # 订阅用户
    username: str | None = None
    # 订阅站点
    sites: list[int] | None = []
    # 是否洗版
    best_version: int | None = 0
    # 当前优先级
    current_priority: int | None = None
    # 保存路径
    save_path: str | None = None
    # 是否使用 imdbid 搜索
    search_imdbid: int | None = 0
    # 时间
    date: str | None = None

    class Config:
        orm_mode = True
