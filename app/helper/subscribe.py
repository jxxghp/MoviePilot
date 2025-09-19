from threading import Thread
from typing import List, Tuple, Optional

from app.core.cache import cached
from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.utils.singleton import WeakSingleton
from app.utils.system import SystemUtils


class SubscribeHelper(metaclass=WeakSingleton):
    """
    订阅数据统计/订阅分享等
    """

    _sub_reg = f"{settings.MP_SERVER_HOST}/subscribe/add"

    _sub_done = f"{settings.MP_SERVER_HOST}/subscribe/done"

    _sub_report = f"{settings.MP_SERVER_HOST}/subscribe/report"

    _sub_statistic = f"{settings.MP_SERVER_HOST}/subscribe/statistic"

    _sub_share = f"{settings.MP_SERVER_HOST}/subscribe/share"

    _sub_shares = f"{settings.MP_SERVER_HOST}/subscribe/shares"

    _sub_share_statistic = f"{settings.MP_SERVER_HOST}/subscribe/share/statistics"

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
        "xyswordzoro",
        "wikrin"
    ]

    def __init__(self):
        systemconfig = SystemConfigOper()
        if settings.SUBSCRIBE_STATISTIC_SHARE:
            if not systemconfig.get(SystemConfigKey.SubscribeReport):
                if self.sub_report():
                    systemconfig.set(SystemConfigKey.SubscribeReport, "1")
        self.get_user_uuid()
        self.get_github_user()

    @staticmethod
    def _check_subscribe_share_enabled() -> Tuple[bool, str]:
        """
        检查订阅分享功能是否开启
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        return True, ""

    @staticmethod
    def _validate_subscribe(subscribe) -> Tuple[bool, str]:
        """
        验证订阅是否存在
        """
        if not subscribe:
            return False, "订阅不存在"
        return True, ""

    @staticmethod
    def _prepare_subscribe_data(subscribe) -> dict:
        """
        准备订阅分享数据
        """
        subscribe_dict = subscribe.to_dict()
        subscribe_dict.pop("id", None)
        return subscribe_dict

    def _build_share_payload(self, share_title: str, share_comment: str,
                             share_user: str, subscribe_dict: dict) -> dict:
        """
        构建分享请求载荷
        """
        return {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": self._share_user_id,
            **subscribe_dict
        }

    def _handle_response(self, res, clear_cache: bool = True) -> Tuple[bool, str]:
        """
        处理HTTP响应
        """
        if res is None:
            return False, "连接MoviePilot服务器失败"

        # 检查响应状态
        if res and res.status_code == 200:
            # 清除缓存
            if clear_cache:
                self.get_shares.cache_clear()
                self.get_statistic.cache_clear()
                self.get_share_statistics.cache_clear()
                self.async_get_shares.cache_clear()
                self.async_get_statistic.cache_clear()
                self.async_get_share_statistics.cache_clear()
            return True, ""
        else:
            return False, res.json().get("message")

    @staticmethod
    def _handle_list_response(res) -> List[dict]:
        """
        处理返回List的HTTP响应
        """
        if res and res.status_code == 200:
            return res.json()
        return []

    @cached(region=_shares_cache_region, maxsize=5, ttl=1800, skip_empty=True)
    def get_statistic(self, stype: str, page: Optional[int] = 1, count: Optional[int] = 30,
                      genre_id: Optional[int] = None, min_rating: Optional[float] = None,
                      max_rating: Optional[float] = None, sort_type: Optional[str] = None) -> List[dict]:
        """
        获取订阅统计数据
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return []

        params = {
            "stype": stype,
            "page": page,
            "count": count
        }

        # 添加可选参数
        if genre_id is not None:
            params["genre_id"] = genre_id
        if min_rating is not None:
            params["min_rating"] = min_rating
        if max_rating is not None:
            params["max_rating"] = max_rating
        if sort_type is not None:
            params["sort_type"] = sort_type

        res = RequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_statistic, params=params)

        return self._handle_list_response(res)

    @cached(region=_shares_cache_region, maxsize=5, ttl=1800, skip_empty=True)
    async def async_get_statistic(self, stype: str, page: Optional[int] = 1, count: Optional[int] = 30,
                                  genre_id: Optional[int] = None, min_rating: Optional[float] = None,
                                  max_rating: Optional[float] = None, sort_type: Optional[str] = None) -> List[dict]:
        """
        异步获取订阅统计数据
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return []

        params = {
            "stype": stype,
            "page": page,
            "count": count
        }

        # 添加可选参数
        if genre_id is not None:
            params["genre_id"] = genre_id
        if min_rating is not None:
            params["min_rating"] = min_rating
        if max_rating is not None:
            params["max_rating"] = max_rating
        if sort_type is not None:
            params["sort_type"] = sort_type

        res = await AsyncRequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_statistic, params=params)

        return self._handle_list_response(res)

    def sub_reg(self, sub: dict) -> bool:
        """
        新增订阅统计
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return False
        res = RequestUtils(proxies=settings.PROXY, timeout=5, headers={
            "Content-Type": "application/json"
        }).post_res(self._sub_reg, json=sub)
        if res and res.status_code == 200:
            return True
        return False

    async def async_sub_reg(self, sub: dict) -> bool:
        """
        异步新增订阅统计
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return False
        res = await AsyncRequestUtils(proxies=settings.PROXY, timeout=5, headers={
            "Content-Type": "application/json"
        }).post_res(self._sub_reg, json=sub)
        if res and res.status_code == 200:
            return True
        return False

    def sub_done(self, sub: dict) -> bool:
        """
        完成订阅统计
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
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
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
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
        # 检查功能是否开启
        enabled, message = self._check_subscribe_share_enabled()
        if not enabled:
            return False, message

        # 获取订阅信息
        subscribe = SubscribeOper().get(subscribe_id)

        # 验证订阅
        valid, message = self._validate_subscribe(subscribe)
        if not valid:
            return False, message

        # 准备数据
        subscribe_dict = self._prepare_subscribe_data(subscribe)
        payload = self._build_share_payload(share_title, share_comment, share_user, subscribe_dict)

        # 发送分享请求
        res = RequestUtils(proxies=settings.PROXY, content_type="application/json",
                           timeout=10).post(self._sub_share, json=payload)

        return self._handle_response(res)

    async def async_sub_share(self, subscribe_id: int,
                              share_title: str, share_comment: str, share_user: str) -> Tuple[bool, str]:
        """
        异步分享订阅
        """
        # 检查功能是否开启
        enabled, message = self._check_subscribe_share_enabled()
        if not enabled:
            return False, message

        # 获取订阅信息
        subscribe = await SubscribeOper().async_get(subscribe_id)

        # 验证订阅
        valid, message = self._validate_subscribe(subscribe)
        if not valid:
            return False, message

        # 准备数据
        subscribe_dict = self._prepare_subscribe_data(subscribe)
        payload = self._build_share_payload(share_title, share_comment, share_user, subscribe_dict)

        # 发送分享请求
        res = await AsyncRequestUtils(proxies=settings.PROXY, content_type="application/json",
                                      timeout=10).post(self._sub_share, json=payload)

        return self._handle_response(res)

    def share_delete(self, share_id: int) -> Tuple[bool, str]:
        """
        删除分享
        """
        # 检查功能是否开启
        enabled, message = self._check_subscribe_share_enabled()
        if not enabled:
            return False, message

        res = RequestUtils(proxies=settings.PROXY,
                           timeout=5).delete_res(f"{self._sub_share}/{share_id}",
                                                 params={"share_uid": self._share_user_id})

        return self._handle_response(res)

    async def async_share_delete(self, share_id: int) -> Tuple[bool, str]:
        """
        异步删除分享
        """
        # 检查功能是否开启
        enabled, message = self._check_subscribe_share_enabled()
        if not enabled:
            return False, message

        res = await AsyncRequestUtils(proxies=settings.PROXY,
                                      timeout=5).delete_res(f"{self._sub_share}/{share_id}",
                                                            params={"share_uid": self._share_user_id})

        return self._handle_response(res)

    def sub_fork(self, share_id: int) -> Tuple[bool, str]:
        """
        复用分享的订阅
        """
        # 检查功能是否开启
        enabled, message = self._check_subscribe_share_enabled()
        if not enabled:
            return False, message

        res = RequestUtils(proxies=settings.PROXY, timeout=5, headers={
            "Content-Type": "application/json"
        }).get_res(self._sub_fork % share_id)

        return self._handle_response(res, clear_cache=False)

    async def async_sub_fork(self, share_id: int) -> Tuple[bool, str]:
        """
        异步复用分享的订阅
        """
        # 检查功能是否开启
        enabled, message = self._check_subscribe_share_enabled()
        if not enabled:
            return False, message

        res = await AsyncRequestUtils(proxies=settings.PROXY, timeout=5, headers={
            "Content-Type": "application/json"
        }).get_res(self._sub_fork % share_id)

        return self._handle_response(res, clear_cache=False)

    @cached(region=_shares_cache_region, maxsize=1, ttl=1800, skip_empty=True)
    def get_shares(self, name: Optional[str] = None, page: Optional[int] = 1, count: Optional[int] = 30,
                   genre_id: Optional[int] = None, min_rating: Optional[float] = None,
                   max_rating: Optional[float] = None, sort_type: Optional[str] = None) -> List[dict]:
        """
        获取订阅分享数据
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return []

        params = {
            "name": name,
            "page": page,
            "count": count
        }
        
        # 添加可选参数
        if genre_id is not None:
            params["genre_id"] = genre_id
        if min_rating is not None:
            params["min_rating"] = min_rating
        if max_rating is not None:
            params["max_rating"] = max_rating
        if sort_type is not None:
            params["sort_type"] = sort_type

        res = RequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_shares, params=params)

        return self._handle_list_response(res)

    @cached(region=_shares_cache_region, maxsize=1, ttl=1800, skip_empty=True)
    async def async_get_shares(self, name: Optional[str] = None, page: Optional[int] = 1, count: Optional[int] = 30,
                               genre_id: Optional[int] = None, min_rating: Optional[float] = None,
                               max_rating: Optional[float] = None, sort_type: Optional[str] = None) -> List[dict]:
        """
        异步获取订阅分享数据
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return []

        params = {
            "name": name,
            "page": page,
            "count": count
        }
        
        # 添加可选参数
        if genre_id is not None:
            params["genre_id"] = genre_id
        if min_rating is not None:
            params["min_rating"] = min_rating
        if max_rating is not None:
            params["max_rating"] = max_rating
        if sort_type is not None:
            params["sort_type"] = sort_type

        res = await AsyncRequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_shares, params=params)

        return self._handle_list_response(res)

    @cached(region=_shares_cache_region, maxsize=1, ttl=1800, skip_empty=True)
    def get_share_statistics(self) -> List[dict]:
        """
        获取订阅分享统计数据
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return []

        res = RequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_share_statistic)

        return self._handle_list_response(res)

    @cached(region=_shares_cache_region, maxsize=1, ttl=1800, skip_empty=True)
    async def async_get_share_statistics(self) -> List[dict]:
        """
        异步获取订阅分享统计数据
        """
        enabled, _ = self._check_subscribe_share_enabled()
        if not enabled:
            return []

        res = await AsyncRequestUtils(proxies=settings.PROXY, timeout=15).get_res(self._sub_share_statistic)

        return self._handle_list_response(res)

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
