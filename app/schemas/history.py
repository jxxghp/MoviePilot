
from pydantic import BaseModel


class DownloadHistory(BaseModel):
    # ID
    id: int
    # 保存路程
    path: str | None = None
    # 类型：电影、电视剧
    type: str | None = None
    # 标题
    title: str | None = None
    # 年份
    year: str | None = None
    # TMDBID
    tmdbid: int | None = None
    # IMDBID
    imdbid: str | None = None
    # TVDBID
    tvdbid: int | None = None
    # 豆瓣ID
    doubanid: str | None = None
    # 季Sxx
    seasons: str | None = None
    # 集Exx
    episodes: str | None = None
    # 海报
    image: str | None = None
    # 下载器Hash
    download_hash: str | None = None
    # 种子名称
    torrent_name: str | None = None
    # 种子描述
    torrent_description: str | None = None
    # 站点
    torrent_site: str | None = None
    # 下载用户
    userid: str | None = None
    # 下载用户名
    username: str | None = None
    # 下载渠道
    channel: str | None = None
    # 创建时间
    date: str | None = None
    # 备注
    note: str | None = None

    class Config:
        orm_mode = True


class TransferHistory(BaseModel):
    # ID
    id: int
    # 源目录
    src: str | None = None
    # 目的目录
    dest: str | None = None
    # 转移模式
    mode: str | None = None
    # 类型：电影、电视剧
    type: str | None = None
    # 二级分类
    category: str | None = None
    # 标题
    title: str | None = None
    # 年份
    year: str | None = None
    # TMDBID
    tmdbid: int | None = None
    # IMDBID
    imdbid: str | None = None
    # TVDBID
    tvdbid: int | None = None
    # 豆瓣ID
    doubanid: str | None = None
    # 季Sxx
    seasons: str | None = None
    # 集Exx
    episodes: str | None = None
    # 海报
    image: str | None = None
    # 下载器Hash
    download_hash: str | None = None
    # 状态 1-成功，0-失败
    status: bool = True
    # 失败原因
    errmsg: str | None = None
    # 日期
    date: str | None = None

    class Config:
        orm_mode = True
