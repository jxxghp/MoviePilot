"""
解析 RSS 资源并按需匹配媒体信息。
"""

from typing import Any, Optional, cast

from pydantic import Field

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.rss import RssHelper
from app.chain.media import MediaChain
from app.domain.context import Context, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class FetchRssParams(ActionParams):
    """
    获取RSS资源列表参数
    """
    url: Optional[str] = Field(default=None, description="RSS地址")
    proxy: Optional[bool] = Field(default=False, description="是否使用代理")
    timeout: Optional[int] = Field(default=15, description="超时时间")
    content_type: Optional[str] = Field(default=None, description="请求使用的 Content-Type 头；不传时由客户端决定")
    referer: Optional[str] = Field(default=None, description="请求使用的 Referer 来源地址；不传时省略")
    ua: Optional[str] = Field(default=None, description="请求使用的 User-Agent 标识；不传时由客户端决定")
    match_media: Optional[bool] = Field(default=False, description="匹配媒体信息")


class FetchRssAction(BaseAction):
    """
    获取RSS资源列表
    """

    contract = {
        "outputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
    }

    def __init__(self, action_id: str):
        """
        建立当前动作的资源集合与失败状态。

        :param action_id: 当前工作流动作的标识
        """
        super().__init__(action_id)
        self._rss_torrents: list[Context] = []
        self._has_error = False

    name = "获取RSS资源"
    description = "订阅RSS地址获取资源"
    data = FetchRssParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回 RSS 请求和解析过程是否未记录错误。

        :return: 未遇到请求或解析错误时为 True
        """
        return not self._has_error

    def execute(self, workflow_id: int, params: dict[str, Any], context: ActionContext) -> ActionContext:
        """
        请求 RSS 地址并将解析出的资源加入工作流上下文。

        :param workflow_id: 当前工作流标识，用于检查停止状态
        :param params: RSS 地址、请求选项和媒体匹配开关
        :param context: 用于接收解析结果的工作流上下文
        :return: 原工作流上下文，动作状态按请求结果更新
        """
        action_params = FetchRssParams(**params)
        if not action_params.url:
            return context

        headers = {}
        if action_params.content_type:
            headers["Content-Type"] = action_params.content_type
        if action_params.referer:
            headers["Referer"] = action_params.referer
        if action_params.ua:
            headers["User-Agent"] = action_params.ua

        rss_items = RssHelper().parse(url=action_params.url,
                                      proxy=(
                                          get_chain_runtime_config_snapshot().proxy
                                          if action_params.proxy
                                          else False
                                      ),
                                      timeout=action_params.timeout,
                                      headers=headers)
        if not isinstance(rss_items, list):
            logger.error(f'RSS地址 {action_params.url} 请求失败！')
            self._has_error = True
            return context

        if not rss_items:
            logger.error(f'RSS地址 {action_params.url} 未获取到RSS数据！')
            return context

        # 组装种子
        for item in rss_items:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            if not item.get("title"):
                continue
            torrentinfo = TorrentInfo(
                title=item.get("title"),
                enclosure=item.get("enclosure"),
                page_url=item.get("link"),
                size=float(item["size"]) if isinstance(item.get("size"), (int, float)) else 0.0,
                pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
            )
            meta = MetaInfo(title=torrentinfo.title or "", subtitle=torrentinfo.description)
            mediainfo = None
            if action_params.match_media:
                mediainfo = MediaChain().recognize_by_meta(
                    meta,
                    obtain_images=False,
                )
                if not mediainfo:
                    logger.warning(f"{torrentinfo.title} 未识别到媒体信息")
                    continue
            self._rss_torrents.append(Context(meta_info=meta, media_info=mediainfo, torrent_info=torrentinfo))

        if self._rss_torrents:
            logger.info(f"获取到 {len(self._rss_torrents)} 个RSS资源")
            if context.torrents is None:
                context.torrents = []
            context.torrents.extend(cast(Any, self._rss_torrents))

        self.job_done(f"获取到 {len(self._rss_torrents)} 个资源")
        return context
