"""
自定义弹弹play 兼容弹幕代理服务的请求与响应数据结构。
"""

from dataclasses import dataclass
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

DanmakuAuthMode = Literal["none", "dandanplay_v2", "header_token"]


@dataclass(frozen=True, slots=True)
class DanmakuHttpResponse:
    """
    上游弹幕 HTTP 传输端口返回的统一响应快照。

    :param status_code: 上游 HTTP 状态码
    :param content: 原始响应字节内容
    :param content_type: 响应头中的 Content-Type 文本
    """

    status_code: int
    content: bytes
    content_type: str = ""

    @property
    def text(self) -> str:
        """
        将原始字节安全解码为 UTF-8 文本。

        :return: UTF-8 字符串，无法解码的字节以替换符保留
        """
        return self.content.decode("utf-8", errors="replace")


class DanmakuStatusResponse(BaseModel):
    """
    面向所有已登录用户的弹幕服务启用状态，不暴露任何上游地址或密钥。
    """

    enabled: bool = Field(default=False, description="当前 MoviePilot 实例是否已启用弹幕代理服务")


class DanmakuCacheStats(BaseModel):
    """
    弹幕本地磁盘缓存用量摘要。
    """

    file_count: int = Field(default=0, ge=0, description="当前已缓存的弹幕压缩文件数量")
    total_bytes: int = Field(default=0, ge=0, description="当前弹幕磁盘缓存占用的总字节数")
    max_mb: int = Field(default=512, ge=16, description="允许占用的最大磁盘缓存配额（MB）")


class DanmakuConfigResponse(BaseModel):
    """
    面向超级管理员的弹幕服务配置快照，敏感密钥仅返回是否已配置标记。
    """

    enabled: bool = Field(default=False, description="是否启用弹幕代理服务")
    api_base_url: str = Field(default="", description="自定义弹弹play 兼容 API 根地址")
    auth_mode: DanmakuAuthMode = Field(
        default="none",
        description="上游鉴权方式：none、dandanplay_v2 或 header_token",
    )
    app_id: str = Field(default="", description="dandanplay_v2 鉴权使用的 AppId")
    has_app_secret: bool = Field(default=False, description="是否已保存加密后的 AppSecret")
    token_header: str = Field(default="Authorization", description="header_token 鉴权使用的请求头名称")
    token_prefix: str = Field(default="Bearer ", description="header_token 鉴权使用的令牌前缀")
    has_token: bool = Field(default=False, description="是否已保存加密后的请求头令牌")
    with_related: bool = Field(default=True, description="拉取弹幕时是否聚合关联第三方弹幕源")
    ch_convert: int = Field(default=1, ge=0, le=2, description="简繁转换参数：0 不转换、1 转简体、2 转繁体")
    cache_ttl_hours: int = Field(default=24, ge=1, le=720, description="弹幕内容磁盘缓存有效期（小时）")
    cache_max_mb: int = Field(default=512, ge=16, le=10240, description="弹幕内容磁盘缓存最大配额（MB）")
    timeout_seconds: int = Field(default=10, ge=2, le=60, description="请求上游弹幕服务的超时时间（秒）")
    use_proxy: bool = Field(default=False, description="是否复用 MoviePilot 全局代理请求上游")
    verify_tls: bool = Field(default=True, description="是否校验证书有效性，自签内网服务可关闭")
    cache_stats: DanmakuCacheStats = Field(
        default_factory=DanmakuCacheStats,
        description="当前弹幕磁盘缓存统计信息",
    )


