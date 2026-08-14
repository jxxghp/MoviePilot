import re
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union
from urllib.parse import urljoin, urlparse

from lxml import etree

from app.chain.storage import StorageChain
from app.runtime.config import settings
from app.domain.context import Context
from app.db.oper.site import SiteOper
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.application.torrent import TorrentHelper
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.schemas import TorrentInfo
from app.schemas.file import FileURI 
from app.schemas.types import ModuleType, OtherModulesType
from app.adapters.network.http import RequestUtils
from app.adapters.system.host import SystemUtils


class SubtitleModule(_ModuleBase):
    """
    字幕下载模块
    """

    _SUBTITLE_ARCHIVE_FORMATS = {
        ".zip": "zip",
        ".rar": "rar",
    }

    # 站点详情页字幕下载元素识别XPATH
    _SITE_SUBTITLE_XPATH = [
        '//td[@class="rowhead"][text()="字幕"]/following-sibling::td//a[not(@class)]',
        '//td[@class="rowhead"][text()="字幕"]/following-sibling::td//a',
        '//div[contains(@class, "font-bold")][text()="字幕"]/following-sibling::div[1]//a[not(@class)]', # 憨憨
    ]
    _SUBTITLE_URL_ATTRS = (
        "href",
        "data-url",
        "data-href",
        "data-link",
        "data-download",
        "data-download-url",
    )
    _SCRIPT_URL_RE = re.compile(
        r"""["'](?P<url>(?:https?:)?//[^"']+|/[^"']+|[^"']*(?:download|subtitle|subs?)[^"']*)["']""",
        re.IGNORECASE,
    )

    def init_module(self) -> None:
        pass

    @staticmethod
    def get_name() -> str:
        return "站点字幕"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.Subtitle

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def stop(self) -> None:
        pass

    def test(self):
        pass

    @classmethod
    def __normalize_subtitle_link(cls, page_url: str, sublink: str) -> Optional[str]:
        """
        转换并过滤真实字幕下载链接
        """
        if not sublink:
            return None
        sublink = sublink.strip()
        if not sublink or sublink.startswith("#"):
            return None
        parsed = urlparse(sublink)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return None
        if sublink.startswith("//"):
            page_scheme = urlparse(page_url).scheme or "https"
            sublink = f"{page_scheme}:{sublink}"
        else:
            sublink = urljoin(page_url, sublink)
        parsed = urlparse(sublink)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return sublink

    @classmethod
    def _parse_subtitle_links(cls, html, page_url: str) -> List[str]:
        """
        从站点详情页中解析字幕下载链接
        """
        sublink_list = []
        found_links = set()
        for xpath in cls._SITE_SUBTITLE_XPATH:
            sublink_count = len(sublink_list)
            sublink_nodes = html.xpath(xpath)
            if sublink_nodes:
                for sublink_node in sublink_nodes:
                    sublinks = [sublink_node.get(attr) for attr in cls._SUBTITLE_URL_ATTRS]
                    sublinks.extend(
                        match.group("url")
                        for match in cls._SCRIPT_URL_RE.finditer(sublink_node.get("onclick") or "")
                    )
                    for sublink in sublinks:
                        sublink = cls.__normalize_subtitle_link(page_url, sublink)
                        if not sublink or sublink in found_links:
                            continue
                        found_links.add(sublink)
                        sublink_list.append(sublink)
                # 已成功匹配字幕区域，后续xpath可以忽略
                if len(sublink_list) > sublink_count:
                    break
        return sublink_list

    def _get_subtitle_links(self, torrent: TorrentInfo):
        """
        获取字幕链接
        """
        # API请求方式的站点需要特殊处理
        if torrent.site is not None:
            site = SiteOper().get(torrent.site)
            if indexer := SitesHelper().get_indexer(site.domain):
                if indexer.get("parser") == "mTorrent":
                    return MTorrentSpider(indexer).get_subtitle_links(
                        torrent.page_url
                    )
                # TODO 其它采用API访问的站点
        # 普通站点通过解析网站代码的方式获取
        request = RequestUtils(
            cookies=torrent.site_cookie,
            ua=torrent.site_ua,
            proxies=settings.PROXY if torrent.site_proxy else None,
        )
        res = request.get_res(torrent.page_url)
        if res and res.status_code == 200:
            if not res.text:
                logger.warn(f"读取页面代码失败：{torrent.page_url}")
                return []
            html = etree.HTML(res.text)
            try:
                return self._parse_subtitle_links(html, torrent.page_url)
            finally:
                if html is not None:
                    del html
        elif res is not None:
            logger.warn(f"连接 {torrent.page_url} 失败，状态码：{res.status_code}")
        else:
            logger.warn(f"无法打开链接：{torrent.page_url}")
        return None

    def download_added(self, context: Context, download_dir: Path, torrent_content: Union[str, bytes] = None):
        """
        添加下载任务成功后，从站点下载字幕，保存到下载目录
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param download_dir:  下载目录
        :param torrent_content: 种子内容，如果是种子文件，则为文件内容，否则为种子字符串
        :return: None，该方法可被多个模块同时处理
        """
        if not settings.DOWNLOAD_SUBTITLE:
            return

        # 没有种子文件不处理
        if not torrent_content:
            return

        # 没有详情页不处理
        torrent = context.torrent_info
        if not torrent.page_url:
            return
        # 字幕下载目录
        logger.info("开始从站点下载字幕：%s" % torrent.page_url)
        # 获取种子信息
        folder_name, _ = TorrentHelper().get_fileinfo_from_torrent_content(torrent_content)
        # 文件保存目录，如果是单文件种子，则folder_name是空，此时文件保存目录就是下载目录
        storageChain = StorageChain()
        # 等待目录存在
        working_dir_item = None
        # split download_dir into storage and path
        fileURI = FileURI.from_uri(download_dir.as_posix())
        storage = fileURI.storage
        download_dir = Path(fileURI.path)
        for _ in range(30):
            found = storageChain.get_file_item(storage,  download_dir / folder_name)
            if found:
                working_dir_item = found
                break
            time.sleep(1)
        # 目录仍然不存在，且有文件夹名，则创建目录
        if not working_dir_item and folder_name:
            parent_dir_item = storageChain.get_folder(storage, download_dir)
            if parent_dir_item:
                working_dir_item = storageChain.create_folder(
                    parent_dir_item,
                    folder_name
                )
            else:
                logger.error(f"下载根目录不存在，无法创建字幕文件夹：{download_dir}")
                return
        if not working_dir_item:
            logger.error(f"下载目录不存在，无法保存字幕：{download_dir / folder_name}")
            return
        # 读取网站代码
        sublink_list = self._get_subtitle_links(torrent)
        if not sublink_list:
            logger.warn(f"{torrent.page_url} 页面未找到字幕下载链接")
            return
        # 下载所有字幕文件
        request = RequestUtils(
            cookies=torrent.site_cookie,
            ua=torrent.site_ua,
            proxies=settings.PROXY if torrent.site_proxy else None,
        )
        settings.TEMP_PATH.mkdir(parents=True, exist_ok=True)
        for sublink in sublink_list:
            logger.info(f"找到字幕下载链接：{sublink}，开始下载...")
            # 下载
            ret = request.get_res(sublink)
            if ret and ret.status_code == 200:
                file_name = TorrentHelper.get_url_filename(ret, sublink)
                if not file_name:
                    logger.warn(f"链接不是字幕文件：{sublink}")
                    continue
                archive_format = self._SUBTITLE_ARCHIVE_FORMATS.get(Path(file_name).suffix.lower())
                if archive_format:
                    archive_file = settings.TEMP_PATH / file_name
                    # 保存
                    archive_file.write_bytes(ret.content)
                    # 解压路径
                    archive_path = archive_file.with_name(archive_file.stem)
                    try:
                        # 解压文件
                        SystemUtils.unpack_archive(
                            archive_file,
                            archive_path,
                            archive_format=archive_format,
                        )
                        # 遍历转移文件
                        for sub_file in SystemUtils.list_files(archive_path, settings.RMT_SUBEXT):
                            target_sub_file = Path(working_dir_item.path) / Path(sub_file.name)
                            if storageChain.get_file_item(storage, target_sub_file):
                                logger.info(f"字幕文件已存在：{target_sub_file}")
                                continue
                            logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
                            storageChain.upload_file(working_dir_item, sub_file)
                    except Exception as err:
                        logger.error(f"字幕压缩包解压失败：{archive_file} - {str(err)}")
                    # 删除临时文件
                    try:
                        if archive_path.exists():
                            shutil.rmtree(archive_path)
                        if archive_file.exists():
                            archive_file.unlink()
                    except Exception as err:
                        logger.error(f"删除临时文件失败：{str(err)}")
                else:
                    if Path(file_name).suffix.lower() not in settings.RMT_SUBEXT:
                        logger.warn(f"链接不是支持的字幕文件：{sublink} - {file_name}")
                        continue
                    sub_file = settings.TEMP_PATH / file_name
                    # 保存
                    sub_file.write_bytes(ret.content)
                    target_sub_file = Path(working_dir_item.path) / Path(sub_file.name)
                    if storageChain.get_file_item(storage, target_sub_file):
                        logger.info(f"字幕文件已存在：{target_sub_file}")
                        continue
                    logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
                    storageChain.upload_file(working_dir_item, sub_file)
            else:
                logger.error(f"下载字幕文件失败：{sublink}")
                continue
        logger.info(f"{torrent.page_url} 页面字幕下载完成")
