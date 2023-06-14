from app.chain import ChainBase
from app.db.sites import Sites


class SiteManageChain(ChainBase):
    """
    站点远程管理处理链
    """

    _sites: Sites = None

    def __init__(self):
        super().__init__()
        self._sites = Sites()

    def process(self):
        """
        查询所有站点，发送消息
        """
        site_list = self._sites.list()
        if not site_list:
            self.post_message(title="没有维护任何站点信息！")
        title = f"共有 {len(site_list)} 个站点，回复 `/site_disable` `[id]` 禁用站点，回复 `/site_enable` `[id]` 启用站点："
        messages = []
        for site in site_list:
            if site.render:
                render_str = "【仿真】"
            else:
                render_str = ""
            if site.is_active:
                messages.append(f"{site.id}. [{site.name}]{site.url}{render_str}")
            else:
                messages.append(f"{site.id}. ~~{site.name}~~")
        # 发送列表
        self.post_message(title=title, text="\n".join(messages))

    def disable(self, arg_str):
        """
        禁用站点
        """
        if not arg_str:
            return
        arg_str = arg_str.strip()
        if not arg_str.isdigit():
            return
        site_id = int(arg_str)
        site = self._sites.get(site_id)
        if not site:
            self.post_message(title=f"站点编号 {site_id} 不存在！")
            return
        # 禁用站点
        self._sites.update(site_id, {
            "is_active": False
        })
        # 重新发送消息
        self.process()

    def enable(self, arg_str):
        """
        启用站点
        """
        if not arg_str:
            return
        arg_str = arg_str.strip()
        if not arg_str.isdigit():
            return
        site_id = int(arg_str)
        site = self._sites.get(site_id)
        if not site:
            self.post_message(title=f"站点编号 {site_id} 不存在！")
            return
        # 禁用站点
        self._sites.update(site_id, {
            "is_active": True
        })
        # 重新发送消息
        self.process()
