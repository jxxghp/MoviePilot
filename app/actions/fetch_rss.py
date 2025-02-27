from typing import Optional

from pydantic import Field

from app.actions import BaseAction, ActionChain
from app.core.config import settings
from app.core.context import Context
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.schemas import ActionParams, ActionContext, TorrentInfo


class FetchRssParams(ActionParams):
    """
    获取RSS资源列表参数
    """
    url: str = Field(None, description="RSS地址")
    proxy: Optional[bool] = Field(False, description="是否使用代理")
    timeout: Optional[int] = Field(15, description="超时时间")
    content_type: Optional[str] = Field(None, description="Content-Type")
    referer: Optional[str] = Field(None, description="Referer")
    ua: Optional[str] = Field(None, description="User-Agent")


class FetchRssAction(BaseAction):
    """
    获取RSS资源列表
    """

    _rss_torrents = []

    def __init__(self):
        super().__init__()
        self.rsshelper = RssHelper()
        self.chain = ActionChain()

    @property
    def name(self) -> str:
        return "获取RSS资源列表"

    @property
    def description(self) -> str:
        return "请求RSS地址获取数据，并解析为资源列表"

    @property
    def data(self) -> dict:
        return FetchRssParams().dict()

    @property
    def success(self) -> bool:
        return True if self._rss_torrents else False

    async def execute(self, params: FetchRssParams, context: ActionContext) -> ActionContext:
        """
        请求RSS地址获取数据，并解析为资源列表
        """
        if not params.url:
            return context

        headers = {}
        if params.content_type:
            headers["Content-Type"] = params.content_type
        if params.referer:
            headers["Referer"] = params.referer
        if params.ua:
            headers["User-Agent"] = params.ua

        rss_items = self.rsshelper.parse(url=params.url,
                                         proxy=settings.PROXY if params.proxy else None,
                                         timeout=params.timeout,
                                         headers=headers)
        if not rss_items:
            logger.error(f'RSS地址 {params.url} 未获取到RSS数据！')
            return context

        # 组装种子
        for item in rss_items:
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
            mediainfo = self.chain.recognize_media(meta)
            if not mediainfo:
                logger.warning(f"{torrentinfo.title} 未识别到媒体信息")
                continue
            self._rss_torrents.append(Context(meta_info=meta, media_info=mediainfo, torrent_info=torrentinfo))

        if self._rss_torrents:
            logger.info(f"已获取 {len(self._rss_torrents)} 个RSS资源")
            context.torrents.extend(self._rss_torrents)

        self.job_done()
        return context