class DanmakuConfigUpdateRequest(BaseModel):
    """
    管理员更新弹幕服务配置的请求体，留空密钥字段默认保留既有密文。
    """

    enabled: bool = Field(default=False, description="是否启用弹幕代理服务")
    api_base_url: str = Field(default="", max_length=1024, description="自定义弹弹play 兼容 API 根地址")
    auth_mode: DanmakuAuthMode = Field(
        default="none",
        description="上游鉴权方式：none、dandanplay_v2 或 header_token",
    )
    app_id: str = Field(default="", max_length=256, description="dandanplay_v2 鉴权使用的 AppId")
    app_secret: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="可选新 AppSecret；传 None 保留既有值，传空串或配合 clear_app_secret 清空",
    )
    clear_app_secret: bool = Field(default=False, description="是否显式清除已保存的 AppSecret")
    token_header: str = Field(default="Authorization", max_length=128, description="header_token 请求头名称")
    token_prefix: str = Field(default="Bearer ", max_length=64, description="header_token 令牌前缀")
    token: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="可选新请求头令牌；传 None 保留既有值，传空串或配合 clear_token 清空",
    )
    clear_token: bool = Field(default=False, description="是否显式清除已保存的请求头令牌")
    with_related: bool = Field(default=True, description="拉取弹幕时是否聚合关联第三方弹幕源")
    ch_convert: int = Field(default=1, ge=0, le=2, description="简繁转换参数：0 不转换、1 转简体、2 转繁体")
    cache_ttl_hours: int = Field(default=24, ge=1, le=720, description="弹幕内容磁盘缓存有效期（小时）")
    cache_max_mb: int = Field(default=512, ge=16, le=10240, description="弹幕内容磁盘缓存最大配额（MB）")
    timeout_seconds: int = Field(default=10, ge=2, le=60, description="上游请求超时时间（秒）")
    use_proxy: bool = Field(default=False, description="是否复用全局代理")
    verify_tls: bool = Field(default=True, description="是否校验上游 HTTPS 证书")


class DanmakuConfigTestRequest(BaseModel):
    """
    管理员测试弹幕上游连通性的可选覆盖参数，未提供字段时回退已保存配置。
    """

    api_base_url: Optional[str] = Field(default=None, max_length=1024, description="待测试的上游 API 根地址")
    auth_mode: Optional[DanmakuAuthMode] = Field(default=None, description="待测试的鉴权方式")
    app_id: Optional[str] = Field(default=None, max_length=256, description="待测试的 AppId")
    app_secret: Optional[str] = Field(default=None, max_length=1024, description="待测试的 AppSecret")
    token_header: Optional[str] = Field(default=None, max_length=128, description="待测试的请求头名称")
    token_prefix: Optional[str] = Field(default=None, max_length=64, description="待测试的令牌前缀")
    token: Optional[str] = Field(default=None, max_length=2048, description="待测试的请求头令牌")
    timeout_seconds: Optional[int] = Field(default=None, ge=2, le=60, description="探测超时时间（秒）")
    use_proxy: Optional[bool] = Field(default=None, description="是否使用全局代理")
    verify_tls: Optional[bool] = Field(default=None, description="是否校验 HTTPS 证书")


class DanmakuConfigTestResult(BaseModel):
    """
    弹幕上游连通性与鉴权探测结果。
    """

    ok: bool = Field(default=False, description="上游服务是否连通且鉴权通过")
    latency_ms: int = Field(default=0, ge=0, description="探测往返耗时（毫秒）")
    status_code: Optional[int] = Field(default=None, description="上游返回的 HTTP 状态码")
    message: str = Field(default="", description="面向管理员的中文诊断说明（已脱敏）")


class DanmakuEpisodeCandidate(BaseModel):
    """
    单个弹幕分集候选条目。
    """

    episode_id: int = Field(description="上游弹幕分集唯一标识")
    anime_id: Optional[int] = Field(default=None, description="所属作品标识")
    anime_title: str = Field(default="", description="所属作品标题")
    episode_title: str = Field(default="", description="分集标题")
    episode_number: Optional[str] = Field(default=None, description="分集编号文本")
    anime_type: Optional[str] = Field(default=None, description="作品分类类型")
    shift_ms: int = Field(default=0, description="建议或已保存的时间偏移（毫秒）")


class DanmakuMatchRequest(BaseModel):
    """
    客户端请求匹配当前播放媒体弹幕分集的元数据参数。
    """

    item_id: Optional[str] = Field(default=None, max_length=256, description="媒体服务器条目标识")
    server: Optional[str] = Field(default=None, max_length=128, description="媒体服务器配置名称")
    title: Optional[str] = Field(default=None, max_length=512, description="条目展示标题")
    series_name: Optional[str] = Field(default=None, max_length=512, description="所属剧集标题")
    season_number: Optional[int] = Field(default=None, ge=0, le=999, description="季号")
    episode_number: Optional[int] = Field(default=None, ge=0, le=99999, description="集号")
    year: Optional[int] = Field(default=None, ge=1880, le=2200, description="发行或首播年份")
    tmdb_id: Optional[str] = Field(default=None, max_length=64, description="TMDB 标识")
    file_name: Optional[str] = Field(default=None, max_length=512, description="纯媒体文件名")
    file_size: Optional[int] = Field(default=None, ge=0, description="文件字节大小")
    duration_ms: Optional[int] = Field(default=None, ge=0, description="媒体总时长（毫秒）")


