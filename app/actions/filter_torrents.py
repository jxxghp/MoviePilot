from typing import Optional, List

from pydantic import Field

from app.actions import BaseAction
from app.helper.torrent import TorrentHelper
from app.schemas import ActionParams, ActionContext


class FilterTorrentsParams(ActionParams):
    """
    过滤资源数据参数
    """
    rule_groups: Optional[List[str]] = Field([], description="规则组")
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

    @property
    def name(self) -> str:
        return "过滤资源数据"

    @property
    def description(self) -> str:
        return "过滤资源数据列表"

    @property
    def success(self) -> bool:
        return self.done

    async def execute(self, params: FilterTorrentsParams, context: ActionContext) -> ActionContext:
        """
        过滤torrents中的资源
        """
        for torrent in context.torrents:
            if self.torrenthelper.filter_torrent(
                    torrent_info=torrent.torrent_info,
                    filter_params={
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
