from typing import Optional

from pydantic import Field

from app.actions import BaseAction, ActionChain
from app.core.config import settings, global_vars
from app.core.context import Context
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.schemas import ActionParams, ActionContext, TorrentInfo


class FetchRssParams(ActionParams):
    """
    获取RSS资源列表参数
    """
    url: str = Field(default=None, description="RSS地址")
    proxy: Optional[bool] = Field(default=False, description="是否使用代理")
    timeout: Optional[int] = Field(default=15, description="超时时间")
    content_type: Optional[str] = Field(default=None, description="Content-Type")
    referer: Optional[str] = Field(default=None, description="Referer")
    ua: Optional[str] = Field(default=None, description="User-Agent")
    match_media: Optional[str] = Field(default=None, description="匹配媒体信息")


class FetchRssAction(BaseAction):
    """
    获取RSS资源列表
    """

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._rss_torrents = []
        self._has_error = False

    @classmethod
    @property
    def name(cls) -> str:  # noqa
        return "获取RSS资源"

    @classmethod
    @property
    def description(cls) -> str:  # noqa
        return "订阅RSS地址获取资源"

    @classmethod
    @property
    def data(cls) -> dict:  # noqa
        return FetchRssParams().dict()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        请求RSS地址获取数据，并解析为资源列表
        """
        params = FetchRssParams(**params)
        if not params.url:
            return context

        headers = {}
        if params.content_type:
            headers["Content-Type"] = params.content_type
        if params.referer:
            headers["Referer"] = params.referer
        if params.ua:
            headers["User-Agent"] = params.ua

        rss_items = RssHelper().parse(url=params.url,
                                      proxy=settings.PROXY if params.proxy else None,
                                      timeout=params.timeout,
                                      headers=headers)
        if rss_items is None or rss_items is False:
            logger.error(f'RSS地址 {params.url} 请求失败！')
            self._has_error = True
            return context

        if not rss_items:
            logger.error(f'RSS地址 {params.url} 未获取到RSS数据！')
            return context

        # 组装种子
        for item in rss_items:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            if not item.get("title"):
                continue
            torrentinfo = TorrentInfo(
                title=item.get("title"),
                enclosure=item.get("enclosure"),
                page_url=item.get("link"),
                size=item.get("size"),
                pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
            )
            meta = MetaInfo(title=torrentinfo.title, subtitle=torrentinfo.description)
            mediainfo = None
            if params.match_media:
                mediainfo = ActionChain().recognize_media(meta)
                if not mediainfo:
                    logger.warning(f"{torrentinfo.title} 未识别到媒体信息")
                    continue
            self._rss_torrents.append(Context(meta_info=meta, media_info=mediainfo, torrent_info=torrentinfo))

        if self._rss_torrents:
            logger.info(f"获取到 {len(self._rss_torrents)} 个RSS资源")
            context.torrents.extend(self._rss_torrents)

        self.job_done(f"获取到 {len(self._rss_torrents)} 个资源")
        return context
