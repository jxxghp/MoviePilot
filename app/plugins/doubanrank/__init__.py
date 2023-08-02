import re
import xml.dom.minidom
from threading import Event
from typing import Tuple, List, Dict, Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils


class DoubanRank(_PluginBase):

    # 插件名称
    plugin_name = "豆瓣榜单订阅"
    # 插件描述
    plugin_desc = "监控豆瓣热门榜单，自动添加订阅。"
    # 插件图标
    plugin_icon = "movie.jpg"
    # 主题色
    plugin_color = "#01B3E3"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubanrank_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 2

    # 退出事件
    _event = Event()
    # 私有属性
    mediaserver = None
    subscribe = None
    rsshelper = None
    media = None
    _douban_address = {
        'movie-ustop': 'https://rsshub.app/douban/movie/ustop',
        'movie-weekly': 'https://rsshub.app/douban/movie/weekly',
        'movie-real-time': 'https://rsshub.app/douban/movie/weekly/subject_real_time_hotest',
        'show-domestic': 'https://rsshub.app/douban/movie/weekly/show_domestic',
        'movie-hot-gaia': 'https://rsshub.app/douban/movie/weekly/movie_hot_gaia',
        'tv-hot': 'https://rsshub.app/douban/movie/weekly/tv_hot',
        'movie-top250': 'https://rsshub.app/douban/movie/weekly/movie_top250',
    }
    _enable = False
    _cron = ""
    _rss_addrs = []
    _ranks = []
    _vote = 0
    _scheduler = None
    
    def init_plugin(self, config: dict = None):
        if config:
            self._enable = config.get("enable")
            self._cron = config.get("cron")
            self._vote = float(config.get("vote")) if config.get("vote") else 0
            rss_addrs = config.get("rss_addrs")
            if rss_addrs:
                if isinstance(rss_addrs, str):
                    self._rss_addrs = rss_addrs.split('\n')
                else:
                    self._rss_addrs = rss_addrs
            else:
                self._rss_addrs = []
            self._ranks = config.get("ranks") or []

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enable:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                logger.info(f"豆瓣榜单订阅服务启动，周期：{self._cron}")
                self._scheduler.add_job(self.__refresh_rss,
                                        CronTrigger.from_crontab(self._cron))

            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        pass

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        pass

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    def __refresh_rss(self):
        """
        刷新RSS
        """
        logger.info(f"开始刷新RSS ...")
        addr_list = self._rss_addrs + [self._douban_address.get(rank) for rank in self._ranks]
        if not addr_list:
            logger.info(f"未设置RSS地址")
            return
        else:
            logger.info(f"共 {len(addr_list)} 个RSS地址需要刷新")
        for addr in addr_list:
            if not addr:
                continue
            try:
                logger.info(f"获取RSS：{addr} ...")
                rss_infos = self.__get_rss_info(addr)
                if not rss_infos:
                    logger.error(f"RSS地址：{addr} ，未查询到数据")
                    continue
                else:
                    logger.info(f"RSS地址：{addr} ，共 {len(rss_infos)} 条数据")
                for rss_info in rss_infos:
                    if self._event.is_set():
                        logger.info(f"订阅服务停止")
                        return

                    title = rss_info.get('title')
                    douban_id = rss_info.get('doubanid')
                    mtype = rss_info.get('type')
                    unique_flag = f"doubanrank: {title} (DB:{douban_id})"
                    # TODO 检查是否已处理过
                    # TODO 识别媒体信息
                    # TODO 检查媒体服务器是否存在
                    # TODO 检查是否已订阅过
                    # TODO　添加处理历史
                    # TODO 添加订阅
                    # TODO 发送通知
                    # TODO 更新历史记录
            except Exception as e:
                logger.error(str(e))
        logger.info(f"所有榜单RSS刷新完成")

    @staticmethod
    def __get_rss_info(addr):
        """
        获取RSS
        """
        try:
            ret = RequestUtils().get_res(addr)
            if not ret:
                return []
            ret.encoding = ret.apparent_encoding
            ret_xml = ret.text
            ret_array = []
            # 解析XML
            dom_tree = xml.dom.minidom.parseString(ret_xml)
            rootNode = dom_tree.documentElement
            items = rootNode.getElementsByTagName("item")
            for item in items:
                try:
                    # 标题
                    title = DomUtils.tag_value(item, "title", default="")
                    # 链接
                    link = DomUtils.tag_value(item, "link", default="")
                    if not title and not link:
                        logger.warn(f"条目标题和链接均为空，无法处理")
                        continue
                    doubanid = re.findall(r"/(\d+)/", link)
                    if doubanid:
                        doubanid = doubanid[0]
                    if doubanid and not str(doubanid).isdigit():
                        logger.warn(f"解析的豆瓣ID格式不正确：{doubanid}")
                        continue
                    # 返回对象
                    ret_array.append({
                        'title': title,
                        'link': link,
                        'doubanid': doubanid
                    })
                except Exception as e1:
                    logger.error("解析RSS条目失败：" + str(e1))
                    continue
            return ret_array
        except Exception as e:
            logger.error("获取RSS失败：" + str(e))
            return []
