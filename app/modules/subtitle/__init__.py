import shutil
import time
from pathlib import Path
from typing import Tuple, Union

from lxml import etree

from app.core.config import settings
from app.core.context import Context
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.modules import _ModuleBase
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class SubtitleModule(_ModuleBase):
    """
    字幕下载模块
    """

    # 站点详情页字幕下载链接识别XPATH
    _SITE_SUBTITLE_XPATH = [
        '//td[@class="rowhead"][text()="字幕"]/following-sibling::td//a/@href',
    ]

    def init_module(self) -> None:
        pass

    @staticmethod
    def get_name() -> str:
        return "站点字幕"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def stop(self) -> None:
        pass

    def test(self):
        pass

    def download_added(self, context: Context, download_dir: Path, torrent_path: Path = None) -> None:
        """
        添加下载任务成功后，从站点下载字幕，保存到下载目录
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param download_dir:  下载目录
        :param torrent_path:  种子文件地址
        :return: None，该方法可被多个模块同时处理
        """
        if not settings.DOWNLOAD_SUBTITLE:
            return None

        # 没有种子文件不处理
        if not torrent_path:
            return

        # 没有详情页不处理
        torrent = context.torrent_info
        if not torrent.page_url:
            return
        # 字幕下载目录
        logger.info("开始从站点下载字幕：%s" % torrent.page_url)
        # 获取种子信息
        folder_name, _ = TorrentHelper.get_torrent_info(torrent_path)
        # 文件保存目录，如果是单文件种子，则folder_name是空，此时文件保存目录就是下载目录
        download_dir = download_dir / folder_name
        # 等待目录存在
        for _ in range(30):
            if download_dir.exists():
                break
            time.sleep(1)
        # 目录仍然不存在，且有文件夹名，则创建目录
        if not download_dir.exists() and folder_name:
            download_dir.mkdir(parents=True, exist_ok=True)
        # 读取网站代码
        request = RequestUtils(cookies=torrent.site_cookie, ua=torrent.site_ua)
        res = request.get_res(torrent.page_url)
        if res and res.status_code == 200:
            if not res.text:
                logger.warn(f"读取页面代码失败：{torrent.page_url}")
                return
            html = etree.HTML(res.text)
            sublink_list = []
            for xpath in self._SITE_SUBTITLE_XPATH:
                sublinks = html.xpath(xpath)
                if sublinks:
                    for sublink in sublinks:
                        if not sublink:
                            continue
                        if not sublink.startswith("http"):
                            base_url = StringUtils.get_base_url(torrent.page_url)
                            if sublink.startswith("/"):
                                sublink = "%s%s" % (base_url, sublink)
                            else:
                                sublink = "%s/%s" % (base_url, sublink)
                        sublink_list.append(sublink)
            # 下载所有字幕文件
            for sublink in sublink_list:
                logger.info(f"找到字幕下载链接：{sublink}，开始下载...")
                # 下载
                ret = request.get_res(sublink)
                if ret and ret.status_code == 200:
                    # 保存ZIP
                    file_name = TorrentHelper.get_url_filename(ret, sublink)
                    if not file_name:
                        logger.warn(f"链接不是字幕文件：{sublink}")
                        continue
                    if file_name.lower().endswith(".zip"):
                        # ZIP包
                        zip_file = settings.TEMP_PATH / file_name
                        # 保存
                        zip_file.write_bytes(ret.content)
                        # 解压路径
                        zip_path = zip_file.with_name(zip_file.stem)
                        # 解压文件
                        shutil.unpack_archive(zip_file, zip_path, format='zip')
                        # 遍历转移文件
                        for sub_file in SystemUtils.list_files(zip_path, settings.RMT_SUBEXT):
                            target_sub_file = download_dir / sub_file.name
                            if target_sub_file.exists():
                                logger.info(f"字幕文件已存在：{target_sub_file}")
                                continue
                            logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
                            SystemUtils.copy(sub_file, target_sub_file)
                        # 删除临时文件
                        try:
                            shutil.rmtree(zip_path)
                            zip_file.unlink()
                        except Exception as err:
                            logger.error(f"删除临时文件失败：{str(err)}")
                    else:
                        sub_file = settings.TEMP_PATH / file_name
                        # 保存
                        sub_file.write_bytes(ret.content)
                        target_sub_file = download_dir / sub_file.name
                        logger.info(f"转移字幕 {sub_file} 到 {target_sub_file}")
                        SystemUtils.copy(sub_file, target_sub_file)
                else:
                    logger.error(f"下载字幕文件失败：{sublink}")
                    continue
            if sublink_list:
                logger.info(f"{torrent.page_url} 页面字幕下载完成")
            else:
                logger.warn(f"{torrent.page_url} 页面未找到字幕下载链接")
        elif res is not None:
            logger.warn(f"连接 {torrent.page_url} 失败，状态码：{res.status_code}")
        else:
            logger.warn(f"无法打开链接：{torrent.page_url}")
