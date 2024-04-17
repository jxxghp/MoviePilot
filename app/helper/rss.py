import re
import traceback
import xml.dom.minidom
from typing import List, Tuple, Union
from urllib.parse import urljoin

import chardet
from lxml import etree

from app.core.config import settings
from app.helper.browser import PlaywrightHelper
from app.log import logger
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class RssHelper:
    """
    RSS帮助类，解析RSS报文、获取RSS地址等
    """
    # 各站点RSS链接获取配置
    rss_link_conf = {
        "default": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
            }
        },
        "hares.top": {
            "xpath": "//*[@id='layui-layer100001']/div[2]/div/p[4]/a/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
            }
        },
        "et8.org": {
            "xpath": "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/a[2]/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
            }
        },
        "pttime.org": {
            "xpath": "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/text()[5]",
            "url": "getrss.php",
            "params": {
                "showrows": 10,
                "inclbookmarked": 0,
                "itemsmalldescr": 1
            }
        },
        "ourbits.club": {
            "xpath": "//a[@class='gen_rsslink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
            }
        },
        "totheglory.im": {
            "xpath": "//textarea/text()",
            "url": "rsstools.php?c51=51&c52=52&c53=53&c54=54&c108=108&c109=109&c62=62&c63=63&c67=67&c69=69&c70=70&c73=73&c76=76&c75=75&c74=74&c87=87&c88=88&c99=99&c90=90&c58=58&c103=103&c101=101&c60=60",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
            }
        },
        "monikadesign.uk": {
            "xpath": "//a/@href",
            "url": "rss",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
            }
        },
        "zhuque.in": {
            "xpath": "//a/@href",
            "url": "user/rss",
            "render": True,
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
            }
        },
        "hdchina.org": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
                "rsscart": 0
            }
        },
        "audiences.me": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
                "torrent_type": 1,
                "exp": 180
            }
        },
        "shadowflow.org": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "paid": 0,
                "search_mode": 0,
                "showrows": 30
            }
        },
        "hddolby.com": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
                "exp": 180
            }
        },
        "hdhome.org": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
                "exp": 180
            }
        },
        "pthome.net": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
                "exp": 180
            }
        },
        "ptsbao.club": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
                "size": 0
            }
        },
        "leaves.red": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 0,
                "paid": 2
            }
        },
        "hdtime.org": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 0,
            }
        },
        "m-team.io": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "showrows": 50,
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "https": 1
            }
        },
        "u2.dmhy.org": {
            "xpath": "//a[@class='faqlink']/@href",
            "url": "getrss.php",
            "params": {
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "showrows": 50,
                "search_mode": 1,
                "inclautochecked": 1,
                "trackerssl": 1
            }
        },
    }

    @staticmethod
    def parse(url, proxy: bool = False, timeout: int = 15) -> Union[List[dict], None]:
        """
        解析RSS订阅URL，获取RSS中的种子信息
        :param url: RSS地址
        :param proxy: 是否使用代理
        :param timeout: 请求超时
        :return: 种子信息列表，如为None代表Rss过期
        """
        # 开始处理
        ret_array: list = []
        if not url:
            return []
        try:
            ret = RequestUtils(proxies=settings.PROXY if proxy else None, timeout=timeout).get_res(url)
            if not ret:
                return []
        except Exception as err:
            logger.error(f"获取RSS失败：{str(err)} - {traceback.format_exc()}")
            return []
        if ret:
            ret_xml = ""
            try:
                # 使用chardet检测字符编码
                raw_data = ret.content
                if raw_data:
                    try:
                        result = chardet.detect(raw_data)
                        encoding = result['encoding']
                        # 解码为字符串
                        ret_xml = raw_data.decode(encoding)
                    except Exception as e:
                        logger.debug(f"chardet解码失败：{str(e)}")
                        # 探测utf-8解码
                        match = re.search(r'encoding\s*=\s*["\']([^"\']+)["\']', ret.text)
                        if match:
                            encoding = match.group(1)
                            if encoding:
                                ret_xml = raw_data.decode(encoding)
                        else:
                            ret.encoding = ret.apparent_encoding
                if not ret_xml:
                    ret_xml = ret.text
                # 解析XML
                dom_tree = xml.dom.minidom.parseString(ret_xml)
                rootNode = dom_tree.documentElement
                items = rootNode.getElementsByTagName("item")
                for item in items:
                    try:
                        # 标题
                        title = DomUtils.tag_value(item, "title", default="")
                        if not title:
                            continue
                        # 描述
                        description = DomUtils.tag_value(item, "description", default="")
                        # 种子页面
                        link = DomUtils.tag_value(item, "link", default="")
                        # 种子链接
                        enclosure = DomUtils.tag_value(item, "enclosure", "url", default="")
                        if not enclosure and not link:
                            continue
                        # 部分RSS只有link没有enclosure
                        if not enclosure and link:
                            enclosure = link
                        # 大小
                        size = DomUtils.tag_value(item, "enclosure", "length", default=0)
                        if size and str(size).isdigit():
                            size = int(size)
                        else:
                            size = 0
                        # 发布日期
                        pubdate = DomUtils.tag_value(item, "pubDate", default="")
                        if pubdate:
                            # 转换为时间
                            pubdate = StringUtils.get_time(pubdate)
                        # 返回对象
                        tmp_dict = {'title': title,
                                    'enclosure': enclosure,
                                    'size': size,
                                    'description': description,
                                    'link': link,
                                    'pubdate': pubdate}
                        ret_array.append(tmp_dict)
                    except Exception as e1:
                        logger.debug(f"解析RSS失败：{str(e1)} - {traceback.format_exc()}")
                        continue
            except Exception as e2:
                logger.error(f"解析RSS失败：{str(e2)} - {traceback.format_exc()}")
                # RSS过期 观众RSS 链接已过期，您需要获得一个新的！  pthome RSS Link has expired, You need to get a new one!
                _rss_expired_msg = [
                    "RSS 链接已过期, 您需要获得一个新的!",
                    "RSS Link has expired, You need to get a new one!",
                    "RSS Link has expired, You need to get new!"
                ]
                if ret_xml in _rss_expired_msg:
                    return None
        return ret_array

    def get_rss_link(self, url: str, cookie: str, ua: str, proxy: bool = False) -> Tuple[str, str]:
        """
        获取站点rss地址
        :param url: 站点地址
        :param cookie: 站点cookie
        :param ua: 站点ua
        :param proxy: 是否使用代理
        :return: rss地址、错误信息
        """
        try:
            # 获取站点域名
            domain = StringUtils.get_url_domain(url)
            # 获取配置
            site_conf = self.rss_link_conf.get(domain) or self.rss_link_conf.get("default")
            # RSS地址
            rss_url = urljoin(url, site_conf.get("url"))
            # RSS请求参数
            rss_params = site_conf.get("params")
            # 请求RSS页面
            if site_conf.get("render"):
                html_text = PlaywrightHelper().get_page_source(
                    url=rss_url,
                    cookies=cookie,
                    ua=ua,
                    proxies=settings.PROXY if proxy else None
                )
            else:
                res = RequestUtils(
                    cookies=cookie,
                    timeout=60,
                    ua=ua,
                    proxies=settings.PROXY if proxy else None
                ).post_res(url=rss_url, data=rss_params)
                if res:
                    html_text = res.text
                elif res is not None:
                    return "", f"获取 {url} RSS链接失败，错误码：{res.status_code}，错误原因：{res.reason}"
                else:
                    return "", f"获取RSS链接失败：无法连接 {url} "
            # 解析HTML
            html = etree.HTML(html_text)
            if html:
                rss_link = html.xpath(site_conf.get("xpath"))
                if rss_link:
                    return str(rss_link[-1]), ""
            return "", f"获取RSS链接失败：{url}"
        except Exception as e:
            return "", f"获取 {url} RSS链接失败：{str(e)}"
