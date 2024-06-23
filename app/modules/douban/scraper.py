from pathlib import Path
from typing import Union, Optional
from xml.dom import minidom

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.log import logger
from app.schemas.types import MediaType
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils


class DoubanScraper:
    _transfer_type = settings.TRANSFER_TYPE
    _force_nfo = False
    _force_img = False

    def get_metadata_nfo(self, mediainfo: MediaInfo, season: int = None) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param mediainfo: 媒体信息
        :param season: 季号
        """
        if mediainfo.type == MediaType.MOVIE:
            # 电影元数据文件
            doc = self.__gen_movie_nfo_file(mediainfo=mediainfo)
        else:
            if season:
                # 季元数据文件
                doc = self.__gen_tv_season_nfo_file(mediainfo=mediainfo, season=season)
            else:
                # 电视剧元数据文件
                doc = self.__gen_tv_nfo_file(mediainfo=mediainfo)
        if doc:
            return doc.toprettyxml(indent="  ", encoding="utf-8")

        return None

    @staticmethod
    def get_metadata_img(mediainfo: MediaInfo, season: int = None) -> Optional[dict]:
        """
        获取图片内容
        :param mediainfo: 媒体信息
        :param season: 季号
        """
        ret_dict = {}
        if season:
            # 豆瓣无季图片
            return {}
        if mediainfo.poster_path:
            ret_dict[f"poster{Path(mediainfo.poster_path).suffix}"] = mediainfo.poster_path
        if mediainfo.backdrop_path:
            ret_dict[f"backdrop{Path(mediainfo.backdrop_path).suffix}"] = mediainfo.backdrop_path
        return ret_dict

    def gen_scraper_files(self, meta: MetaBase, mediainfo: MediaInfo,
                          file_path: Path, transfer_type: str,
                          force_nfo: bool = False, force_img: bool = False):
        """
        生成刮削文件
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param file_path: 文件路径或者目录路径
        :param transfer_type: 转输类型
        :param force_nfo: 强制生成NFO
        :param force_img: 强制生成图片
        """

        if not mediainfo or not file_path:
            return

        self._transfer_type = transfer_type
        self._force_nfo = force_nfo
        self._force_img = force_img

        try:
            # 电影
            if mediainfo.type == MediaType.MOVIE:
                # 强制或者不已存在时才处理
                if self._force_nfo or (not file_path.with_name("movie.nfo").exists()
                                       and not file_path.with_suffix(".nfo").exists()):
                    #  生成电影描述文件
                    self.__gen_movie_nfo_file(mediainfo=mediainfo,
                                              file_path=file_path)
                # 生成电影图片
                image_dict = self.get_metadata_img(mediainfo)
                for img_name, img_url in image_dict.items():
                    image_path = file_path.with_name(img_name)
                    if self._force_img or not image_path.exists():
                        self.__save_image(url=img_url,
                                          file_path=image_path)
            # 电视剧
            else:
                # 不存在时才处理
                if self._force_nfo or not file_path.parent.with_name("tvshow.nfo").exists():
                    # 根目录描述文件
                    self.__gen_tv_nfo_file(mediainfo=mediainfo,
                                           dir_path=file_path.parents[1])
                # 生成根目录图片
                image_dict = self.get_metadata_img(mediainfo)
                for img_name, img_url in image_dict.items():
                    image_path = file_path.with_name(img_name)
                    if self._force_img or not image_path.exists():
                        self.__save_image(url=img_url,
                                          file_path=image_path)
                # 季目录NFO
                if self._force_nfo or not file_path.with_name("season.nfo").exists():
                    self.__gen_tv_season_nfo_file(mediainfo=mediainfo,
                                                  season=meta.begin_season,
                                                  season_path=file_path.parent)
        except Exception as e:
            logger.error(f"{file_path} 刮削失败：{str(e)}")

    @staticmethod
    def __gen_common_nfo(mediainfo: MediaInfo, doc: minidom.Document, root: minidom.Node):
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        # 导演
        for director in mediainfo.directors:
            DomUtils.add_node(doc, root, "director", director.get("name") or "")
        # 演员
        for actor in mediainfo.actors:
            xactor = DomUtils.add_node(doc, root, "actor")
            DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
            DomUtils.add_node(doc, xactor, "type", "Actor")
            DomUtils.add_node(doc, xactor, "role", actor.get("character") or actor.get("role") or "")
            DomUtils.add_node(doc, xactor, "thumb", actor.get('avatar', {}).get('normal'))
            DomUtils.add_node(doc, xactor, "profile", actor.get('url'))
        # 评分
        DomUtils.add_node(doc, root, "rating", mediainfo.vote_average or "0")

        return doc

    def __gen_movie_nfo_file(self,
                             mediainfo: MediaInfo,
                             file_path: Path = None) -> minidom.Document:
        """
        生成电影的NFO描述文件
        :param mediainfo: 豆瓣信息
        :param file_path: 电影文件路径
        """
        # 开始生成XML
        if file_path:
            logger.info(f"正在生成电影NFO文件：{file_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "movie")
        # 公共部分
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        # 年份
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        # 保存
        if file_path:
            self.__save_nfo(doc, file_path.with_suffix(".nfo"))

        return doc

    def __gen_tv_nfo_file(self,
                          mediainfo: MediaInfo,
                          dir_path: Path = None) -> minidom.Document:
        """
        生成电视剧的NFO描述文件
        :param mediainfo: 媒体信息
        :param dir_path: 电视剧根目录
        """
        # 开始生成XML
        logger.info(f"正在生成电视剧NFO文件：{dir_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        # 公共部分
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        # 年份
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")
        # 保存
        if dir_path:
            self.__save_nfo(doc, dir_path.joinpath("tvshow.nfo"))

        return doc

    def __gen_tv_season_nfo_file(self, mediainfo: MediaInfo,
                                 season: int, season_path: Path = None) -> minidom.Document:
        """
        生成电视剧季的NFO描述文件
        :param mediainfo: 媒体信息
        :param season: 季号
        :param season_path: 电视剧季的目录
        """
        logger.info(f"正在生成季NFO文件：{season_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "season")
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        # 标题
        DomUtils.add_node(doc, root, "title", "季 %s" % season)
        # 发行日期
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        DomUtils.add_node(doc, root, "releasedate", mediainfo.release_date or "")
        # 发行年份
        DomUtils.add_node(doc, root, "year", mediainfo.release_date[:4] if mediainfo.release_date else "")
        # seasonnumber
        DomUtils.add_node(doc, root, "seasonnumber", str(season))
        # 保存
        if season_path:
            self.__save_nfo(doc, season_path.joinpath("season.nfo"))
        return doc

    def __save_image(self, url: str, file_path: Path):
        """
        下载图片并保存
        """
        if not url:
            return
        try:
            # 没有后缀时，处理URL转化为jpg格式
            if not file_path.suffix:
                url = url.replace("/format/webp", "/format/jpg")
                file_path.with_suffix(".jpg")
            logger.info(f"正在下载{file_path.stem}图片：{url} ...")
            r = RequestUtils().get_res(url=url)
            if r:
                if self._transfer_type in ['rclone_move', 'rclone_copy']:
                    self.__save_remove_file(file_path, r.content)
                else:
                    file_path.write_bytes(r.content)
                logger.info(f"图片已保存：{file_path}")
            else:
                logger.info(f"{file_path.stem}图片下载失败，请检查网络连通性")
        except Exception as err:
            logger.error(f"{file_path.stem}图片下载失败：{str(err)}")

    def __save_nfo(self, doc, file_path: Path):
        """
        保存NFO
        """
        xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
        if self._transfer_type in ['rclone_move', 'rclone_copy']:
            self.__save_remove_file(file_path, xml_str)
        else:
            file_path.write_bytes(xml_str)
        logger.info(f"NFO文件已保存：{file_path}")

    def __save_remove_file(self, out_file: Path, content: Union[str, bytes]):
        """
        保存文件到远端
        """
        temp_file = settings.TEMP_PATH / str(out_file)[1:]
        temp_file_dir = temp_file.parent
        if not temp_file_dir.exists():
            temp_file_dir.mkdir(parents=True, exist_ok=True)
        temp_file.write_bytes(content)
        if self._transfer_type == 'rclone_move':
            SystemUtils.rclone_move(temp_file, out_file)
        elif self._transfer_type == 'rclone_copy':
            SystemUtils.rclone_copy(temp_file, out_file)
