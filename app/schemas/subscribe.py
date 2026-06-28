from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field, ConfigDict, model_validator

from app.schemas.types import MediaType


def compute_subscribe_completed_episode(subscribe: Any) -> Optional[int]:
    """
    计算订阅"已完成"集数派生值，仅用于响应填充，不入库。

    普通电视剧按 ``total_episode - lack_episode`` 计算；洗版电视剧按订阅目标范围内
    priority==100 的分集数量，加上起始集前的逻辑完成集数计算。
    """
    total_episode = getattr(subscribe, "total_episode", None) or 0
    if getattr(subscribe, "type", None) != MediaType.TV.value or not total_episode:
        return None

    start_episode = getattr(subscribe, "start_episode", None) or 1
    if not getattr(subscribe, "best_version", None):
        lack = getattr(subscribe, "lack_episode", None) or 0
        return max(total_episode - lack, 0)

    episode_priority = getattr(subscribe, "episode_priority", None) or {}
    if not episode_priority and getattr(subscribe, "current_priority", None) is not None:
        # 兼容只有整体优先级的洗版快照，响应派生值需与链路侧按集口径保持一致。
        episode_priority = {
            str(episode): int(getattr(subscribe, "current_priority"))
            for episode in range(start_episode, total_episode + 1)
        }
    priority_completed = sum(
        1
        for ep_key, priority in episode_priority.items()
        if str(ep_key).isdigit()
        and start_episode <= int(ep_key) <= total_episode
        and priority == 100
    )
    return min(max(start_episode - 1, 0), total_episode) + priority_completed


class Subscribe(BaseModel):
    id: Optional[int] = None
    # 订阅名称
    name: Optional[str] = None
    # 订阅年份
    year: Optional[str] = None
    # 订阅类型 电影/电视剧
    type: Optional[str] = None
    # 搜索关键字
    keyword: Optional[str] = None
    tmdbid: Optional[int] = None
    doubanid: Optional[str] = None
    bangumiid: Optional[int] = None
    mediaid: Optional[str] = None
    # 季号
    season: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分
    vote: Optional[float] = 0.0
    # 描述
    description: Optional[str] = None
    # 过滤规则
    filter: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 质量
    quality: Optional[str] = None
    # 分辨率
    resolution: Optional[str] = None
    # 特效
    effect: Optional[str] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 开始集数
    start_episode: Optional[int] = 0
    # 缺失集数
    lack_episode: Optional[int] = 0
    # 已完成集数
    completed_episode: Optional[int] = None
    # 附加信息
    note: Optional[Any] = None
    # 状态：N-新建， R-订阅中
    state: Optional[str] = None
    # 最后更新时间
    last_update: Optional[str] = None
    # 订阅用户
    username: Optional[str] = None
    # 订阅站点
    sites: Optional[List[int]] = Field(default_factory=list)
    # 下载器
    downloader: Optional[str] = None
    # 是否洗版
    best_version: Optional[int] = None
    # 是否只洗全集整包
    best_version_full: Optional[int] = None
    # 当前优先级
    current_priority: Optional[int] = None
    # 洗版时已下载剧集的优先级状态
    episode_priority: Optional[Dict[str, int]] = None
    # 保存路径
    save_path: Optional[str] = None
    # 是否使用 imdbid 搜索
    search_imdbid: Optional[int] = 0
    # 时间
    date: Optional[str] = None
    # 自定义识别词
    custom_words: Optional[str] = None
    # 自定义媒体类别
    media_category: Optional[str] = None
    # 过滤规则组
    filter_groups: Optional[List[str]] = Field(default_factory=list)
    # 剧集组
    episode_group: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _fill_completed_episode(self) -> "Subscribe":
        """
        填充 ``completed_episode`` 派生字段。电视剧订阅按 best_version 分支计算，
        电影或缺少 total_episode 时保持 None。
        """
        if self.completed_episode is not None:
            # 调用方显式提供过的值不覆盖
            return self
        self.completed_episode = compute_subscribe_completed_episode(self)
        return self


class SubscribeShare(BaseModel):
    # 分享ID
    id: Optional[int] = None
    # 订阅ID
    subscribe_id: Optional[int] = None
    # 分享标题
    share_title: Optional[str] = None
    # 分享说明
    share_comment: Optional[str] = None
    # 分享人
    share_user: Optional[str] = None
    # 分享人唯一ID
    share_uid: Optional[str] = None
    # 订阅名称
    name: Optional[str] = None
    # 订阅年份
    year: Optional[str] = None
    # 订阅类型 电影/电视剧
    type: Optional[str] = None
    # 搜索关键字
    keyword: Optional[str] = None
    tmdbid: Optional[int] = None
    doubanid: Optional[str] = None
    bangumiid: Optional[int] = None
    # 季号
    season: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分
    vote: Optional[float] = 0.0
    # 描述
    description: Optional[str] = None
    # 包含
    include: Optional[str] = None
    # 排除
    exclude: Optional[str] = None
    # 质量
    quality: Optional[str] = None
    # 分辨率
    resolution: Optional[str] = None
    # 特效
    effect: Optional[str] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 时间
    date: Optional[str] = None
    # 自定义识别词
    custom_words: Optional[str] = None
    # 自定义媒体类别
    media_category: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None
    # 复用人次
    count: Optional[int] = 0


class SubscribeShareStatistics(BaseModel):
    # 分享人
    share_user: Optional[str] = None
    # 分享数量
    share_count: Optional[int] = 0
    # 总复用人次
    total_reuse_count: Optional[int] = 0


class SubscribeDownloadFileInfo(BaseModel):
    # 种子名称
    torrent_title: Optional[str] = None
    # 站点名称
    site_name: Optional[str] = None
    # 下载器
    downloader: Optional[str] = None
    # hash
    hash: Optional[str] = None
    # 文件路径
    file_path: Optional[str] = None


class SubscribeLibraryFileInfo(BaseModel):
    # 存储
    storage: Optional[str] = "local"
    # 文件路径
    file_path: Optional[str] = None


class SubscribeEpisodeInfo(BaseModel):
    # 标题
    title: Optional[str] = None
    # 描述
    description: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 下载文件信息
    download: Optional[List[SubscribeDownloadFileInfo]] = Field(default_factory=list)
    # 媒体库文件信息
    library: Optional[List[SubscribeLibraryFileInfo]] = Field(default_factory=list)


class SubscrbieInfo(BaseModel):
    # 订阅信息
    subscribe: Optional[Subscribe] = None
    # 集信息 {集号: {download: 文件路径，library: 文件路径, backdrop: url, title: 标题, description: 描述}}
    episodes: Optional[Dict[int, SubscribeEpisodeInfo]] = Field(default_factory=dict)
