from abc import abstractmethod, ABCMeta
from pathlib import Path
from typing import Optional, List, Tuple, Union, Set

from fastapi import Request

from app.core.context import MediaInfo, TorrentInfo
from app.core.meta import MetaBase
from app.utils.types import TorrentStatus


class _ModuleBase(metaclass=ABCMeta):
    """
    模块基类，实现对应方法，在有需要时会被自动调用，返回None代表不启用该模块
    输入参数与输出参数一致的，可以被多个模块重复实现
    通过监听事件来实现多个模块之间的协作
    """

    @abstractmethod
    def init_module(self) -> None:
        """
        模块初始化
        """
        pass

    @abstractmethod
    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        模块开关设置，返回开关名和开关值，开关值为True时代表有值即打开，不实现该方法或返回None代表不使用开关
        """
        pass

    def prepare_recognize(self, title: str,
                          subtitle: str = None) -> Tuple[str, str]:
        """
        识别前的预处理
        :param title:     标题
        :param subtitle:  副标题
        :return: 处理后的标题、副标题，注意如果返回None，有可能是没有对应的处理模块，应无视结果
        """
        pass

    def recognize_media(self, meta: MetaBase,
                        tmdbid: str = None) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param tmdbid:   tmdbid
        :return: 识别的媒体信息，包括剧集信息
        """
        pass

    def douban_info(self, doubanid: str) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :return: 识别的媒体信息，包括剧集信息
        """
        pass

    def message_parser(self, request: Request) -> Optional[dict]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param request:  请求体
        :return: 消息内容、用户ID
        """
        pass

    def webhook_parser(self, message: dict) -> Optional[dict]:
        """
        解析Webhook报文体
        :param message:  请求体
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        pass

    def obtain_image(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息，注意如果返回None，有可能是没有对应的处理模块，应无视结果
        """
        pass

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息
        """
        pass

    def search_torrents(self, mediainfo: Optional[MediaInfo], sites: List[dict],
                        keyword: str = None) -> Optional[List[TorrentInfo]]:
        """
        搜索站点，多个站点需要多线程处理
        :param mediainfo:  识别的媒体信息
        :param sites:  站点列表
        :param keyword:  搜索关键词，如有按关键词搜索，否则按媒体信息名称搜索
        :reutrn: 资源列表
        """
        pass

    def refresh_torrents(self, sites: List[dict]) -> Optional[List[TorrentInfo]]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param sites:  站点列表
        :reutrn: 种子资源列表
        """
        pass

    def filter_torrents(self, torrent_list: List[TorrentInfo]) -> List[TorrentInfo]:
        """
        过滤资源
        :param torrent_list:  资源列表
        :return: 过滤后的资源列表，注意如果返回None，有可能是没有对应的处理模块，应无视结果
        """
        pass

    def download(self, torrent_path: Path, cookie: str,
                 episodes: Set[int] = None) -> Optional[Tuple[Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param torrent_path:  种子文件地址
        :param cookie:  站点Cookie
        :param episodes:  需要下载的集数
        :return: 种子Hash
        """
        pass

    def list_torrents(self, status: TorrentStatus) -> Optional[List[dict]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :return: 下载器中符合状态的种子列表
        """
        pass

    def remove_torrents(self, hashs: Union[str, list]) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :return: bool
        """
        pass

    def transfer(self, path: str, mediainfo: MediaInfo) -> Optional[str]:
        """
        转移一个路径下的文件
        :param path:  文件路径
        :param mediainfo:  识别的媒体信息
        :return: 转移后的目录或None代表失败
        """
        pass

    def media_exists(self, mediainfo: MediaInfo) -> Optional[dict]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        pass

    def refresh_mediaserver(self, mediainfo: MediaInfo, file_path: str) -> Optional[bool]:
        """
        刷新媒体库
        :param mediainfo:  识别的媒体信息
        :param file_path:  文件路径
        :return: 成功或失败
        """
        pass

    def post_message(self, title: str,
                     text: str = None, image: str = None, userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送消息
        :param title:  标题
        :param text: 内容
        :param image: 图片
        :param userid:  用户ID
        :return: 成功或失败
        """
        pass

    def post_medias_message(self, title: str, items: List[MediaInfo],
                            userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param title:  标题
        :param items:  消息列表
        :param userid:  用户ID
        :return: 成功或失败
        """
        pass

    def post_torrents_message(self, title: str, items: List[TorrentInfo],
                              userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param title: 标题
        :param items:  消息列表
        :param userid:  用户ID
        :return: 成功或失败
        """
        pass

    def scrape_metadata(self, path: str, mediainfo: MediaInfo) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        pass
