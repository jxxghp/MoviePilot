from typing import Optional, Any

from pydantic import BaseModel, ConfigDict, Field


class DownloadHistory(BaseModel):
    # ID
    id: int
    # 保存路程
    path: Optional[str] = None
    # 类型：电影、电视剧
    type: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # TMDBID
    tmdbid: Optional[int] = None
    # IMDBID
    imdbid: Optional[str] = None
    # TVDBID
    tvdbid: Optional[int] = None
    # 豆瓣ID
    doubanid: Optional[str] = None
    # 季Sxx
    seasons: Optional[str] = None
    # 集Exx
    episodes: Optional[str] = None
    # 海报
    image: Optional[str] = None
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
    note: Optional[Any] = None
    # 自定义媒体类别
    media_category: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TransferHistory(BaseModel):
    """
    文件整理历史记录
    """

    # ID
    id: int
    # 源目录
    src: Optional[str] = None
    # 目的目录
    dest: Optional[str] = None
    # 转移模式
    mode: Optional[str] = None
    # 类型：电影、电视剧
    type: Optional[str] = None
    # 二级分类
    category: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # TMDBID
    tmdbid: Optional[int] = None
    # IMDBID
    imdbid: Optional[str] = None
    # TVDBID
    tvdbid: Optional[int] = None
    # 豆瓣ID
    doubanid: Optional[str] = None
    # 媒体数据源
    media_source: Optional[str] = None
    # 数据源原生ID
    media_id: Optional[str] = None
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

    model_config = ConfigDict(from_attributes=True)


class BatchTransferHistoryRedoRequest(BaseModel):
    history_ids: list[int] = Field(default_factory=list)
