from threading import Thread
from typing import List, Tuple, Optional

from app.core.cache import cached, cache_backend
from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils


class SubscribeHelper(metaclass=Singleton):
    """
    订阅数据统计/订阅分享等
    """

    _sub_reg = f"{settings.MP_SERVER_HOST}/subscribe/add"

    _sub_done = f"{settings.MP_SERVER_HOST}/subscribe/done"

    _sub_report = f"{settings.MP_SERVER_HOST}/subscribe/report"

    _sub_statistic = f"{settings.MP_SERVER_HOST}/subscribe/statistic"

    _sub_share = f"{settings.MP_SERVER_HOST}/subscribe/share"

    _sub_shares = f"{settings.MP_SERVER_HOST}/subscribe/shares"

    _sub_fork = f"{settings.MP_SERVER_HOST}/subscribe/fork/%s"

    _shares_cache_region = "subscribe_share"

    _github_user = None

    _share_user_id = None

    _admin_users = [
        "jxxghp",
        "thsrite",
        "InfinityPacer",
        "DDSRem",
        "Aqr-K",
        "Putarku",
        "4Nest",
        "xyswordzoro"
    ]

    def __init__(self):
        self.systemconfig = SystemConfigOper()
        if settings.SUBSCRIBE_STATISTIC_SHARE:
            if not self.systemconfig.get(SystemConfigKey.SubscribeReport):
                if self.sub_report():
                    self.systemconfig.set(SystemConfigKey.SubscribeReport, "1")
        self.get_user_uuid()
        self.get_github_user()

    @cached(maxsize=20, ttl=1800)
    def get_statistic(self, stype: str, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        获取订阅统计数据
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        res = RequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_statistic, params={
            "stype": stype,
            "page": page,
            "count": count
        })
        if res and res.status_code == 200:
            return res.json()
        return []

    def sub_reg(self, sub: dict) -> bool:
        """
        新增订阅统计
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False
        res = RequestUtils(proxies=settings.PROXY, timeout=5, headers={
            "Content-Type": "application/json"
        }).post_res(self._sub_reg, json=sub)
        if res and res.status_code == 200:
            return True
        return False

    def sub_done(self, sub: dict) -> bool:
        """
        完成订阅统计
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False
        res = RequestUtils(proxies=settings.PROXY, timeout=5, headers={
            "Content-Type": "application/json"
        }).post_res(self._sub_done, json=sub)
        if res and res.status_code == 200:
            return True
        return False

    def sub_reg_async(self, sub: dict) -> bool:
        """
        异步新增订阅统计
        """
        # 开新线程处理
        Thread(target=self.sub_reg, args=(sub,)).start()
        return True

    def sub_done_async(self, sub: dict) -> bool:
        """
        异步完成订阅统计
        """
        # 开新线程处理
        Thread(target=self.sub_done, args=(sub,)).start()
        return True

    def sub_report(self) -> bool:
        """
        上报存量订阅统计
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False
        subscribes = SubscribeOper().list()
        if not subscribes:
            return True
        res = RequestUtils(proxies=settings.PROXY, content_type="application/json",
                           timeout=10).post(self._sub_report,
                                            json={
                                                "subscribes": [
                                                    sub.to_dict() for sub in subscribes
                                                ]
                                            })
        return True if res else False

    def sub_share(self, subscribe_id: int,
                  share_title: str, share_comment: str, share_user: str) -> Tuple[bool, str]:
        """
        分享订阅
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        subscribe = SubscribeOper().get(subscribe_id)
        if not subscribe:
            return False, "订阅不存在"
        subscribe_dict = subscribe.to_dict()
        subscribe_dict.pop("id")
        cache_backend.clear(region=self._shares_cache_region)
        res = RequestUtils(proxies=settings.PROXY, content_type="application/json",
                           timeout=10).post(self._sub_share,
                                            json={
                                                "share_title": share_title,
                                                "share_comment": share_comment,
                                                "share_user": share_user,
                                                "share_uid": self._share_user_id,
                                                **subscribe_dict
                                            })
        if res is None:
            return False, "连接MoviePilot服务器失败"
        if res.ok:
            # 清除 get_shares 的缓存，以便实时看到结果
            cache_backend.clear(region=self._shares_cache_region)
            return True, ""
        else:
            return False, res.json().get("message")

    def share_delete(self, share_id: int) -> Tuple[bool, str]:
        """
        删除分享
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        res = RequestUtils(proxies=settings.PROXY,
                           timeout=5).delete_res(f"{self._sub_share}/{share_id}",
                                                 params={"share_uid": self._share_user_id})
        if res is None:
            return False, "连接MoviePilot服务器失败"
        if res.ok:
            # 清除 get_shares 的缓存，以便实时看到结果
            cache_backend.clear(region=self._shares_cache_region)
            return True, ""
        else:
            return False, res.json().get("message")

    def sub_fork(self, share_id: int) -> Tuple[bool, str]:
        """
        复用分享的订阅
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        res = RequestUtils(proxies=settings.PROXY, timeout=5, headers={
            "Content-Type": "application/json"
        }).get_res(self._sub_fork % share_id)
        if res is None:
            return False, "连接MoviePilot服务器失败"
        if res.ok:
            return True, ""
        else:
            return False, res.json().get("message")

    @cached(region=_shares_cache_region)
    def get_shares(self, name: Optional[str] = None, page: Optional[int] = 1, count: Optional[int] = 30) -> List[dict]:
        """
        获取订阅分享数据
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        res = RequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_shares, params={
            "name": name,
            "page": page,
            "count": count
        })
        if res and res.status_code == 200:
            return res.json()
        return []

    def get_user_uuid(self) -> str:
        """
        获取用户uuid
        """
        if not self._share_user_id:
            self._share_user_id = SystemUtils.generate_user_unique_id()
            logger.info(f"当前用户UUID: {self._share_user_id}")
        return self._share_user_id

    def get_github_user(self) -> str:
        """
        获取github用户
        """
        if self._github_user is None and settings.GITHUB_HEADERS:
            res = RequestUtils(headers=settings.GITHUB_HEADERS,
                               proxies=settings.PROXY,
                               timeout=15).get_res(f"https://api.github.com/user")
            if res:
                self._github_user = res.json().get("login")
                logger.info(f"当前Github用户: {self._github_user}")
        return self._github_user

    def is_admin_user(self) -> bool:
        """
        判断是否是管理员
        """
        if not self._github_user:
            return False
        if self._github_user in self._admin_users:
            return True
        return False
