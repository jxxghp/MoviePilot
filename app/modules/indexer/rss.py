"""
索引模块拥有的 RSS 生成页面、表单参数及过期响应协议。

这里只选择和解释站点配方，HTTP、浏览器和 XML 解析仍由公共用例执行。
"""

from typing import Any
from urllib.parse import urljoin, urlparse

from app.application.rss import RssLinkRecipe

_DEFAULT_PARAMS = {
    "inclbookmarked": 0, "itemsmalldescr": 1, "showrows": 50, "search_mode": 1,
}
_DEFAULT_XPATH = "//a[@class='faqlink']/@href"

# 只登记相对于通用配方的专站差异，配方构造时复制参数，避免跨请求共享可变状态。
_SITE_RECIPES: dict[str, dict[str, Any]] = {
    "hares.top": {"xpath": "//*[@id='layui-layer100001']/div[2]/div/p[4]/a/@href"},
    "et8.org": {"xpath": "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/a[2]/@href"},
    "pttime.org": {
        "xpath": "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/text()[5]",
        "params": {"showrows": 10, "inclbookmarked": 0, "itemsmalldescr": 1},
    },
    "ourbits.club": {"xpath": "//a[@class='gen_rsslink']/@href"},
    "totheglory.im": {
        "xpath": "//textarea/text()",
        "url": "rsstools.php?c51=51&c52=52&c53=53&c54=54&c108=108&c109=109&c62=62&c63=63&c67=67"
               "&c69=69&c70=70&c73=73&c76=76&c75=75&c74=74&c87=87&c88=88&c99=99&c90=90"
               "&c58=58&c103=103&c101=101&c60=60",
    },
    "monikadesign.uk": {"xpath": "//a/@href", "url": "rss"},
    "zhuque.in": {"xpath": "//a/@href", "url": "user/rss", "render": True},
    "hdwing.org": {"params": {**_DEFAULT_PARAMS, "rsscart": 0}},
    "audiences.me": {"params": {**_DEFAULT_PARAMS, "torrent_type": 1, "exp": 180}},
    "shadowflow.org": {
        "params": {"inclbookmarked": 0, "itemsmalldescr": 1, "paid": 0, "search_mode": 0, "showrows": 30},
    },
    "hddolby.com": {"params": {**_DEFAULT_PARAMS, "exp": 180}},
    "hdhome.org": {"params": {**_DEFAULT_PARAMS, "exp": 180}},
    "pthome.net": {"params": {**_DEFAULT_PARAMS, "exp": 180}},
    "ptsbao.club": {"params": {**_DEFAULT_PARAMS, "size": 0}},
    "leaves.red": {"params": {**_DEFAULT_PARAMS, "search_mode": 0, "paid": 2}},
    "hdtime.org": {"params": {**_DEFAULT_PARAMS, "search_mode": 0}},
    "m-team.io": {"params": {"showrows": 50, "inclbookmarked": 0, "itemsmalldescr": 1, "https": 1}},
    "u2.dmhy.org": {"params": {**_DEFAULT_PARAMS, "inclautochecked": 1, "trackerssl": 1}},
}
_EXPIRED_MESSAGES = frozenset({
    "RSS 链接已过期, 您需要获得一个新的!",
    "RSS Link has expired, You need to get a new one!",
    "RSS Link has expired, You need to get new!",
})


def get_rss_recipe(url: str) -> RssLinkRecipe:
    """
    根据主机的精确或子域匹配选择专站 RSS 配方，未登记的站点使用通用配方。

    :param url: 站点根地址
    :return: 已解析完整页面地址且参数独立的 RSS 获取配方
    """
    hostname = (urlparse(url).hostname or "").lower()
    conf = next((value for domain, value in _SITE_RECIPES.items()
                 if hostname == domain or hostname.endswith(f".{domain}")), {})
    return RssLinkRecipe(
        url=urljoin(url, conf.get("url") or "getrss.php"),
        xpath=conf.get("xpath") or _DEFAULT_XPATH,
        params=dict(conf.get("params") or _DEFAULT_PARAMS),
        render=bool(conf.get("render")),
    )


def is_rss_expired(text: str) -> bool:
    """
    解释站点 RSS 协议明确给出的过期响应，其他错误不冒充过期。

    :param text: 原始 RSS 响应正文
    :return: 是否属于支持的过期响应
    """
    return text.strip() in _EXPIRED_MESSAGES
