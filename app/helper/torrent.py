import datetime
import re
from pathlib import Path
from typing import Tuple, Optional, List, Union
from urllib.parse import unquote

from requests import Response
from torrentool.api import Torrent

from app.core.config import settings
from app.core.context import Context
from app.core.metainfo import MetaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.utils.http import RequestUtils
from app.schemas.types import MediaType, SystemConfigKey
from app.utils.singleton import Singleton


class TorrentHelper(metaclass=Singleton):
    """
    种子帮助类
    """

    # 失败的种子：站点链接
    _invalid_torrents = []

    def __init__(self):
        self.system_config = SystemConfigOper()

    def download_torrent(self, url: str,
                         cookie: str = None,
                         ua: str = None,
                         referer: str = None,
                         proxy: bool = False) \
            -> Tuple[Optional[Path], Optional[Union[str, bytes]], Optional[str], Optional[list], Optional[str]]:
        """
        把种子下载到本地
        :return: 种子保存路径、种子内容、种子主目录、种子文件清单、错误信息
        """
        if url.startswith("magnet:"):
            return None, url, "", [], f"磁力链接"
        # 请求种子文件
        req = RequestUtils(
            ua=ua,
            cookies=cookie,
            referer=referer,
            proxies=settings.PROXY if proxy else None
        ).get_res(url=url, allow_redirects=False)
        while req and req.status_code in [301, 302]:
            url = req.headers['Location']
            if url and url.startswith("magnet:"):
                return None, url, "", [], f"获取到磁力链接"
            req = RequestUtils(
                ua=ua,
                cookies=cookie,
                referer=referer,
                proxies=settings.PROXY if proxy else None
            ).get_res(url=url, allow_redirects=False)
        if req and req.status_code == 200:
            if not req.content:
                return None, None, "", [], "未下载到种子数据"
            # 解析内容格式
            if req.text and str(req.text).startswith("magnet:"):
                # 磁力链接
                return None, req.text, "", [], f"获取到磁力链接"
            elif req.text and "下载种子文件" in req.text:
                # 首次下载提示页面
                skip_flag = False
                try:
                    forms = re.findall(r'<form.*?action="(.*?)".*?>(.*?)</form>', req.text, re.S)
                    for form in forms:
                        action = form[0]
                        if action != "?":
                            continue
                        action = url
                        inputs = re.findall(r'<input.*?name="(.*?)".*?value="(.*?)".*?>', form[1], re.S)
                        if inputs:
                            data = {}
                            for item in inputs:
                                data[item[0]] = item[1]
                            # 改写req
                            req = RequestUtils(
                                ua=ua,
                                cookies=cookie,
                                referer=referer,
                                proxies=settings.PROXY if proxy else None
                            ).post_res(url=action, data=data)
                            if req and req.status_code == 200:
                                # 检查是不是种子文件，如果不是抛出异常
                                Torrent.from_string(req.content)
                                # 跳过成功
                                logger.info(f"触发了站点首次种子下载，已自动跳过：{url}")
                                skip_flag = True
                            elif req is not None:
                                logger.warn(f"触发了站点首次种子下载，且无法自动跳过，"
                                            f"返回码：{req.status_code}，错误原因：{req.reason}")
                            else:
                                logger.warn(f"触发了站点首次种子下载，且无法自动跳过：{url}")
                        break
                except Exception as err:
                    logger.warn(f"触发了站点首次种子下载，尝试自动跳过时出现错误：{str(err)}，链接：{url}")
                if not skip_flag:
                    return None, None, "", [], "种子数据有误，请确认链接是否正确，如为PT站点则需手工在站点下载一次种子"
            # 种子内容
            if req.content:
                # 检查是不是种子文件，如果不是仍然抛出异常
                try:
                    # 读取种子文件名
                    file_name = self.get_url_filename(req, url)
                    # 种子文件路径
                    file_path = Path(settings.TEMP_PATH) / file_name
                    # 保存到文件
                    file_path.write_bytes(req.content)
                    # 获取种子目录和文件清单
                    folder_name, file_list = self.get_torrent_info(file_path)
                    # 成功拿到种子数据
                    return file_path, req.content, folder_name, file_list, ""
                except Exception as err:
                    logger.error(f"种子文件解析失败：{str(err)}")
                # 种子数据仍然错误
                return None, None, "", [], "种子数据有误，请确认链接是否正确"
            # 返回失败
            return None, None, "", [], ""
        elif req is None:
            return None, None, "", [], "无法打开链接"
        elif req.status_code == 429:
            return None, None, "", [], "触发站点流控，请稍后重试"
        else:
            # 把错误的种子记下来，避免重复使用
            self.add_invalid(url)
            return None, None, "", [], f"下载种子出错，状态码：{req.status_code}"

    @staticmethod
    def get_torrent_info(torrent_path: Path) -> Tuple[str, List[str]]:
        """
        获取种子文件的文件夹名和文件清单
        :param torrent_path: 种子文件路径
        :return: 文件夹名、文件清单，单文件种子返回空文件夹名
        """
        if not torrent_path or not torrent_path.exists():
            return "", []
        try:
            torrentinfo = Torrent.from_file(torrent_path)
            # 获取文件清单
            if (not torrentinfo.files
                    or (len(torrentinfo.files) == 1
                        and torrentinfo.files[0].name == torrentinfo.name)):
                # 单文件种子目录名返回空
                folder_name = ""
                # 单文件种子
                file_list = [torrentinfo.name]
            else:
                # 目录名
                folder_name = torrentinfo.name
                # 文件清单，如果一级目录与种子名相同则去掉
                file_list = []
                for fileinfo in torrentinfo.files:
                    file_path = Path(fileinfo.name)
                    # 根路径
                    root_path = file_path.parts[0]
                    if root_path == folder_name:
                        file_list.append(str(file_path.relative_to(root_path)))
                    else:
                        file_list.append(fileinfo.name)
            logger.debug(f"解析种子：{torrent_path.name} => 目录：{folder_name}，文件清单：{file_list}")
            return folder_name, file_list
        except Exception as err:
            logger.error(f"种子文件解析失败：{str(err)}")
            return "", []

    @staticmethod
    def get_url_filename(req: Response, url: str) -> str:
        """
        从下载请求中获取种子文件名
        """
        if not req:
            return ""
        disposition = req.headers.get('content-disposition') or ""
        file_name = re.findall(r"filename=\"?(.+)\"?", disposition)
        if file_name:
            file_name = unquote(str(file_name[0].encode('ISO-8859-1').decode()).split(";")[0].strip())
            if file_name.endswith('"'):
                file_name = file_name[:-1]
        elif url and url.endswith(".torrent"):
            file_name = unquote(url.split("/")[-1])
        else:
            file_name = str(datetime.datetime.now())
        return file_name

    def sort_torrents(self, torrent_list: List[Context]) -> List[Context]:
        """
        对种子对行排序
        """
        if not torrent_list:
            return []

        def get_sort_str(_context):
            """
            排序函数，值越大越优先
            """
            _meta = _context.meta_info
            _torrent = _context.torrent_info
            _media = _context.media_info
            # 站点优先级
            _site_order = 999 - (_torrent.site_order or 0)
            # 季数
            _season_len = str(len(_meta.season_list)).rjust(2, '0')
            # 集数
            if not _meta.episode_list:
                # 无集数的排最前面
                _episode_len = "9999"
            else:
                # 集数越多的排越前面
                _episode_len = str(len(_meta.episode_list)).rjust(4, '0')
            # 优先规则
            priority = self.system_config.get(SystemConfigKey.TorrentsPriority)
            if priority != "site":
                # 排序：标题、资源类型、做种、季集
                return "%s%s%s%s" % (str(_media.title).ljust(100, ' '),
                                     str(_torrent.pri_order).rjust(3, '0'),
                                     str(_torrent.seeders).rjust(10, '0'),
                                     "%s%s" % (_season_len, _episode_len))
            else:
                # 排序：标题、资源类型、站点、做种、季集
                return "%s%s%s%s%s" % (str(_media.title).ljust(100, ' '),
                                       str(_torrent.pri_order).rjust(3, '0'),
                                       str(_site_order).rjust(3, '0'),
                                       str(_torrent.seeders).rjust(10, '0'),
                                       "%s%s" % (_season_len, _episode_len))

        # 匹配的资源中排序分组选最好的一个下载
        # 按站点顺序、资源匹配顺序、做种人数下载数逆序排序
        torrent_list = sorted(torrent_list, key=lambda x: get_sort_str(x), reverse=True)

        return torrent_list

    def sort_group_torrents(self, torrent_list: List[Context]) -> List[Context]:
        """
        对媒体信息进行排序、去重
        """
        if not torrent_list:
            return []

        # 排序
        torrent_list = self.sort_torrents(torrent_list)

        # 控重
        result = []
        _added = []
        # 排序后重新加入数组，按真实名称控重，即只取每个名称的第一个
        for context in torrent_list:
            # 控重的主链是名称、年份、季、集
            meta = context.meta_info
            media = context.media_info
            if media.type == MediaType.TV:
                media_name = "%s%s" % (media.title_year,
                                       meta.season_episode)
            else:
                media_name = media.title_year
            if media_name not in _added:
                _added.append(media_name)
                result.append(context)

        return result

    @staticmethod
    def get_torrent_episodes(files: list) -> list:
        """
        从种子的文件清单中获取所有集数
        """
        episodes = []
        for file in files:
            if not file:
                continue
            file_path = Path(file)
            if file_path.suffix not in settings.RMT_MEDIAEXT:
                continue
            # 只使用文件名识别
            meta = MetaInfo(file_path.stem)
            if not meta.begin_episode:
                continue
            episodes = list(set(episodes).union(set(meta.episode_list)))
        return episodes

    def is_invalid(self, url: str) -> bool:
        """
        判断种子是否是无效种子
        """
        return url in self._invalid_torrents

    def add_invalid(self, url: str):
        """
        添加无效种子
        """
        if url not in self._invalid_torrents:
            self._invalid_torrents.append(url)
