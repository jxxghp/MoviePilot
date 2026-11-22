"""
通过可乐（ColaPT）开放 API 的会员数据接口读取身份、流量、瓶盖、做种和未读消息数量。
"""

import json
from typing import Any, Optional

import app.modules.indexer.spider.colapt as colapt_rules
from app.adapters.network.http import RequestUtils
from app.application.site.throttle import defer_site_request, site_request_config, wait_for_site_request
from app.domain import torrent as torrent_rules
from app.modules.indexer.parser import SitePage, SiteParserBase, SiteSchema
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.exception import RateLimitExceededException


class ColaPTSiteUserInfo(SiteParserBase):
    """
    可乐开放 API 用户数据解析器，失败时不探测 NexusPHP 页面或发布半份身份。
    """

    schema = SiteSchema.ColaPT
    request_mode = "apikey"
    allow_schema_fallback = False

    def __init__(self, site_name: Optional[str], url: str, site_cookie: Optional[str], apikey: Optional[str],
                 token: Optional[str], request_utils: Optional[RequestUtils] = None, ua: Optional[str] = None,
                 emulate: bool = False, proxy: Optional[bool] = None, api_url: Optional[str] = None) -> None:
        """
        保留解析器构造合同，但禁用网页 Cookie、会话与浏览器仿真。

        :param site_name: 站点展示名称
        :param url: 网页地址，用于站点流控和默认 API 地址
        :param site_cookie: 保留旧合同，不用于 API 认证
        :param apikey: 具备「读取」范围的 API Key
        :param token: 保留旧合同，不替代 API Key
        :param request_utils: 可注入的 HTTP 客户端
        :param ua: 请求 User-Agent
        :param emulate: 保留旧合同，不启动浏览器
        :param proxy: 是否使用站点代理
        :param api_url: 资源声明的 API 基础地址
        """
        super().__init__(site_name=site_name, url=url, site_cookie=None, apikey=apikey, token=token,
                         request_utils=request_utils or RequestUtils(), ua=ua, emulate=False, proxy=proxy,
                         api_url=api_url)

    def parse(self) -> None:
        """
        只请求会员数据接口，完整校验后才发布用户 ID，避免把错误响应保存为有效统计。
        """
        self.userid = None
        self.err_msg = None
        response = None
        rate_site = site_request_config(self._site_url)
        apikey = str(self.apikey or "").strip()
        try:
            if not apikey:
                raise ValueError(colapt_rules.KEY_REQUIRED)
            self._parse_site_page({})
            wait_for_site_request(rate_site)
            response = self._request_utils.get_res(
                url=f"{self._base_url}/me/stats", cookies=None, headers=self._addition_headers,
                proxies=get_runtime_setting("PROXY") if self._proxy else None, timeout=60, allow_redirects=False,
            )
            if response is None:
                raise ValueError("无法连接 ColaPT API")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            data = colapt_rules.response_data(response.status_code, payload, apikey)
            self._parse_user_base_info(data)
        except (TypeError, ValueError, RateLimitExceededException) as err:
            self.err_msg = str(err).replace(apikey, "[已隐藏]") if apikey else str(err)
            if torrent_rules.rate_limit_cooldown(self.err_msg) is not None:
                defer_site_request(rate_site, self.err_msg)
            logger.warning(f"{self._site_name} 用户数据刷新失败：{self.err_msg}")
        finally:
            if response is not None:
                response.close()
            self.close()

    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        配置开放 API 来源并禁用所有网页入口。

        :param html_text: 保留基类签名，不解析 HTML
        """
        self._base_url = str(self._api_url or f"{self._site_url.rstrip('/')}/api/v1").rstrip("/")
        self._addition_headers = {"X-API-Key": str(self.apikey or "").strip(), "Accept": "application/json",
                                  "User-Agent": self._ua}
        self._user_basic_page = self._user_detail_page = self._user_traffic_page = None
        self._torrent_seeding_page = self._user_mail_unread_page = self._sys_mail_unread_page = None

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        一次投影会员数据，字节、瓶盖和人数校验完毕后才公开身份。

        身份取 user.id / user.username，注册时间取 joined_at；流量取计费口径（billed_*），与站点分享率一致；
        分享率为 null 表示没有计费下载量，保留为空。

        :param html_text: 已校验的响应对象或其 JSON 文本
        :raises ValueError: 用户身份或必要统计字段无效
        """
        data: dict[str, Any] = json.loads(html_text) if isinstance(html_text, str) else html_text
        user = data.get("user")
        if not isinstance(user, dict):
            raise ValueError("ColaPT API 用户身份无效")
        user_id = colapt_rules.integer(user.get("id"), minimum=1)
        if not isinstance(user.get("username"), str) or not user["username"].strip():
            raise ValueError("ColaPT API 用户名无效")
        join_at = colapt_rules.local_time(data.get("joined_at"))
        if join_at is None:
            raise ValueError("ColaPT API 时间字段无效")
        class_name = data.get("class_name")
        if not isinstance(class_name, (str, type(None))):
            raise ValueError("ColaPT API 用户等级无效")
        ratio = data.get("ratio")
        values = {
            "username": user["username"], "user_level": class_name, "join_at": join_at,
            "upload": colapt_rules.integer(data.get("billed_uploaded_bytes")),
            "download": colapt_rules.integer(data.get("billed_downloaded_bytes")),
            "ratio": None if ratio is None else colapt_rules.decimal(ratio),
            "bonus": colapt_rules.decimal(data.get("points_balance"), signed=True),
            "seeding": colapt_rules.integer(data.get("seeding")),
            "seeding_size": colapt_rules.integer(data.get("seeding_bytes", "0")),
            "leeching": colapt_rules.integer(data.get("leeching")),
            "completed": colapt_rules.integer(data.get("completions")),
            "message_unread": colapt_rules.integer(data.get("unread_messages")),
        }
        for name, value in values.items():
            setattr(self, name, value)
        self.userid = user_id

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        会员数据已提供流量，不再读取网页。

        :param html_text: 保留基类签名，不读取页面
        """

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        会员数据已提供等级和瓶盖，不再读取用户详情页。

        :param html_text: 保留基类签名，不读取页面
        """

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Optional[str]:
        """
        直接使用会员数据的做种数量和字节体积，不重复请求做种分页。

        :param html_text: 保留基类签名，不读取页面
        :param multi_page: 保留基类签名，不进行页面翻页
        :return: 没有后续页面
        """
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """
        保留未读数量，不额外读取或标记站内信。

        :param html_text: 保留基类签名，不读取页面
        :param msg_links: 不添加网页消息地址
        :return: 没有消息后续页面
        """
        return None

    def _parse_message_content(self, html_text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        本次适配只提供未读数量，不自动访问站内信正文。

        :param html_text: 保留基类签名，不读取页面
        :return: 空标题、时间和正文
        """
        return None, None, None
