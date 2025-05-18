import copy
import gc
import pickle
import traceback
from abc import ABCMeta
from collections.abc import Callable
from pathlib import Path
from typing import Optional, Any, Tuple, List, Set, Union, Dict

from qbittorrentapi import TorrentFilesList
from transmission_rpc import File

from app.core.config import settings
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.event import EventManager
from app.core.meta import MetaBase
from app.core.module import ModuleManager
from app.core.plugin import PluginManager
from app.db.message_oper import MessageOper
from app.db.user_oper import UserOper
from app.helper.message import MessageHelper, MessageQueueManager, MessageTemplateHelper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent, ExistMediaInfo, DownloadingTorrent, CommingMessage, Notification, \
    WebhookEventInfo, TmdbEpisode, MediaPerson, FileItem, TransferDirectoryConf
from app.schemas.types import TorrentStatus, MediaType, MediaImageType, EventType, ModuleExecutionType
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
        self.messagequeue = MessageQueueManager(
            send_callback=self.run_module
        )
        self.useroper = UserOper()
        self.pluginmanager = PluginManager()

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
                pickle.dump(cache, f)  # noqa
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
            cache_path.unlink()

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
                return ret is None

        def run_plugin_modules(_plugin_modules, _module_type):
            _result = None
            for plugin, module_dict in plugin_modules.items():
                plugin_id, plugin_name = plugin
                if method in module_dict:
                    method_info = module_dict[method]
                    # 兼容旧的返回值
                    if isinstance(method_info, tuple) and len(method_info) == 2:
                        # 类型为 (self.xxx1, ModuleExecutionType)
                        _func = method_info[0]
                        module_execution_type = method_info[1]
                    else:
                        # 类型为 self.xxx1
                        _func = method_info
                        module_execution_type = ModuleExecutionType.Hijack
                    if _func and module_execution_type == _module_type:
                        try:
                            logger.info(f"请求插件 {plugin_name} 执行：{method} ({_module_type.value})...")
                            if is_result_empty(_result):
                                # 返回None，第一次执行或者需继续执行下一模块
                                _result = _func(*args, **kwargs)
                            elif isinstance(_result, list):
                                # 返回为列表，有多个模块运行结果时进行合并
                                _temp = _func(*args, **kwargs)
                                if isinstance(_temp, list):
                                    result.extend(_temp)
                            else:
                                break
                        except Exception as _err:
                            if kwargs.get("raise_exception"):
                                raise
                            logger.error(
                                f"运行插件 {plugin_id} 模块 {method} 出错：{str(_err)}\n{traceback.format_exc()}")
                            self.messagehelper.put(title=f"{plugin_name} 发生了错误",
                                                   message=str(err),
                                                   role="plugin")
                            self.eventmanager.send_event(
                                EventType.SystemError,
                                {
                                    "type": "plugin",
                                    "plugin_id": plugin_id,
                                    "plugin_name": plugin_name,
                                    "plugin_method": method,
                                    "error": str(err),
                                    "traceback": traceback.format_exc()
                                }
                            )
            return _result

        plugin_modules = self.pluginmanager.get_plugin_modules()
        # 劫持类型插件模块
        result = run_plugin_modules(plugin_modules, ModuleExecutionType.Hijack)
        if not is_result_empty(result) and not isinstance(result, list):
            # 插件模块返回结果不为空且不是列表，直接返回
            return result

        # 系统模块
        logger.debug(f"请求系统模块执行：{method} ...")
        modules = self.modulemanager.get_running_modules(method)
        # 按优先级排序
        modules = sorted(modules, key=lambda x: x.get_priority())
        for module in modules:
            module_id = module.__class__.__name__
            try:
                module_name = module.get_name()
            except Exception as err:
                logger.debug(f"获取模块名称出错：{str(err)}")
                module_name = module_id
            try:
                func = getattr(module, method)
                if is_result_empty(result):
                    # 返回None，第一次执行或者需继续执行下一模块
                    result = func(*args, **kwargs)
                elif ObjectUtils.check_signature(func, result):
                    # 返回结果与方法签名一致，将结果传入
                    result = func(result)
                elif isinstance(result, list):
                    # 返回为列表，有多个模块运行结果时进行合并
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
        if not is_result_empty(result) and not isinstance(result, list):
            # 插件模块返回结果不为空且不是列表，直接返回
            return result
        # 补充类型插件模块
        temp = run_plugin_modules(plugin_modules, ModuleExecutionType.Supplement)
        if isinstance(result, list):
            # 返回为列表，有多个模块运行结果时进行合并
            if isinstance(temp, list):
                result.extend(temp)
        else:
            return temp
        return result

    def recognize_media(self, meta: MetaBase = None,
                        mtype: Optional[MediaType] = None,
                        tmdbid: Optional[int] = None,
                        doubanid: Optional[str] = None,
                        bangumiid: Optional[int] = None,
                        episode_group: Optional[str] = None,
                        cache: bool = True) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param doubanid: 豆瓣ID
        :param bangumiid: BangumiID
        :param episode_group: 剧集组
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
                               tmdbid=tmdbid, doubanid=doubanid, bangumiid=bangumiid,
                               episode_group=episode_group, cache=cache)

    def match_doubaninfo(self, name: str, imdbid: Optional[str] = None,
                         mtype: Optional[MediaType] = None, year: Optional[str] = None, season: Optional[int] = None,
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

    def match_tmdbinfo(self, name: str, mtype: Optional[MediaType] = None,
                       year: Optional[str] = None, season: Optional[int] = None) -> Optional[dict]:
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
                              image_type: MediaImageType, image_prefix: Optional[str] = None,
                              season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
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

    def douban_info(self, doubanid: str, mtype: Optional[MediaType] = None,
                    raise_exception: bool = False) -> Optional[dict]:
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

    def tmdb_info(self, tmdbid: int, mtype: MediaType, season: Optional[int] = None) -> Optional[dict]:
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

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[CommingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param source: 消息来源（渠道配置名称）
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 消息渠道、消息内容
        """
        return self.run_module("message_parser", source=source, body=body, form=form, args=args)

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

    def search_collections(self, name: str) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息
        :param name:  集合名称
        """
        return self.run_module("search_collections", name=name)

    def search_torrents(self, site: dict,
                        keywords: List[str],
                        mtype: Optional[MediaType] = None,
                        page: Optional[int] = 0) -> List[TorrentInfo]:
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

    def refresh_torrents(self, site: dict, keyword: Optional[str] = None,
                         cat: Optional[str] = None, page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  标题
        :param cat:  分类
        :param page:  页码
        :reutrn: 种子资源列表
        """
        return self.run_module("refresh_torrents", site=site, keyword=keyword, cat=cat, page=page)

    def filter_torrents(self, rule_groups: List[str],
                        torrent_list: List[TorrentInfo],
                        mediainfo: MediaInfo = None) -> List[TorrentInfo]:
        """
        过滤种子资源
        :param rule_groups:  过滤规则组名称列表
        :param torrent_list:  资源列表
        :param mediainfo:  识别的媒体信息
        :return: 过滤后的资源列表，添加资源优先级
        """
        return self.run_module("filter_torrents", rule_groups=rule_groups,
                               torrent_list=torrent_list, mediainfo=mediainfo)

    def download(self, content: Union[Path, str], download_dir: Path, cookie: str,
                 episodes: Set[int] = None, category: Optional[str] = None, label: Optional[str] = None,
                 downloader: Optional[str] = None
                 ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  种子分类
        :param label:  标签
        :param downloader:  下载器
        :return: 下载器名称、种子Hash、种子文件布局、错误原因
        """
        return self.run_module("download", content=content, download_dir=download_dir,
                               cookie=cookie, episodes=episodes, category=category, label=label,
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
                      downloader: Optional[str] = None
                      ) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: 下载器中符合状态的种子列表
        """
        return self.run_module("list_torrents", status=status, hashs=hashs, downloader=downloader)

    def transfer(self, fileitem: FileItem, meta: MetaBase, mediainfo: MediaInfo,
                 target_directory: TransferDirectoryConf = None,
                 target_storage: Optional[str] = None, target_path: Path = None,
                 transfer_type: Optional[str] = None, scrape: bool = None,
                 library_type_folder: bool = None, library_category_folder: bool = None,
                 episodes_info: List[TmdbEpisode] = None,
                 source_oper: Callable = None, target_oper: Callable = None) -> Optional[TransferInfo]:
        """
        文件转移
        :param fileitem:  文件信息
        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        :param target_directory:  目标目录配置
        :param target_storage:  目标存储
        :param target_path:  目标路径
        :param transfer_type:  转移模式
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型创建目录
        :param library_category_folder: 是否按类别创建目录
        :param episodes_info: 当前季的全部集信息
        :param source_oper:  源存储操作类
        :param target_oper:  目标存储操作类
        :return: {path, target_path, message}
        """
        return self.run_module("transfer",
                               fileitem=fileitem, meta=meta, mediainfo=mediainfo,
                               target_directory=target_directory,
                               target_path=target_path, target_storage=target_storage,
                               transfer_type=transfer_type, scrape=scrape,
                               library_type_folder=library_type_folder,
                               library_category_folder=library_category_folder,
                               episodes_info=episodes_info,
                               source_oper=source_oper, target_oper=target_oper)

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        下载器转移完成后的处理
        :param hashs:  种子Hash
        :param downloader:  下载器
        """
        return self.run_module("transfer_completed", hashs=hashs, downloader=downloader)

    def remove_torrents(self, hashs: Union[str, list], delete_file: bool = True,
                        downloader: Optional[str] = None) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file: 是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("remove_torrents", hashs=hashs, delete_file=delete_file, downloader=downloader)

    def start_torrents(self, hashs: Union[list, str], downloader: Optional[str] = None) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("start_torrents", hashs=hashs, downloader=downloader)

    def stop_torrents(self, hashs: Union[list, str], downloader: Optional[str] = None) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("stop_torrents", hashs=hashs, downloader=downloader)

    def torrent_files(self, tid: str,
                      downloader: Optional[str] = None) -> Optional[Union[TorrentFilesList, List[File]]]:
        """
        获取种子文件
        :param tid:  种子Hash
        :param downloader:  下载器
        :return: 种子文件
        """
        return self.run_module("torrent_files", tid=tid, downloader=downloader)

    def media_exists(self, mediainfo: MediaInfo, itemid: Optional[str] = None,
                     server: Optional[str] = None) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :param server:  媒体服务器
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        return self.run_module("media_exists", mediainfo=mediainfo, itemid=itemid, server=server)

    def media_files(self, mediainfo: MediaInfo) -> Optional[List[FileItem]]:
        """
        获取媒体文件清单
        :param mediainfo:  识别的媒体信息
        :return: 媒体文件列表
        """
        return self.run_module("media_files", mediainfo=mediainfo)

    def post_message(self,
                     message: Optional[Notification] = None,
                     meta: Optional[MetaBase] = None,
                     mediainfo: Optional[MediaInfo] = None,
                     torrentinfo: Optional[TorrentInfo] = None,
                     transferinfo: Optional[TransferInfo] = None,
                     **kwargs) -> None:
        """
        发送消息
        :param message:  Notification实例
        :param meta:  元数据
        :param mediainfo:  媒体信息
        :param torrentinfo:  种子信息
        :param transferinfo:  文件整理信息
        :param kwargs:  其他参数(覆盖业务对象属性值)
        :return: 成功或失败
        """
        # 渲染消息
        message = MessageTemplateHelper.render(message=message, meta=meta, mediainfo=mediainfo,
                                               torrentinfo=torrentinfo, transferinfo=transferinfo, **kwargs)
        # 保存消息
        self.messagehelper.put(message, role="user", title=message.title)
        self.messageoper.add(**message.dict())
        # 发送消息按设置隔离
        if not message.userid and message.mtype:
            # 消息隔离设置
            notify_action = ServiceConfigHelper.get_notification_switch(message.mtype)
            if notify_action:
                # 'admin' 'user,admin' 'user' 'all'
                actions = notify_action.split(",")
                # 是否已发送管理员标志
                admin_sended = False
                send_orignal = False
                for action in actions:
                    send_message = copy.deepcopy(message)
                    if action == "admin" and not admin_sended:
                        # 仅发送管理员
                        logger.info(f"{send_message.mtype} 的消息已设置发送给管理员")
                        # 读取管理员消息IDS
                        send_message.targets = self.useroper.get_settings(settings.SUPERUSER)
                        admin_sended = True
                    elif action == "user" and send_message.username:
                        # 发送对应用户
                        logger.info(f"{send_message.mtype} 的消息已设置发送给用户 {send_message.username}")
                        # 读取用户消息IDS
                        send_message.targets = self.useroper.get_settings(send_message.username)
                        if send_message.targets is None:
                            # 没有找到用户
                            if not admin_sended:
                                # 回滚发送管理员
                                logger.info(f"用户 {send_message.username} 不存在，消息将发送给管理员")
                                # 读取管理员消息IDS
                                send_message.targets = self.useroper.get_settings(settings.SUPERUSER)
                                admin_sended = True
                            else:
                                # 管理员发过了，此消息不发了
                                logger.info(f"用户 {send_message.username} 不存在，消息无法发送到对应用户")
                                continue
                        elif send_message.username == settings.SUPERUSER:
                            # 管理员同名已发送
                            admin_sended = True
                    else:
                        # 按原消息发送全体
                        if not admin_sended:
                            send_orignal = True
                        break
                    # 按设定发送
                    self.eventmanager.send_event(etype=EventType.NoticeMessage,
                                                 data={**send_message.dict(), "type": send_message.mtype})
                    self.messagequeue.send_message("post_message", message=send_message)
                if not send_orignal:
                    return
        # 发送消息事件
        self.eventmanager.send_event(etype=EventType.NoticeMessage, data={**message.dict(), "type": message.mtype})
        # 按原消息发送
        self.messagequeue.send_message("post_message", message=message)

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> None:
        """
        发送媒体信息选择列表
        :param message:  消息体
        :param medias:  媒体列表
        :return: 成功或失败
        """
        note_list = [media.to_dict() for media in medias]
        self.messagehelper.put(message, role="user", note=note_list, title=message.title)
        self.messageoper.add(**message.dict(), note=note_list)
        return self.messagequeue.send_message("post_medias_message", message=message, medias=medias)

    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> None:
        """
        发送种子信息选择列表
        :param message:  消息体
        :param torrents:  种子列表
        :return: 成功或失败
        """
        note_list = [torrent.torrent_info.to_dict() for torrent in torrents]
        self.messagehelper.put(message, role="user", note=note_list, title=message.title)
        self.messageoper.add(**message.dict(), note=note_list)
        return self.messagequeue.send_message("post_torrents_message", message=message, torrents=torrents)

    def metadata_img(self, mediainfo: MediaInfo,
                     season: Optional[int] = None, episode: Optional[int] = None) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        return self.run_module("metadata_img", mediainfo=mediainfo, season=season, episode=episode)

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
