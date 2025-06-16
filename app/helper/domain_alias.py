from typing import Dict, List


class DomainAliasHelper:
    """
    域名别名管理工具类
    用于处理同一站点的不同域名别名，防止重复入库
    以最后入站的域名为准
    """

    # 域名别名组配置
    # 格式：[同一站点的所有域名列表]
    DOMAIN_ALIAS_GROUPS: List[List[str]] = [
        ["pt.gtk.pw", "pt.gtkpw.xyz"],  # GTK站点的所有域名
        # 可以在这里添加更多站点的域名组
        # ["example.com", "alias1.example.com", "alias2.example.com"],
    ]

    # 域名到组的映射：域名 -> 组索引
    _DOMAIN_TO_GROUP: Dict[str, int] = {}

    def __init__(self):
        """初始化域名组映射"""
        self._build_domain_mapping()

    def _build_domain_mapping(self):
        """构建域名到组的映射"""
        self._DOMAIN_TO_GROUP.clear()
        for group_index, domains in enumerate(self.DOMAIN_ALIAS_GROUPS):
            for domain in domains:
                self._DOMAIN_TO_GROUP[domain] = group_index
    
    def get_all_domains(self, domain: str) -> List[str]:
        """
        获取域名的所有相关域名（同一站点的所有域名）

        :param domain: 输入域名
        :return: 所有相关域名列表
        """
        if not domain:
            return [domain] if domain else []

        # 查找域名所属的组
        if domain in self._DOMAIN_TO_GROUP:
            group_index = self._DOMAIN_TO_GROUP[domain]
            return self.DOMAIN_ALIAS_GROUPS[group_index].copy()

        # 如果不在任何组中，返回自身
        return [domain]

    def is_same_site(self, domain1: str, domain2: str) -> bool:
        """
        检查两个域名是否属于同一站点

        :param domain1: 域名1
        :param domain2: 域名2
        :return: 是否为同一站点
        """
        if not domain1 or not domain2:
            return False

        # 检查是否在同一个域名组中
        group1 = self._DOMAIN_TO_GROUP.get(domain1)
        group2 = self._DOMAIN_TO_GROUP.get(domain2)

        return group1 is not None and group1 == group2

    def get_alias_info(self) -> List[List[str]]:
        """
        获取所有域名别名组信息

        :return: 域名别名组列表
        """
        return [group.copy() for group in self.DOMAIN_ALIAS_GROUPS]


