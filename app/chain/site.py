from typing import Union

from app.chain import ChainBase
from app.core.config import settings
from app.db.site_oper import SiteOper
from app.helper.cookie import CookieHelper
from app.log import logger


class SiteChain(ChainBase):
    """
    站点远程管理处理链
    """

    _siteoper: SiteOper = None
    _cookiehelper: CookieHelper = None

    def __init__(self):
        super().__init__()
        self._siteoper = SiteOper()
        self._cookiehelper = CookieHelper()

    def list(self, userid: Union[str, int] = None):
        """
        查询所有站点，发送消息
        """
        site_list = self._siteoper.list()
        if not site_list:
            self.post_message(title="没有维护任何站点信息！")
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
        self.post_message(title=title, text="\n".join(messages), userid=userid)

    def disable(self, arg_str, userid: Union[str, int] = None):
        """
        禁用站点
        """
        if not arg_str:
            return
        arg_str = str(arg_str).strip()
        if not arg_str.isdigit():
            return
        site_id = int(arg_str)
        site = self._siteoper.get(site_id)
        if not site:
            self.post_message(title=f"站点编号 {site_id} 不存在！", userid=userid)
            return
        # 禁用站点
        self._siteoper.update(site_id, {
            "is_active": False
        })
        # 重新发送消息
        self.list()

    def enable(self, arg_str, userid: Union[str, int] = None):
        """
        启用站点
        """
        if not arg_str:
            return
        arg_str = str(arg_str).strip()
        if not arg_str.isdigit():
            return
        site_id = int(arg_str)
        site = self._siteoper.get(site_id)
        if not site:
            self.post_message(title=f"站点编号 {site_id} 不存在！", userid=userid)
            return
        # 禁用站点
        self._siteoper.update(site_id, {
            "is_active": True
        })
        # 重新发送消息
        self.list()

    def get_cookie(self, arg_str: str, userid: Union[str, int] = None):
        """
        使用用户名密码更新站点Cookie
        """
        err_title = "请输入正确的命令格式：/site_cookie [id] [username] [password]，" \
                    "[id]为站点编号，[uername]为站点用户名，[password]为站点密码"
        if not arg_str:
            self.post_message(title=err_title, userid=userid)
            return
        arg_str = str(arg_str).strip()
        args = arg_str.split()
        if len(args) != 3:
            self.post_message(title=err_title, userid=userid)
            return
        site_id = args[0]
        if not site_id.isdigit():
            self.post_message(title=err_title, userid=userid)
            return
        # 站点ID
        site_id = int(site_id)
        # 站点信息
        site_info = self._siteoper.get(site_id)
        if not site_info:
            self.post_message(title=f"站点编号 {site_id} 不存在！", userid=userid)
            return
        # 用户名
        username = args[1]
        # 密码
        password = args[2]
        # 更新站点Cookie
        result = self._cookiehelper.get_site_cookie_ua(
            url=site_info.url,
            username=username,
            password=password,
            proxies=settings.PROXY_HOST if site_info.proxy else None
        )
        if result:
            cookie, ua, msg = result
            if not cookie:
                logger.error(msg)
                self.post_message(title=f"【{site_info.name}】 Cookie&UA更新失败！",
                                  text=f"错误原因：{msg}",
                                  userid=userid)
                return
            self._siteoper.update(site_id, {
                "cookie": cookie,
                "ua": ua
            })
            self.post_message(title=f"【{site_info.name}】 Cookie&UA更新成功",
                              userid=userid)
