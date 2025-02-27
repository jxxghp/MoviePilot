from app.actions import BaseAction
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas import ActionParams, ActionContext


class AddSubscribeParams(ActionParams):
    """
    添加订阅参数
    """
    pass


class AddSubscribeAction(BaseAction):
    """
    添加订阅
    """

    _added_subscribes = []

    def __init__(self):
        super().__init__()
        self.subscribechain = SubscribeChain()
        self.subscribeoper = SubscribeOper()

    @property
    def name(self) -> str:
        return "添加订阅"

    @property
    def description(self) -> str:
        return "根据媒体列表添加订阅"

    @property
    def data(self) -> dict:
        return AddSubscribeParams().dict()

    @property
    def success(self) -> bool:
        return True if self._added_subscribes else False

    def execute(self, params: dict, context: ActionContext) -> ActionContext:
        """
        将medias中的信息添加订阅，如果订阅不存在的话
        """
        for media in context.medias:
            if self.subscribechain.exists(media):
                logger.info(f"{media.title} 已存在订阅")
                continue
            # 添加订阅
            sid, message = self.subscribechain.add(mtype=media.type,
                                                   title=media.title,
                                                   year=media.year,
                                                   tmdbid=media.tmdb_id,
                                                   season=media.season,
                                                   doubanid=media.douban_id,
                                                   bangumiid=media.bangumi_id,
                                                   mediaid=media.media_id,
                                                   username=settings.SUPERUSER)
            if sid:
                self._added_subscribes.append(sid)

        if self._added_subscribes:
            logger.info(f"已添加 {len(self._added_subscribes)} 个订阅")
            for sid in self._added_subscribes:
                context.subscribes.append(self.subscribeoper.get(sid))

        self.job_done()
        return context
