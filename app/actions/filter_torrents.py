from typing import Optional, List

from pydantic import Field

from app.actions import BaseAction, ActionChain
from app.helper.torrent import TorrentHelper
from app.schemas import ActionParams, ActionContext


class FilterTorrentsParams(ActionParams):
    """
    过滤资源数据参数
    """
    rule_groups: Optional[List[str]] = Field([], description="规则组")
    quality: Optional[str] = Field(None, description="资源质量")
    resolution: Optional[str] = Field(None, description="资源分辨率")
    effect: Optional[str] = Field(None, description="特效")
    include: Optional[str] = Field(None, description="包含规则")
    exclude: Optional[str] = Field(None, description="排除规则")
    size: Optional[str] = Field(None, description="资源大小范围（MB）")


class FilterTorrentsAction(BaseAction):
    """
    过滤资源数据
    """

    _torrents = []

    def __init__(self):
        super().__init__()
        self.torrenthelper = TorrentHelper()
        self.chain = ActionChain()

    @property
    def name(self) -> str:
        return "过滤资源"

    @property
    def description(self) -> str:
        return "对资源列表数据进行过滤"

    @property
    def data(self) -> dict:
        return FilterTorrentsParams().dict()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, params: dict, context: ActionContext) -> ActionContext:
        """
        过滤torrents中的资源
        """
        params = FilterTorrentsParams(**params)
        for torrent in context.torrents:
            if self.torrenthelper.filter_torrent(
                    torrent_info=torrent.torrent_info,
                    filter_params={
                        "quality": params.quality,
                        "resolution": params.resolution,
                        "effect": params.effect,
                        "include": params.include,
                        "exclude": params.exclude,
                        "size": params.size
                    }
            ):
                if self.chain.filter_torrents(
                        rule_groups=params.rule_groups,
                        torrent_list=[torrent.torrent_info],
                        mediainfo=torrent.media_info
                ):
                    self._torrents.append(torrent)

        context.torrents = self._torrents

        self.job_done()
        return context
