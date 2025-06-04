import gc
import re
import traceback
from typing import List, Tuple, Union, Optional
from urllib.parse import urljoin

import chardet
from lxml import etree

from app.core.config import settings
from app.helper.browser import PlaywrightHelper
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class RssHelper:
    """
    RSS帮助类，解析RSS报文、获取RSS地址等
    """
    
    # RSS解析限制配置
    MAX_RSS_SIZE = 50 * 1024 * 1024  # 50MB最大RSS文件大小
    MAX_RSS_ITEMS = 1000  # 最大解析条目数
    
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

    def parse(self, url, proxy: bool = False, timeout: Optional[int] = 15, headers: dict = None) -> Union[List[dict], None, bool]:
        """
        解析RSS订阅URL，获取RSS中的种子信息
        :param url: RSS地址
        :param proxy: 是否使用代理
        :param timeout: 请求超时
        :param headers: 自定义请求头
        :return: 种子信息列表，如为None代表Rss过期，如果为False则为错误
        """
        # 开始处理
        ret_array: list = []
        if not url:
            return False
        
        try:
            ret = RequestUtils(proxies=settings.PROXY if proxy else None,
                               timeout=timeout, headers=headers).get_res(url)
            if not ret:
                return False
        except Exception as err:
            logger.error(f"获取RSS失败：{str(err)} - {traceback.format_exc()}")
            return False
        
        if ret:
            ret_xml = None
            root = None
            try:
                # 检查响应大小，避免处理过大的RSS文件
                raw_data = ret.content
                if raw_data and len(raw_data) > self.MAX_RSS_SIZE:
                    logger.warning(f"RSS文件过大: {len(raw_data)/1024/1024:.1f}MB，跳过解析")
                    return False
                
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
                
                # 使用lxml.etree解析XML
                parser = None
                try:
                    # 创建解析器，禁用网络访问以提高安全性和性能
                    parser = etree.XMLParser(
                        recover=True,  # 容错模式
                        strip_cdata=False,  # 保留CDATA
                        resolve_entities=False,  # 禁用外部实体解析
                        no_network=True,  # 禁用网络访问
                        huge_tree=False  # 禁用大文档解析，避免内存问题
                    )
                    root = etree.fromstring(ret_xml.encode('utf-8'), parser=parser)
                except etree.XMLSyntaxError:
                    # 如果XML解析失败，尝试作为HTML解析
                    try:
                        root = etree.HTML(ret_xml)
                        if root is not None:
                            # 查找RSS根节点
                            rss_root = root.xpath('//rss | //feed')
                            if rss_root:
                                root = rss_root[0]
                    except Exception as e:
                        logger.error(f"HTML解析也失败：{str(e)}")
                        return False
                finally:
                    if parser is not None:
                        del parser
                
                if root is None:
                    logger.error("无法解析RSS内容")
                    return False
                
                # 查找所有item或entry节点
                items = root.xpath('.//item | .//entry')
                
                # 限制处理的条目数量
                items_count = min(len(items), self.MAX_RSS_ITEMS)
                if len(items) > self.MAX_RSS_ITEMS:
                    logger.warning(f"RSS条目过多: {len(items)}，仅处理前{self.MAX_RSS_ITEMS}个")
                
                for i, item in enumerate(items[:items_count]):
                    try:
                        # 定期执行垃圾回收
                        if i > 0 and i % 100 == 0:
                            gc.collect()
                        
                        # 使用xpath提取信息，更高效
                        title_nodes = item.xpath('.//title')
                        title = title_nodes[0].text if title_nodes and title_nodes[0].text else ""
                        if not title:
                            continue
                        
                        # 描述
                        desc_nodes = item.xpath('.//description | .//summary')
                        description = desc_nodes[0].text if desc_nodes and desc_nodes[0].text else ""
                        
                        # 种子页面
                        link_nodes = item.xpath('.//link')
                        if link_nodes:
                            link = link_nodes[0].text if hasattr(link_nodes[0], 'text') and link_nodes[0].text else link_nodes[0].get('href', '')
                        else:
                            link = ""
                        
                        # 种子链接
                        enclosure_nodes = item.xpath('.//enclosure')
                        enclosure = enclosure_nodes[0].get('url', '') if enclosure_nodes else ""
                        if not enclosure and not link:
                            continue
                        # 部分RSS只有link没有enclosure
                        if not enclosure and link:
                            enclosure = link
                        
                        # 大小
                        size = 0
                        if enclosure_nodes:
                            size_attr = enclosure_nodes[0].get('length', '0')
                            if size_attr and str(size_attr).isdigit():
                                size = int(size_attr)
                        
                        # 发布日期
                        pubdate_nodes = item.xpath('.//pubDate | .//published | .//updated')
                        pubdate = ""
                        if pubdate_nodes and pubdate_nodes[0].text:
                            pubdate = StringUtils.get_time(pubdate_nodes[0].text)
                        
                        # 获取豆瓣昵称
                        nickname_nodes = item.xpath('.//*[local-name()="creator"]')
                        nickname = nickname_nodes[0].text if nickname_nodes and nickname_nodes[0].text else ""
                        
                        # 返回对象
                        tmp_dict = {
                            'title': title,
                            'enclosure': enclosure,
                            'size': size,
                            'description': description,
                            'link': link,
                            'pubdate': pubdate
                        }
                        # 如果豆瓣昵称不为空，返回数据增加豆瓣昵称，供doubansync插件获取
                        if nickname:
                            tmp_dict['nickname'] = nickname
                        ret_array.append(tmp_dict)
                        
                    except Exception as e1:
                        logger.debug(f"解析RSS条目失败：{str(e1)} - {traceback.format_exc()}")
                        continue
                        
            except Exception as e2:
                logger.error(f"解析RSS失败：{str(e2)} - {traceback.format_exc()}")
                # RSS过期检查
                _rss_expired_msg = [
                    "RSS 链接已过期, 您需要获得一个新的!",
                    "RSS Link has expired, You need to get a new one!",
                    "RSS Link has expired, You need to get new!"
                ]
                if ret_xml in _rss_expired_msg:
                    return None
                return False
            finally:
                if root is not None:
                    del root
                if ret_xml is not None:
                    del ret_xml
                gc.collect()
        
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
            if html_text:
                html = None
                try:
                    html = etree.HTML(html_text)
                    if StringUtils.is_valid_html_element(html):
                        rss_link = html.xpath(site_conf.get("xpath"))
                        if rss_link:
                            return str(rss_link[-1]), ""
                finally:
                    if html is not None:
                        del html
                    
            return "", f"获取RSS链接失败：{url}"
        except Exception as e:
            return "", f"获取 {url} RSS链接失败：{str(e)}"
