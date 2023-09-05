from typing import Union, Tuple

from sqlalchemy.orm import Session

from app.chain import ChainBase
from app.core.config import settings
from app.db.models.site import Site
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.cookie import CookieHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import MessageChannel, Notification
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils


class SiteChain(ChainBase):
    """
    站点管理处理链
    """

    def __init__(self, db: Session = None):
        super().__init__(db)
        self.siteoper = SiteOper(self._db)
        self.cookiehelper = CookieHelper()
        self.message = MessageHelper()

    def test(self, url: str) -> Tuple[bool, str]:
        """
        测试站点是否可用
        :param url: 站点域名
        :return: (是否可用, 错误信息)
        """
        # 检查域名是否可用
        domain = StringUtils.get_url_domain(url)
        site_info = self.siteoper.get_by_domain(domain)
        if not site_info:
            return False, f"站点【{url}】不存在"
        site_url = site_info.url
        site_cookie = site_info.cookie
        ua = site_info.ua
        render = site_info.render
        public = site_info.public
        proxies = settings.PROXY if site_info.proxy else None
        proxy_server = settings.PROXY_SERVER if site_info.proxy else None
        # 模拟登录
        try:
            # 访问链接
            if render:
                page_source = PlaywrightHelper().get_page_source(url=site_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server)
                if not public and not SiteUtils.is_logged_in(page_source):
                    if under_challenge(page_source):
                        return False, f"无法通过Cloudflare！"
                    return False, f"仿真登录失败，Cookie已失效！"
            else:
                res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=proxies
                                   ).get_res(url=site_url)
                # 判断登录状态
                if res and res.status_code in [200, 500, 403]:
                    if not public and not SiteUtils.is_logged_in(res.text):
                        if under_challenge(res.text):
                            msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                        elif res.status_code == 200:
                            msg = "Cookie已失效"
                        else:
                            msg = f"状态码：{res.status_code}"
                        return False, f"{msg}！"
                    elif public and res.status_code != 200:
                        return False, f"状态码：{res.status_code}！"
                elif res is not None:
                    return False, f"状态码：{res.status_code}！"
                else:
                    return False, f"无法打开网站！"
        except Exception as e:
            return False, f"{str(e)}！"
        return True, "连接成功"

    def remote_list(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        查询所有站点，发送消息
        """
        site_list = self.siteoper.list()
        if not site_list:
            self.post_message(Notification(
                channel=channel,
                title="没有维护任何站点信息！",
                userid=userid))
        title = f"共有 {len(site_list)} 个站点，回复对应指令操作：" \
                f"\n- 禁用站点：/site_disable [id]" \
                f"\n- 启用站点：/site_enable [id]" \
                f"\n- 更新站点Cookie：/site_cookie [id] [username] [password]"
        messages = []
        for site in site_list:
            if site.render:
                render_str = "🧭"
            else:
                render_str = ""
            if site.is_active:
                messages.append(f"{site.id}. [{site.name}]({site.url}){render_str}")
            else:
                messages.append(f"{site.id}. {site.name}")
        # 发送列表
        self.post_message(Notification(
            channel=channel,
            title=title, text="\n".join(messages), userid=userid))

    def remote_disable(self, arg_str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        禁用站点
        """
        if not arg_str:
            return
        arg_str = str(arg_str).strip()
        if not arg_str.isdigit():
            return
        site_id = int(arg_str)
        site = self.siteoper.get(site_id)
        if not site:
            self.post_message(Notification(
                channel=channel,
                title=f"站点编号 {site_id} 不存在！",
                userid=userid))
            return
        # 禁用站点
        self.siteoper.update(site_id, {
            "is_active": False
        })
        # 重新发送消息
        self.remote_list(channel, userid)

    def remote_enable(self, arg_str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        启用站点
        """
        if not arg_str:
            return
        arg_strs = str(arg_str).split()
        for arg_str in arg_strs:
            arg_str = arg_str.strip()
            if not arg_str.isdigit():
                continue
            site_id = int(arg_str)
            site = self.siteoper.get(site_id)
            if not site:
                self.post_message(Notification(
                    channel=channel,
                    title=f"站点编号 {site_id} 不存在！", userid=userid))
                return
            # 禁用站点
            self.siteoper.update(site_id, {
                "is_active": True
            })
        # 重新发送消息
        self.remote_list(channel, userid)

    def update_cookie(self, site_info: Site,
                      username: str, password: str) -> Tuple[bool, str]:
        """
        根据用户名密码更新站点Cookie
        :param site_info: 站点信息
        :param username: 用户名
        :param password: 密码
        :return: (是否成功, 错误信息)
        """
        # 更新站点Cookie
        result = self.cookiehelper.get_site_cookie_ua(
            url=site_info.url,
            username=username,
            password=password,
            proxies=settings.PROXY_HOST if site_info.proxy else None
        )
        if result:
            cookie, ua, msg = result
            if not cookie:
                return False, msg
            self.siteoper.update(site_info.id, {
                "cookie": cookie,
                "ua": ua
            })
            return True, msg
        return False, "未知错误"

    def remote_cookie(self, arg_str: str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        使用用户名密码更新站点Cookie
        """
        err_title = "请输入正确的命令格式：/site_cookie [id] [username] [password]，" \
                    "[id]为站点编号，[uername]为站点用户名，[password]为站点密码"
        if not arg_str:
            self.post_message(Notification(
                channel=channel,
                title=err_title, userid=userid))
            return
        arg_str = str(arg_str).strip()
        args = arg_str.split()
        if len(args) != 3:
            self.post_message(Notification(
                channel=channel,
                title=err_title, userid=userid))
            return
        site_id = args[0]
        if not site_id.isdigit():
            self.post_message(Notification(
                channel=channel,
                title=err_title, userid=userid))
            return
        # 站点ID
        site_id = int(site_id)
        # 站点信息
        site_info = self.siteoper.get(site_id)
        if not site_info:
            self.post_message(Notification(
                channel=channel,
                title=f"站点编号 {site_id} 不存在！", userid=userid))
            return
        self.post_message(Notification(
            channel=channel,
            title=f"开始更新【{site_info.name}】Cookie&UA ...", userid=userid))
        # 用户名
        username = args[1]
        # 密码
        password = args[2]
        # 更新Cookie
        status, msg = self.update_cookie(site_info=site_info,
                                         username=username,
                                         password=password)
        if not status:
            logger.error(msg)
            self.post_message(Notification(
                channel=channel,
                title=f"【{site_info.name}】 Cookie&UA更新失败！",
                text=f"错误原因：{msg}",
                userid=userid))
        else:
            self.post_message(Notification(
                channel=channel,
                title=f"【{site_info.name}】 Cookie&UA更新成功",
                userid=userid))
