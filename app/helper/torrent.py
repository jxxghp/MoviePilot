import datetime
import re
from pathlib import Path
from typing import Tuple, Optional, List, Union, Dict
from urllib.parse import unquote

from requests import Response
from torrentool.api import Torrent

from app.core.config import settings
from app.core.context import Context, TorrentInfo, MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import MediaType, SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


class TorrentHelper(metaclass=Singleton):
    """
    种子帮助类
    """

    # 失败的种子：站点链接
    _invalid_torrents = []

    def download_torrent(self, url: str,
                         cookie: Optional[str] = None,
                         ua: Optional[str] = None,
                         referer: Optional[str] = None,
                         proxy: Optional[bool] = False) \
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
            if req.content.startswith(b"magnet:"):
                # 磁力链接
                return None, req.text, "", [], f"获取到磁力链接"
            if "下载种子文件".encode("utf-8") in req.content:
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

    @staticmethod
    def sort_torrents(torrent_list: List[Context]) -> List[Context]:
        """
        对种子对行排序：torrent、site、upload、seeder
        """
        if not torrent_list:
            return []

        # 下载规则
        priority_rule: List[str] = SystemConfigOper().get(
            SystemConfigKey.TorrentsPriority) or ["torrent", "upload", "seeder"]
        # 站点上传量
        site_uploads = {
            site.name: site.upload for site in SiteOper().get_userdata_latest()
        }

        def get_sort_str(_context):
            """
            拼装排序字段
            """
            _meta = _context.meta_info
            _torrent = _context.torrent_info
            _media = _context.media_info
            # 标题
            _title = str(_media.title).ljust(200, ' ')
            # 站点优先级
            _site_order = str(999 - (_torrent.site_order or 0)).rjust(3, '0')
            # 站点上传量
            _site_upload = str(site_uploads.get(_torrent.site_name) or 0).rjust(30, '0')
            # 资源优先级
            _torrent_order = str(_torrent.pri_order or 0).rjust(3, '0')
            # 资源做种数
            _torrent_seeders = str(_torrent.seeders or 0).rjust(10, '0')
            # 季集
            if not _meta.episode_list:
                # 无集数的排最前面
                _season_episode = "%s%s" % (str(len(_meta.season_list)).rjust(3, '0'), "9999")
            else:
                # 集数越多的排越前面
                _season_episode = "%s%s" % (str(len(_meta.season_list)).rjust(3, '0'),
                                            str(len(_meta.episode_list)).rjust(4, '0'))
            # 根据下载规则的顺序拼装排序字符串
            _sort_str = _title
            for rule in priority_rule:
                if rule == "torrent":
                    _sort_str += _torrent_order
                elif rule == "site":
                    _sort_str += _site_order
                elif rule == "upload":
                    _sort_str += _site_upload
                elif rule == "seeder":
                    _sort_str += _torrent_seeders
            _sort_str += _season_episode
            return _sort_str

        # 排序
        return sorted(torrent_list, key=lambda x: get_sort_str(x), reverse=True)

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
            if not file_path.suffix or file_path.suffix.lower() not in settings.RMT_MEDIAEXT:
                continue
            # 只使用文件名识别
            meta = MetaInfo(file_path.name)
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

    @staticmethod
    def match_torrent(mediainfo: MediaInfo, torrent_meta: MetaInfo, torrent: TorrentInfo) -> bool:
        """
        检查种子是否匹配媒体信息
        :param mediainfo: 需要匹配的媒体信息
        :param torrent_meta: 种子识别信息
        :param torrent: 种子信息
        """
        # 比对词条指定的tmdbid
        if torrent_meta.tmdbid or torrent_meta.doubanid:
            if torrent_meta.tmdbid and torrent_meta.tmdbid == mediainfo.tmdb_id:
                logger.info(
                    f'{mediainfo.title} 通过词表指定TMDBID匹配到资源：{torrent.site_name} - {torrent.title}')
                return True
            if torrent_meta.doubanid and torrent_meta.doubanid == mediainfo.douban_id:
                logger.info(
                    f'{mediainfo.title} 通过词表指定豆瓣ID匹配到资源：{torrent.site_name} - {torrent.title}')
                return True
        # 要匹配的媒体标题、原标题
        media_titles = {
                           StringUtils.clear_upper(mediainfo.title),
                           StringUtils.clear_upper(mediainfo.original_title)
                       } - {""}
        # 要匹配的媒体别名、译名
        media_names = {StringUtils.clear_upper(name) for name in mediainfo.names if name}
        # 识别的种子中英文名
        meta_names = {
                         StringUtils.clear_upper(torrent_meta.cn_name),
                         StringUtils.clear_upper(torrent_meta.en_name)
                     } - {""}
        # 比对种子识别类型
        if torrent_meta.type == MediaType.TV and mediainfo.type != MediaType.TV:
            logger.debug(f'{torrent.site_name} - {torrent.title} 种子标题类型为 {torrent_meta.type.value}，'
                         f'不匹配 {mediainfo.type.value}')
            return False
        # 比对种子在站点中的类型
        if torrent.category == MediaType.TV.value and mediainfo.type != MediaType.TV:
            logger.debug(f'{torrent.site_name} - {torrent.title} 种子在站点中归类为 {torrent.category}，'
                         f'不匹配 {mediainfo.type.value}')
            return False
        # 比对年份
        if mediainfo.year:
            if mediainfo.type == MediaType.TV:
                # 剧集年份，每季的年份可能不同，没年份时不比较年份（很多剧集种子不带年份）
                if torrent_meta.year and torrent_meta.year not in [year for year in
                                                                   mediainfo.season_years.values()]:
                    logger.debug(f'{torrent.site_name} - {torrent.title} 年份不匹配 {mediainfo.season_years}')
                    return False
            else:
                # 电影年份，上下浮动1年，没年份时不通过
                if not torrent_meta.year or torrent_meta.year not in [str(int(mediainfo.year) - 1),
                                                                      mediainfo.year,
                                                                      str(int(mediainfo.year) + 1)]:
                    logger.debug(f'{torrent.site_name} - {torrent.title} 年份不匹配 {mediainfo.year}')
                    return False
        # 比对标题和原语种标题
        if meta_names.intersection(media_titles):
            logger.info(f'{mediainfo.title} 通过标题匹配到资源：{torrent.site_name} - {torrent.title}')
            return True
        # 比对别名和译名
        if media_names:
            if meta_names.intersection(media_names):
                logger.info(f'{mediainfo.title} 通过别名或译名匹配到资源：{torrent.site_name} - {torrent.title}')
                return True
        # 标题拆分
        if torrent_meta.org_string:
            # 只拆分出标题中的非英文单词进行匹配，英文单词容易误匹配（带空格的多个单词组合除外）
            titles = [StringUtils.clear_upper(t) for t in re.split(
                r'[\s/【】.\[\]\-]+',
                torrent_meta.org_string
            ) if not StringUtils.is_english_word(t)]
            # 在标题中判断是否存在标题、原语种标题
            if media_titles.intersection(titles):
                logger.info(f'{mediainfo.title} 通过标题匹配到资源：{torrent.site_name} - {torrent.title}')
                return True
        # 在副标题中（非英文单词）判断是否存在标题、原语种标题、别名、译名
        if torrent.description:
            subtitles = {StringUtils.clear_upper(t) for t in re.split(
                r'[\s/【】|]+',
                torrent.description) if not StringUtils.is_english_word(t)}
            if media_titles.intersection(subtitles) or media_names.intersection(subtitles):
                logger.info(f'{mediainfo.title} 通过副标题匹配到资源：{torrent.site_name} - {torrent.title}，'
                            f'副标题：{torrent.description}')
                return True
        # 未匹配
        logger.debug(f'{torrent.site_name} - {torrent.title} 标题不匹配，识别名称：{meta_names}')
        return False

    @staticmethod
    def filter_torrent(torrent_info: TorrentInfo,
                       filter_params: Dict[str, str]) -> bool:
        """
        检查种子是否匹配订阅过滤规则
        """

        if not filter_params:
            return True

        # 匹配内容
        content = (f"{torrent_info.title} "
                   f"{torrent_info.description} "
                   f"{' '.join(torrent_info.labels or [])} "
                   f"{torrent_info.volume_factor}")

        # 包含
        include = filter_params.get("include")
        if include:
            if not re.search(r"%s" % include, content, re.I):
                logger.info(f"{content} 不匹配包含规则 {include}")
                return False
        # 排除
        exclude = filter_params.get("exclude")
        if exclude:
            if re.search(r"%s" % exclude, content, re.I):
                logger.info(f"{content} 匹配排除规则 {exclude}")
                return False
        # 质量
        quality = filter_params.get("quality")
        if quality:
            if not re.search(r"%s" % quality, torrent_info.title, re.I):
                logger.info(f"{torrent_info.title} 不匹配质量规则 {quality}")
                return False
        # 分辨率
        resolution = filter_params.get("resolution")
        if resolution:
            if not re.search(r"%s" % resolution, torrent_info.title, re.I):
                logger.info(f"{torrent_info.title} 不匹配分辨率规则 {resolution}")
                return False
        # 特效
        effect = filter_params.get("effect")
        if effect:
            if not re.search(r"%s" % effect, torrent_info.title, re.I):
                logger.info(f"{torrent_info.title} 不匹配特效规则 {effect}")
                return False

        # 大小
        size_range = filter_params.get("size")
        if size_range:
            if size_range.find("-") != -1:
                # 区间
                size_min, size_max = size_range.split("-")
                size_min = float(size_min.strip()) * 1024 * 1024
                size_max = float(size_max.strip()) * 1024 * 1024
                if torrent_info.size < size_min or torrent_info.size > size_max:
                    return False
            elif size_range.startswith(">"):
                # 大于
                size_min = float(size_range[1:].strip()) * 1024 * 1024
                if torrent_info.size < size_min:
                    return False
            elif size_range.startswith("<"):
                # 小于
                size_max = float(size_range[1:].strip()) * 1024 * 1024
                if torrent_info.size > size_max:
                    return False

        return True

    @staticmethod
    def match_season_episodes(torrent: TorrentInfo, meta: MetaBase, season_episodes: Dict[int, list]) -> bool:
        """
        判断种子是否匹配季集数
        :param torrent: 种子信息
        :param meta: 种子元数据
        :param season_episodes: 季集数 {season:[episodes]}
        """
        # 匹配季
        seasons = season_episodes.keys()
        # 种子季
        torrent_seasons = meta.season_list
        if not torrent_seasons:
            # 按第一季处理
            torrent_seasons = [1]
        # 种子集
        torrent_episodes = meta.episode_list
        if not set(torrent_seasons).issubset(set(seasons)):
            # 种子季不在过滤季中
            logger.debug(
                f"种子 {torrent.site_name} - {torrent.title} 包含季 {torrent_seasons} 不是需要的季 {list(seasons)}")
            return False
        if not torrent_episodes:
            # 整季按匹配处理
            return True
        if len(torrent_seasons) == 1:
            need_episodes = season_episodes.get(torrent_seasons[0])
            if need_episodes \
                    and not set(torrent_episodes).intersection(set(need_episodes)):
                # 单季集没有交集的不要
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} "
                             f"集 {torrent_episodes} 没有需要的集：{need_episodes}")
                return False
        return True
