import gc
import pickle
import traceback
from abc import ABCMeta
from pathlib import Path
from typing import Optional, Any, Tuple, List, Set, Union, Dict

from qbittorrentapi import TorrentFilesList
from ruamel.yaml import CommentedMap
from transmission_rpc import File

from app.core.config import settings
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.event import EventManager
from app.core.meta import MetaBase
from app.core.module import ModuleManager
from app.db.message_oper import MessageOper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent, ExistMediaInfo, DownloadingTorrent, CommingMessage, Notification, \
    WebhookEventInfo, TmdbEpisode, MediaPerson
from app.schemas.types import TorrentStatus, MediaType, MediaImageType, EventType
from app.utils.object import ObjectUtils


class ChainBase(metaclass=ABCMeta):
    """
    处理链基类
    """

    def __init__(self):
        """
        公共初始化
        """
        self.modulemanager = ModuleManager()
        self.eventmanager = EventManager()
        self.messageoper = MessageOper()
        self.messagehelper = MessageHelper()

    @staticmethod
    def load_cache(filename: str) -> Any:
        """
        从本地加载缓存
        """
        cache_path = settings.TEMP_PATH / filename
        if cache_path.exists():
            try:
                with open(cache_path, 'rb') as f:
                    return pickle.load(f)
            except Exception as err:
                logger.error(f"加载缓存 {filename} 出错：{str(err)}")
        return None

    @staticmethod
    def save_cache(cache: Any, filename: str) -> None:
        """
        保存缓存到本地
        """
        try:
            with open(settings.TEMP_PATH / filename, 'wb') as f:
                pickle.dump(cache, f)
        except Exception as err:
            logger.error(f"保存缓存 {filename} 出错：{str(err)}")
        finally:
            # 主动资源回收
            del cache
            gc.collect()

    @staticmethod
    def remove_cache(filename: str) -> None:
        """
        删除本地缓存
        """
        cache_path = settings.TEMP_PATH / filename
        if cache_path.exists():
            Path(cache_path).unlink()

    def run_module(self, method: str, *args, **kwargs) -> Any:
        """
        运行包含该方法的所有模块，然后返回结果
        当kwargs包含命名参数raise_exception时，如模块方法抛出异常且raise_exception为True，则同步抛出异常
        """

        def is_result_empty(ret):
            """
            判断结果是否为空
            """
            if isinstance(ret, tuple):
                return all(value is None for value in ret)
            else:
                return result is None

        logger.debug(f"请求模块执行：{method} ...")
        result = None
        modules = self.modulemanager.get_running_modules(method)
        for module in modules:
            module_id = module.__class__.__name__
            try:
                module_name = module.get_name()
            except Exception as err:
                logger.error(f"获取模块名称出错：{str(err)}")
                module_name = module_id
            try:
                func = getattr(module, method)
                if is_result_empty(result):
                    # 返回None，第一次执行或者需继续执行下一模块
                    result = func(*args, **kwargs)
                elif ObjectUtils.check_signature(func, result):
                    # 返回结果与方法签名一致，将结果传入（不能多个模块同时运行的需要通过开关控制）
                    result = func(result)
                elif isinstance(result, list):
                    # 返回为列表，有多个模块运行结果时进行合并（不能多个模块同时运行的需要通过开关控制）
                    temp = func(*args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    # 中止继续执行
                    break
            except Exception as err:
                if kwargs.get("raise_exception"):
                    raise
                logger.error(
                    f"运行模块 {module_id}.{method} 出错：{str(err)}\n{traceback.format_exc()}")
                self.messagehelper.put(title=f"{module_name}发生了错误",
                                       message=str(err),
                                       role="system")
                self.eventmanager.send_event(
                    EventType.SystemError,
                    {
                        "type": "module",
                        "module_id": module_id,
                        "module_name": module_name,
                        "module_method": method,
                        "error": str(err),
                        "traceback": traceback.format_exc()
                    }
                )
        return result

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        tmdbid: int = None,
                        doubanid: str = None,
                        bangumiid: int = None,
                        cache: bool = True) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param doubanid: 豆瓣ID
        :param bangumiid: BangumiID
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """
        # 识别用名中含指定信息情形
        if not mtype and meta and meta.type in [MediaType.TV, MediaType.MOVIE]:
            mtype = meta.type
        if not tmdbid and hasattr(meta, "tmdbid"):
            tmdbid = meta.tmdbid
        if not doubanid and hasattr(meta, "doubanid"):
            doubanid = meta.doubanid
        # 有tmdbid时不使用其它ID
        if tmdbid:
            doubanid = None
            bangumiid = None
        return self.run_module("recognize_media", meta=meta, mtype=mtype,
                               tmdbid=tmdbid, doubanid=doubanid, bangumiid=bangumiid, cache=cache)

    def match_doubaninfo(self, name: str, imdbid: str = None,
                         mtype: MediaType = None, year: str = None, season: int = None,
                         raise_exception: bool = False) -> Optional[dict]:
        """
        搜索和匹配豆瓣信息
        :param name: 标题
        :param imdbid: imdbid
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return self.run_module("match_doubaninfo", name=name, imdbid=imdbid,
                               mtype=mtype, year=year, season=season, raise_exception=raise_exception)

    def match_tmdbinfo(self, name: str, mtype: MediaType = None,
                       year: str = None, season: int = None) -> Optional[dict]:
        """
        搜索和匹配TMDB信息
        :param name: 标题
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        """
        return self.run_module("match_tmdbinfo", name=name,
                               mtype=mtype, year=year, season=season)

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        return self.run_module("obtain_images", mediainfo=mediainfo)

    def obtain_specific_image(self, mediaid: Union[str, int], mtype: MediaType,
                              image_type: MediaImageType, image_prefix: str = None,
                              season: int = None, episode: int = None) -> Optional[str]:
        """
        获取指定媒体信息图片，返回图片地址
        :param mediaid:     媒体ID
        :param mtype:       媒体类型
        :param image_type:  图片类型
        :param image_prefix: 图片前缀
        :param season:      季
        :param episode:     集
        """
        return self.run_module("obtain_specific_image", mediaid=mediaid, mtype=mtype,
                               image_prefix=image_prefix, image_type=image_type,
                               season=season, episode=episode)

    def douban_info(self, doubanid: str, mtype: MediaType = None, raise_exception: bool = False) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :return: 豆瓣信息
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return self.run_module("douban_info", doubanid=doubanid, mtype=mtype, raise_exception=raise_exception)

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: TVDB信息
        """
        return self.run_module("tvdb_info", tvdbid=tvdbid)

    def tmdb_info(self, tmdbid: int, mtype: MediaType, season: int = None) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季
        :return: TVDB信息
        """
        return self.run_module("tmdb_info", tmdbid=tmdbid, mtype=mtype, season=season)

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: int
        :return: Bangumi信息
        """
        return self.run_module("bangumi_info", bangumiid=bangumiid)

    def message_parser(self, body: Any, form: Any,
                       args: Any) -> Optional[CommingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 消息渠道、消息内容
        """
        return self.run_module("message_parser", body=body, form=form, args=args)

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[WebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.run_module("webhook_parser", body=body, form=form, args=args)

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息列表
        """
        return self.run_module("search_medias", meta=meta)

    def search_persons(self, name: str) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        :param name:  人物名称
        """
        return self.run_module("search_persons", name=name)

    def search_torrents(self, site: CommentedMap,
                        keywords: List[str],
                        mtype: MediaType = None,
                        page: int = 0) -> List[TorrentInfo]:
        """
        搜索一个站点的种子资源
        :param site:  站点
        :param keywords:  搜索关键词列表
        :param mtype:  媒体类型
        :param page:  页码
        :reutrn: 资源列表
        """
        return self.run_module("search_torrents", site=site, keywords=keywords,
                               mtype=mtype, page=page)

    def refresh_torrents(self, site: CommentedMap) -> List[TorrentInfo]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :reutrn: 种子资源列表
        """
        return self.run_module("refresh_torrents", site=site)

    def filter_torrents(self, rule_string: str,
                        torrent_list: List[TorrentInfo],
                        season_episodes: Dict[int, list] = None,
                        mediainfo: MediaInfo = None) -> List[TorrentInfo]:
        """
        过滤种子资源
        :param rule_string:  过滤规则
        :param torrent_list:  资源列表
        :param season_episodes:  季集数过滤 {season:[episodes]}
        :param mediainfo:  识别的媒体信息
        :return: 过滤后的资源列表，添加资源优先级
        """
        return self.run_module("filter_torrents", rule_string=rule_string,
                               torrent_list=torrent_list, season_episodes=season_episodes,
                               mediainfo=mediainfo)

    def download(self, content: Union[Path, str], download_dir: Path, cookie: str,
                 episodes: Set[int] = None, category: str = None,
                 downloader: str = settings.DEFAULT_DOWNLOADER
                 ) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  种子分类
        :param downloader:  下载器
        :return: 种子Hash，错误信息
        """
        return self.run_module("download", content=content, download_dir=download_dir,
                               cookie=cookie, episodes=episodes, category=category,
                               downloader=downloader)

    def download_added(self, context: Context, download_dir: Path, torrent_path: Path = None) -> None:
        """
        添加下载任务成功后，从站点下载字幕，保存到下载目录
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param download_dir:  下载目录
        :param torrent_path:  种子文件地址
        :return: None，该方法可被多个模块同时处理
        """
        return self.run_module("download_added", context=context, torrent_path=torrent_path,
                               download_dir=download_dir)

    def list_torrents(self, status: TorrentStatus = None,
                      hashs: Union[list, str] = None,
                      downloader: str = settings.DEFAULT_DOWNLOADER
                      ) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: 下载器中符合状态的种子列表
        """
        return self.run_module("list_torrents", status=status, hashs=hashs, downloader=downloader)

    def transfer(self, path: Path, meta: MetaBase, mediainfo: MediaInfo,
                 transfer_type: str, target: Path = None,
                 episodes_info: List[TmdbEpisode] = None,
                 scrape: bool = None) -> Optional[TransferInfo]:
        """
        文件转移
        :param path:  文件路径
        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        :param transfer_type:  转移模式
        :param target:  转移目标路径
        :param episodes_info: 当前季的全部集信息
        :param scrape: 是否刮削元数据
        :return: {path, target_path, message}
        """
        return self.run_module("transfer", path=path, meta=meta, mediainfo=mediainfo,
                               transfer_type=transfer_type, target=target, episodes_info=episodes_info,
                               scrape=scrape)

    def transfer_completed(self, hashs: str, path: Path = None,
                           downloader: str = settings.DEFAULT_DOWNLOADER) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param path:  源目录
        :param downloader:  下载器
        """
        return self.run_module("transfer_completed", hashs=hashs, path=path, downloader=downloader)

    def remove_torrents(self, hashs: Union[str, list], delete_file: bool = True,
                        downloader: str = settings.DEFAULT_DOWNLOADER) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file: 是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("remove_torrents", hashs=hashs, delete_file=delete_file, downloader=downloader)

    def start_torrents(self, hashs: Union[list, str], downloader: str = settings.DEFAULT_DOWNLOADER) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("start_torrents", hashs=hashs, downloader=downloader)

    def stop_torrents(self, hashs: Union[list, str], downloader: str = settings.DEFAULT_DOWNLOADER) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("stop_torrents", hashs=hashs, downloader=downloader)

    def torrent_files(self, tid: str,
                      downloader: str = settings.DEFAULT_DOWNLOADER) -> Optional[Union[TorrentFilesList, List[File]]]:
        """
        获取种子文件
        :param tid:  种子Hash
        :param downloader:  下载器
        :return: 种子文件
        """
        return self.run_module("torrent_files", tid=tid, downloader=downloader)

    def media_exists(self, mediainfo: MediaInfo, itemid: str = None) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        return self.run_module("media_exists", mediainfo=mediainfo, itemid=itemid)

    def post_message(self, message: Notification) -> None:
        """
        发送消息
        :param message:  消息体
        :return: 成功或失败
        """
        logger.info(f"发送消息：channel={message.channel}，"
                    f"title={message.title}, "
                    f"text={message.text}，"
                    f"userid={message.userid}")
        # 发送事件
        self.eventmanager.send_event(etype=EventType.NoticeMessage,
                                     data={
                                         "channel": message.channel,
                                         "type": message.mtype,
                                         "title": message.title,
                                         "text": message.text,
                                         "image": message.image,
                                         "userid": message.userid,
                                     })
        # 保存消息
        self.messagehelper.put(message, role="user")
        self.messageoper.add(channel=message.channel, mtype=message.mtype,
                             title=message.title, text=message.text,
                             image=message.image, link=message.link,
                             userid=message.userid, action=1)
        # 发送
        self.run_module("post_message", message=message)

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param message:  消息体
        :param medias:  媒体列表
        :return: 成功或失败
        """
        note_list = [media.to_dict() for media in medias]
        self.messagehelper.put(message, role="user", note=note_list)
        self.messageoper.add(channel=message.channel, mtype=message.mtype,
                             title=message.title, text=message.text,
                             image=message.image, link=message.link,
                             userid=message.userid, action=1,
                             note=note_list)
        return self.run_module("post_medias_message", message=message, medias=medias)

    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param message:  消息体
        :param torrents:  种子列表
        :return: 成功或失败
        """
        note_list = [torrent.torrent_info.to_dict() for torrent in torrents]
        self.messagehelper.put(message, role="user", note=note_list)
        self.messageoper.add(channel=message.channel, mtype=message.mtype,
                             title=message.title, text=message.text,
                             image=message.image, link=message.link,
                             userid=message.userid, action=1,
                             note=note_list)
        return self.run_module("post_torrents_message", message=message, torrents=torrents)

    def scrape_metadata(self, path: Path, mediainfo: MediaInfo, transfer_type: str,
                        metainfo: MetaBase = None, force_nfo: bool = False, force_img: bool = False) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :param metainfo: 源文件的识别元数据
        :param transfer_type:  转移模式
        :param force_nfo:  强制刮削nfo
        :param force_img:  强制刮削图片
        :return: 成功或失败
        """
        self.run_module("scrape_metadata", path=path, mediainfo=mediainfo, metainfo=metainfo,
                        transfer_type=transfer_type, force_nfo=force_nfo, force_img=force_img)

    def metadata_img(self, mediainfo: MediaInfo, season: int = None) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        """
        return self.run_module("metadata_img", mediainfo=mediainfo, season=season)

    def media_category(self) -> Optional[Dict[str, list]]:
        """
        获取媒体分类
        :return: 获取二级分类配置字典项，需包括电影、电视剧
        """
        return self.run_module("media_category")

    def register_commands(self, commands: Dict[str, dict]) -> None:
        """
        注册菜单命令
        """
        self.run_module("register_commands", commands=commands)

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次，模块实现该接口以实现定时服务
        """
        self.run_module("scheduler_job")

    def clear_cache(self) -> None:
        """
        清理缓存，模块实现该接口响应清理缓存事件
        """
        self.run_module("clear_cache")
