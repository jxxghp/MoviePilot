"""
解析 FluxTorrent 兼容层的用户资料与流量信息。
"""

import json
from typing import Any, Dict, Optional, Tuple

from app.adapters.network.http import RequestUtils
from app.application.site.throttle import site_request_config
from app.foundation import temporal as time_tools
from app.modules.indexer.parser import SitePage, SiteParserBase, SiteSchema
from app.modules.indexer.spider.fluxtorrent import FluxTorrentSpider, get_api_response, site_request_limits
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


class FluxTorrentSiteUserInfo(SiteParserBase):
    """
    FluxTorrent 兼容层用户数据解析器。

    兼容层按 NexusPHP userdetails 口径返回用户资料，因此不需要解析站点页面。
    """

    schema = SiteSchema.FluxTorrent
    request_mode = "apikey"
    # 开放 API 认证失败时保留接口错误，避免继续尝试网页模型并触发网关限流。
    allow_schema_fallback = False

    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        配置兼容层用户信息接口地址和认证请求头。

        :param html_text: API Key 模式下的空首页数据
        """
        self._user_basic_page = "api/v1/compat/nexusphp/user.json"
        self._user_basic_params = {}
        self._user_detail_page = None
        # 老版本兼容层的 user.json 还没有做种与注册时间，用 PTPP 聚合端点补齐；
        # 新版本已经返回时 `_parse_user_base_info` 会把这里置空，少发一次请求。
        self._user_traffic_page = "api/v1/plugins/ptppUserInfo"
        self._torrent_seeding_page = None
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None
        token = self.apikey or self.token or ""
        headers: Dict[str, Optional[str]] = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        headers["User-Agent"] = FluxTorrentSpider.get_user_agent(self._ua)
        self._addition_headers = headers

    def _get_page_content(
        self,
        url: str,
        params: Optional[dict[str, object]] = None,
        headers: Optional[dict[str, Optional[str]]] = None,
        method: Optional[str] = None,
    ) -> str:
        """
        使用只读 API 传输和共享流控读取资料，网关 503 最多退避重试两次。

        :param url: 用户资料或补充统计接口的完整地址
        :param params: 兼容基类参数，只读接口使用查询参数
        :param headers: 兼容基类参数，认证头由开放 API 令牌构造
        :param method: 兼容基类参数，只读接口始终使用 GET
        :return: 接口响应文本，无响应时返回空字符串
        """
        response = get_api_response(RequestUtils(
            headers=self._addition_headers,
            proxies=get_runtime_setting("PROXY") if self._proxy else None,
            timeout=60,
        ), url=url, limits=site_request_limits(site_request_config(self._site_url)), params=params)
        try:
            return response.text if response is not None else ""
        finally:
            if response is not None:
                response.close()

    def _load_api_payload(self, html_text: SitePage, operation: str) -> Optional[Dict[str, Any]]:
        """
        解析并校验兼容层统一响应信封。

        :param html_text: 兼容层响应文本
        :param operation: 错误信息中的操作名称
        :return: 成功时返回响应 data，失败时返回 None
        """
        if not html_text:
            self.err_msg = f"{operation}失败，未收到接口响应"
            return None
        payload: Any
        if isinstance(html_text, dict):
            payload = html_text
        else:
            try:
                payload = json.loads(html_text)
            except (TypeError, json.JSONDecodeError) as err:
                self.err_msg = f"{operation}失败，接口响应不是有效 JSON"
                logger.warning(f"{self._site_name} {self.err_msg}：{str(err)}")
                return None
        if not isinstance(payload, dict):
            self.err_msg = f"{operation}失败，接口响应结构无效"
            logger.warning(f"{self._site_name} {self.err_msg}")
            return None
        if payload.get("code") != 0:
            self.err_msg = payload.get("message") or f"{operation}失败"
            logger.warning(f"{self._site_name} {operation}失败：{self.err_msg}")
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        解析兼容层返回的用户资料与流量统计。

        :param html_text: user.json 接口响应文本
        """
        user_info = self._load_api_payload(html_text, "获取用户信息")
        if not user_info:
            self._user_traffic_page = None
            return
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = user_info.get("class_name") or str(user_info.get("class") or "")
        self.upload = int(user_info.get("uploaded") or 0)
        self.download = int(user_info.get("downloaded") or 0)
        # 站点在只上传未下载时把分享率记为无穷大，此时没有可用的有限数值。
        if user_info.get("ratio_infinite") is True:
            self.ratio = 0.0
        else:
            self.ratio = float(user_info.get("ratio") or 0)
        self.bonus = float(user_info.get("seedbonus") or 0)
        # 兼容层直接返回做种统计与注册时间；老版本没有这些字段时保持默认
        # 0 / None，交给 PTPP 端点补齐，不能因此判成解析失败。
        self.seeding = int(user_info.get("seeding") or 0)
        self.seeding_size = int(
            user_info.get("seeding_size") or user_info.get("seedingSize") or 0
        )
        join_at = user_info.get("join_at") or user_info.get("joinTime")
        self.join_at = (
            time_tools.normalize_datetime(str(join_at)) if join_at else None
        )
        # 三项字段齐全说明站点已是新版兼容层，不必再查一次聚合端点：
        # 开放 API 有配额，两个端点的口径也不必再比一遍。
        has_seeding = user_info.get("seeding") is not None
        has_size = any(
            user_info.get(key) is not None
            for key in ("seeding_size", "seedingSize")
        )
        has_join = any(
            user_info.get(key) is not None for key in ("join_at", "joinTime")
        )
        if has_seeding and has_size and has_join:
            self._user_traffic_page = None

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        用 PTPP 聚合端点补齐老版本站点缺失的做种数与注册时间。

        user.json 已经返回这些字段时不覆盖，避免两个端点口径互相打架。

        :param html_text: ptppUserInfo 接口响应文本
        """
        previous_error = self.err_msg
        info = self._load_api_payload(html_text, "获取做种统计")
        # 补充端点失败不能把已经成功的用户资料判成失败：
        # 老站可能没有部署它，令牌也可能没有该接口权限。
        if previous_error is None:
            self.err_msg = None
        if not info:
            return
        if not self.seeding:
            self.seeding = int(info.get("seeding") or 0)
        if not self.seeding_size:
            self.seeding_size = int(info.get("seedingSize") or 0)
        if not self.join_at:
            join_time = info.get("joinTime")
            self.join_at = (
                time_tools.normalize_datetime(str(join_time))
                if join_time else None
            )

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        跳过独立用户详情页面，兼容层用户接口已经返回完整资料。

        :param html_text: 未使用的页面文本
        """

    def _parse_user_torrent_seeding_info(
        self,
        html_text: str,
        multi_page: bool = False,
    ) -> Optional[str]:
        """
        跳过独立做种列表，统计已由 user.json / PTPP 端点给出。

        :param html_text: 未使用的页面文本
        :param multi_page: 是否为后续分页
        :return: 始终返回 None
        """
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """
        兼容层未提供站内消息接口，跳过未读消息解析。

        :param html_text: 未使用的页面文本
        :param msg_links: 兼容解析器基类签名的消息链接容器
        :return: 始终返回 None
        """
        return None

    def _parse_message_content(
        self,
        html_text: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        兼容层未提供站内消息接口，跳过消息内容解析。

        :param html_text: 未使用的页面文本
        :return: 三个空值
        """
        return None, None, None
