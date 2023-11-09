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
    WebhookEventInfo, TmdbEpisode
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
                    # 中止继续执行
                    break
            except Exception as err:
                logger.error(
                    f"运行模块 {method} 出错：{module.__class__.__name__} - {str(err)}\n{traceback.print_exc()}")
        return result

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        tmdbid: int = None,
                        doubanid: str = None) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param doubanid: 豆瓣ID
        :return: 识别的媒体信息，包括剧集信息
        """
        # 识别用名中含指定信息情形
        if not mtype and meta and meta.type in [MediaType.TV, MediaType.MOVIE]:
            mtype = meta.type
        if not tmdbid and hasattr(meta, "tmdbid"):
            tmdbid = meta.tmdbid
        if not doubanid and hasattr(meta, "doubanid"):
            doubanid = meta.doubanid
        return self.run_module("recognize_media", meta=meta, mtype=mtype,
                               tmdbid=tmdbid, doubanid=doubanid)

    def match_doubaninfo(self, name: str, imdbid: str = None,
                         mtype: MediaType = None, year: str = None, season: int = None) -> Optional[dict]:
        """
        搜索和匹配豆瓣信息
        :param name: 标题
        :param imdbid: imdbid
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        """
        return self.run_module("match_doubaninfo", name=name, imdbid=imdbid,
                               mtype=mtype, year=year, season=season)

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

    def douban_info(self, doubanid: str, mtype: MediaType = None) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :return: 豆瓣信息
        """
        return self.run_module("douban_info", doubanid=doubanid, mtype=mtype)

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
                 episodes: Set[int] = None, category: str = None
                 ) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  种子分类
        :return: 种子Hash，错误信息
        """
        return self.run_module("download", content=content, download_dir=download_dir,
                               cookie=cookie, episodes=episodes, category=category)

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
                      hashs: Union[list, str] = None) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :return: 下载器中符合状态的种子列表
        """
        return self.run_module("list_torrents", status=status, hashs=hashs)

    def transfer(self, path: Path, meta: MetaBase, mediainfo: MediaInfo,
                 transfer_type: str, target: Path = None,
                 episodes_info: List[TmdbEpisode] = None) -> Optional[TransferInfo]:
        """
        文件转移
        :param path:  文件路径
        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        :param transfer_type:  转移模式
        :param target:  转移目标路径
        :param episodes_info: 当前季的全部集信息
        :return: {path, target_path, message}
        """
        return self.run_module("transfer", path=path, meta=meta, mediainfo=mediainfo,
                               transfer_type=transfer_type, target=target,
                               episodes_info=episodes_info)

    def transfer_completed(self, hashs: Union[str, list], path: Path = None) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param path:  源目录
        """
        return self.run_module("transfer_completed", hashs=hashs, path=path)

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
        获取种子文件
        :param tid:  种子Hash
        :return: 种子文件
        """
        return self.run_module("torrent_files", tid=tid)

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
        logger.info(f"发送消息：channel={message.channel}，"
                    f"title={message.title}, "
                    f"text={message.text}，"
                    f"userid={message.userid}")
        self.run_module("post_message", message=message)

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

    def scrape_metadata(self, path: Path, mediainfo: MediaInfo, transfer_type: str) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :param transfer_type:  转移模式
        :return: 成功或失败
        """
        self.run_module("scrape_metadata", path=path, mediainfo=mediainfo, transfer_type=transfer_type)

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
