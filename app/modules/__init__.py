from abc import abstractmethod, ABCMeta
from pathlib import Path
from typing import Optional, List, Tuple, Union, Set, Any, Dict

from ruamel.yaml import CommentedMap

from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.meta import MetaBase
from app.schemas.context import TransferInfo, TransferTorrent, ExistMediaInfo
from app.utils.types import TorrentStatus, MediaType


class _ModuleBase(metaclass=ABCMeta):
    """
    模块基类，实现对应方法，在有需要时会被自动调用，返回None代表不启用该模块，将继续执行下一模块
    输入参数与输出参数一致的，或没有输出的，可以被多个模块重复实现
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
        :return: 处理后的标题、副标题，该方法可被多个模块同时处理
        """
        pass

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        tmdbid: int = None) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :return: 识别的媒体信息，包括剧集信息
        """
        pass

    def obtain_image(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息，该方法可被多个模块同时处理
        """
        pass

    def douban_info(self, doubanid: str) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :return: 识别的媒体信息
        """
        pass

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: 识别的媒体信息，包括剧集信息
        """
        pass

    def message_parser(self, body: Any, form: Any, args: Any) -> Optional[dict]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 消息内容、用户ID
        """
        pass

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[dict]:
        """
        解析Webhook报文体
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        pass

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息
        """
        pass

    def media_exists(self, mediainfo: MediaInfo) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        pass

    def search_torrents(self, mediainfo: Optional[MediaInfo], sites: List[CommentedMap],
                        keyword: str = None) -> Optional[List[TorrentInfo]]:
        """
        搜索站点，多个站点需要多线程处理
        :param mediainfo:  识别的媒体信息
        :param sites:  站点列表
        :param keyword:  搜索关键词，如有按关键词搜索，否则按媒体信息名称搜索
        :reutrn: 资源列表
        """
        pass

    def refresh_torrents(self, sites: List[CommentedMap]) -> Optional[List[TorrentInfo]]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param sites:  站点列表
        :reutrn: 种子资源列表
        """
        pass

    def filter_torrents(self, torrent_list: List[TorrentInfo],
                        season_episodes: Dict[int, dict] = None) -> List[TorrentInfo]:
        """
        过滤资源
        :param torrent_list:  资源列表
        :param season_episodes:  过滤的剧集信息
        :return: 过滤后的资源列表，该方法可被多个模块同时处理
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

    def download_added(self, context: Context, torrent_path: Path) -> None:
        """
        添加下载任务后的处理
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param torrent_path:  种子文件地址
        :return: None，该方法可被多个模块同时处理
        """
        pass

    def list_torrents(self, status: TorrentStatus = None,
                      hashs: Union[list, str] = None) -> Optional[List[TransferTorrent]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :return: 下载器中符合状态的种子列表
        """
        pass

    def transfer(self, path: Path, mediainfo: MediaInfo) -> Optional[TransferInfo]:
        """
        转移一个路径下的文件
        :param path:  文件路径
        :param mediainfo:  识别的媒体信息
        :return: {path, target_path, message}
        """
        pass

    def transfer_completed(self, hashs: Union[str, list], transinfo: TransferInfo) -> None:
        """
        转移完成后的处理
        :param hashs:  种子Hash
        :param transinfo:  转移信息
        :return: None，该方法可被多个模块同时处理
        """
        pass

    def remove_torrents(self, hashs: Union[str, list]) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :return: bool
        """
        pass

    def refresh_mediaserver(self, mediainfo: MediaInfo, file_path: Path) -> Optional[bool]:
        """
        刷新媒体库
        :param mediainfo:  识别的媒体信息
        :param file_path:  文件路径
        :return: 成功或失败
        """
        pass

    def post_message(self, title: str, text: str = None,
                     image: str = None, userid: Union[str, int] = None) -> Optional[bool]:
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

    def post_torrents_message(self, title: str, items: List[Context],
                              mediainfo: MediaInfo,
                              userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param title: 标题
        :param items:  消息列表
        :param mediainfo:  识别的媒体信息
        :param userid:  用户ID
        :return: 成功或失败
        """
        pass

    def scrape_metadata(self, path: str, mediainfo: MediaInfo) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :return: None，该方法可被多个模块同时处理
        """
        pass

    def register_commands(self, commands: dict) -> None:
        """
        注册命令，实现这个函数接收系统可用的命令菜单
        :param commands: 命令字典
        :return: None，该方法可被多个模块同时处理
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        如果关闭时模块有服务需要停止，需要实现此方法
        :return: None，该方法可被多个模块同时处理
        """
        pass
