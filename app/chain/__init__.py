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
from app.core.context import Context
from app.core.context import MediaInfo, TorrentInfo
from app.core.event import EventManager
from app.core.meta import MetaBase
from app.core.module import ModuleManager
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent, ExistMediaInfo, DownloadingTorrent, CommingMessage, Notification, \
    WebhookEventInfo
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
                logger.error(f"加载缓存 {filename} 出错：{err}")
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
            logger.error(f"保存缓存 {filename} 出错：{err}")
        finally:
            # 主动资源回收
            del cache
            gc.collect()

    def run_module(self, method: str, *args, **kwargs) -> Any:
        """
        运行包含该方法的所有模块，然后返回结果
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
        modules = self.modulemanager.get_modules(method)
        for module in modules:
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
                    # 返回结果非列表也非空，则继续执行下一模块
                    continue
            except Exception as err:
                logger.error(f"运行模块 {method} 出错：{module.__class__.__name__} - {err}\n{traceback.print_exc()}")
        return result

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        tmdbid: int = None) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :return: 识别的媒体信息，包括剧集信息
        """
        return self.run_module("recognize_media", meta=meta, mtype=mtype, tmdbid=tmdbid)

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

    def douban_info(self, doubanid: str) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :return: 豆瓣信息
        """
        return self.run_module("douban_info", doubanid=doubanid)

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: TVDB信息
        """
        return self.run_module("tvdb_info", tvdbid=tvdbid)

    def tmdb_info(self, tmdbid: int, mtype: MediaType) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :return: TVDB信息
        """
        return self.run_module("tmdb_info", tmdbid=tmdbid, mtype=mtype)

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

    def search_torrents(self, site: CommentedMap,
                        mediainfo: Optional[MediaInfo] = None,
                        keyword: str = None,
                        page: int = 0) -> List[TorrentInfo]:
        """
        搜索一个站点的种子资源
        :param site:  站点
        :param mediainfo:  识别的媒体信息
        :param keyword:  搜索关键词，如有按关键词搜索，否则按媒体信息名称搜索
        :param page:  页码
        :reutrn: 资源列表
        """
        return self.run_module("search_torrents", mediainfo=mediainfo, site=site,
                               keyword=keyword, page=page)

    def refresh_torrents(self, site: CommentedMap) -> List[TorrentInfo]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :reutrn: 种子资源列表
        """
        return self.run_module("refresh_torrents", site=site)

    def filter_torrents(self, rule_string: str,
                        torrent_list: List[TorrentInfo],
                        season_episodes: Dict[int, list] = None) -> List[TorrentInfo]:
        """
        过滤种子资源
        :param rule_string:  过滤规则
        :param torrent_list:  资源列表
        :param season_episodes:  季集数过滤 {season:[episodes]}
        :return: 过滤后的资源列表，添加资源优先级
        """
        return self.run_module("filter_torrents", rule_string=rule_string,
                               torrent_list=torrent_list, season_episodes=season_episodes)

    def download(self, torrent_path: Path, download_dir: Path, cookie: str,
                 episodes: Set[int] = None,
                 ) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param torrent_path:  种子文件地址
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :return: 种子Hash，错误信息
        """
        return self.run_module("download", torrent_path=torrent_path, download_dir=download_dir,
                               cookie=cookie, episodes=episodes, )

    def download_added(self, context: Context, torrent_path: Path, download_dir: Path) -> None:
        """
        添加下载任务成功后，从站点下载字幕，保存到下载目录
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param torrent_path:  种子文件地址
        :param download_dir:  下载目录
        :return: None，该方法可被多个模块同时处理
        """
        if settings.DOWNLOAD_SUBTITLE:
            return self.run_module("download_added", context=context, torrent_path=torrent_path,
                                   download_dir=download_dir)
        return None

    def list_torrents(self, status: TorrentStatus = None,
                      hashs: Union[list, str] = None) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :return: 下载器中符合状态的种子列表
        """
        return self.run_module("list_torrents", status=status, hashs=hashs)

    def transfer(self, path: Path, mediainfo: MediaInfo, transfer_type: str) -> Optional[TransferInfo]:
        """
        文件转移
        :param path:  文件路径
        :param mediainfo:  识别的媒体信息
        :param transfer_type:  转移模式
        :return: {path, target_path, message}
        """
        return self.run_module("transfer", path=path, mediainfo=mediainfo, transfer_type=transfer_type)

    def transfer_completed(self, hashs: Union[str, list], transinfo: TransferInfo) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param transinfo:  转移信息
        """
        return self.run_module("transfer_completed", hashs=hashs, transinfo=transinfo)

    def remove_torrents(self, hashs: Union[str, list]) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :return: bool
        """
        return self.run_module("remove_torrents", hashs=hashs)

    def start_torrents(self, hashs: Union[list, str]) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :return: bool
        """
        return self.run_module("start_torrents", hashs=hashs)

    def stop_torrents(self, hashs: Union[list, str]) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :return: bool
        """
        return self.run_module("stop_torrents", hashs=hashs)

    def torrent_files(self, tid: str) -> Optional[Union[TorrentFilesList, List[File]]]:
        """
        根据种子文件，选择并添加下载任务
        :param tid:  种子Hash
        :return: 种子文件
        """
        return self.run_module("torrent_files", tid=tid)

    def media_exists(self, mediainfo: MediaInfo, itemid: List[str] = []) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID列表
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        return self.run_module("media_exists", mediainfo=mediainfo, itemid=itemid)

    def refresh_mediaserver(self, mediainfo: MediaInfo, file_path: Path) -> Optional[bool]:
        """
        刷新媒体库
        :param mediainfo:  识别的媒体信息
        :param file_path:  文件路径
        :return: 成功或失败
        """
        if settings.REFRESH_MEDIASERVER:
            return self.run_module("refresh_mediaserver", mediainfo=mediainfo, file_path=file_path)
        return None

    def post_message(self, message: Notification) -> Optional[bool]:
        """
        发送消息
        :param message:  消息体
        :return: 成功或失败
        """
        # 发送事件
        self.eventmanager.send_event(etype=EventType.NoticeMessage,
                                     data={
                                         "channel": message.channel,
                                         "title": message.title,
                                         "text": message.text,
                                         "image": message.image,
                                         "userid": message.userid,
                                     })
        return self.run_module("post_message", message=message)

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param message:  消息体
        :param medias:  媒体列表
        :return: 成功或失败
        """
        return self.run_module("post_medias_message", message=message, medias=medias)

    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param message:  消息体
        :param torrents:  种子列表
        :return: 成功或失败
        """
        return self.run_module("post_torrents_message", message=message, torrents=torrents)

    def scrape_metadata(self, path: Path, mediainfo: MediaInfo) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        if settings.SCRAP_METADATA:
            return self.run_module("scrape_metadata", path=path, mediainfo=mediainfo)
        return None

    def register_commands(self, commands: dict) -> None:
        """
        注册菜单命令
        """
        return self.run_module("register_commands", commands=commands)

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次，模块实现该接口以实现定时服务
        """
        return self.run_module("scheduler_job")