class DanmakuMatchResponse(BaseModel):
    """
    弹幕自动匹配或手动绑定查询结果。
    """

    matched: bool = Field(default=False, description="是否已确定可直接加载的目标弹幕分集")
    from_binding: bool = Field(default=False, description="当前结果是否命中后端手动纠偏绑定")
    shift_ms: int = Field(default=0, description="当前条目的时间偏移（毫秒）")
    episode: Optional[DanmakuEpisodeCandidate] = Field(
        default=None,
        description="已匹配或绑定的目标弹幕分集",
    )
    candidates: List[DanmakuEpisodeCandidate] = Field(
        default_factory=list,
        description="可选候选弹幕分集列表，用于用户手动选集纠偏",
    )


class DanmakuAnimeSearchItem(BaseModel):
    """
    手动搜剧返回的单部作品及其可选分集列表。
    """

    anime_id: int = Field(description="上游作品唯一标识")
    anime_title: str = Field(default="", description="作品标题")
    anime_type: Optional[str] = Field(default=None, description="作品类型描述")
    episode_count: Optional[int] = Field(default=None, description="已知分集数量")
    episodes: List[DanmakuEpisodeCandidate] = Field(
        default_factory=list,
        description="搜索接口同时返回的分集候选列表",
    )


class DanmakuBangumiResponse(BaseModel):
    """
    指定作品的完整分集目录。
    """

    anime_id: int = Field(description="上游作品唯一标识")
    anime_title: str = Field(default="", description="作品标题")
    anime_type: Optional[str] = Field(default=None, description="作品类型描述")
    episodes: List[DanmakuEpisodeCandidate] = Field(
        default_factory=list,
        description="按集序排列的分集列表",
    )


class DanmakuCommentsPayload(BaseModel):
    """
    归一化后的紧凑单集弹幕载荷。
    """

    schema_version: int = Field(default=1, description="弹幕载荷协议版本号")
    episode_id: int = Field(description="上游弹幕分集唯一标识")
    count: int = Field(default=0, ge=0, description="当前载荷包含的弹幕总条数")
    cached: bool = Field(default=False, description="本次响应是否命中后端磁盘缓存")
    updated_at: int = Field(default=0, ge=0, description="弹幕缓存写入的 Unix 秒级时间戳")
    items: List[List[Any]] = Field(
        default_factory=list,
        description="紧凑弹幕元组列表：[time_ms, mode, color_rgb, text, source]",
    )


class DanmakuBindingRequest(BaseModel):
    """
    保存或覆盖媒体条目与弹幕分集手动绑定的请求体。
    """

    server: Optional[str] = Field(default=None, max_length=128, description="媒体服务器配置名称")
    item_id: Optional[str] = Field(default=None, max_length=256, description="媒体服务器条目标识")
    tmdb_id: Optional[str] = Field(default=None, max_length=64, description="TMDB 标识")
    season_number: Optional[int] = Field(default=None, ge=0, le=999, description="季号")
    episode_number: Optional[int] = Field(default=None, ge=0, le=99999, description="集号")
    episode_id: int = Field(gt=0, description="手动选定的弹幕分集唯一标识")
    anime_id: Optional[int] = Field(default=None, description="所属作品唯一标识")
    anime_title: str = Field(default="", max_length=512, description="所属作品标题")
    episode_title: str = Field(default="", max_length=512, description="分集标题")
    shift_ms: int = Field(default=0, ge=-600000, le=600000, description="时间偏移（毫秒），负值提前、正值延后")


class DanmakuCacheClearResult(BaseModel):
    """
    清理弹幕缓存后的执行统计。
    """

    removed_files: int = Field(default=0, ge=0, description="已删除的磁盘缓存文件数")
    freed_bytes: int = Field(default=0, ge=0, description="已释放的磁盘空间字节数")
